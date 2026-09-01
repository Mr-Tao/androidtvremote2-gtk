import asyncio
import concurrent.futures
import stat
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from androidtvremote2_gtk.controller import (
    CannotConnect,
    ConnectionClosed,
    InvalidAuth,
    RemoteController,
    _wait_remote_closed,
)
from androidtvremote2_gtk.discovery import ServiceIdentityError
from androidtvremote2_gtk.models import ConnectionStatus, DeviceConfig, DiscoveredDevice, RemoteState
from androidtvremote2_gtk.storage import DeviceStore


class StateRecorder:
    def __init__(self) -> None:
        self.states: list[RemoteState] = []
        self.dispatch_count = 0
        self.condition = threading.Condition()

    def dispatch(self, callback: Callable[[], bool]) -> bool:
        self.dispatch_count += 1
        return callback()

    def callback(self, state: RemoteState) -> None:
        with self.condition:
            self.states.append(state)
            self.condition.notify_all()

    def wait_for(self, status: ConnectionStatus) -> RemoteState:
        with self.condition:
            assert self.condition.wait_for(
                lambda: any(state.status is status for state in self.states),
                timeout=2,
            )
            return next(state for state in reversed(self.states) if state.status is status)


class FakeRemote:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.generate_calls = 0
        self.start_pairing_calls = 0
        self.finish_codes: list[str] = []
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.disconnect_exception: Exception | None = None
        self.keep_reconnecting_calls = 0
        self.connect_exception: Exception | None = None
        self.command_exception: Exception | None = None
        self.command_calls: list[tuple[str, tuple[object, ...], int]] = []
        self.is_on_callbacks: list[Callable[[bool], None]] = []
        self.app_callbacks: list[Callable[[str], None]] = []
        self.volume_callbacks: list[Callable[[dict[str, Any]], None]] = []
        self.loop: asyncio.AbstractEventLoop = kwargs["loop"]
        self.closed: asyncio.Future[Exception | None] = self.loop.create_future()
        self.device_info = {"manufacturer": "Example", "model": "Panel", "sw_version": "1.0"}
        self.is_on = True
        self.current_app = "com.example.home"
        self.volume_info = {"level": 8, "max": 20, "muted": False}

    async def async_generate_cert_if_missing(self) -> bool:
        self.generate_calls += 1
        await asyncio.to_thread(Path(self.kwargs["certfile"]).write_text, "generated certificate", encoding="utf-8")
        await asyncio.to_thread(Path(self.kwargs["keyfile"]).write_text, "generated key", encoding="utf-8")
        return True

    async def async_start_pairing(self) -> None:
        self.start_pairing_calls += 1

    async def async_finish_pairing(self, code: str) -> None:
        self.finish_codes.append(code)

    async def async_connect(self) -> None:
        self.connect_calls += 1
        if self.connect_exception is not None:
            raise self.connect_exception

    def disconnect(self) -> None:
        self.disconnect_calls += 1
        if not self.closed.done():
            self.closed.set_result(None)
        if self.disconnect_exception is not None:
            raise self.disconnect_exception

    async def async_wait_closed(self) -> Exception | None:
        return await asyncio.shield(self.closed)

    def lose_connection(self, reason: Exception | None = None) -> None:
        def complete() -> None:
            if not self.closed.done():
                self.closed.set_result(reason)

        self.loop.call_soon_threadsafe(complete)

    def keep_reconnecting(self, invalid_auth_callback: Callable[[], None] | None = None) -> None:
        del invalid_auth_callback
        self.keep_reconnecting_calls += 1

    def add_is_on_updated_callback(self, callback: Callable[[bool], None]) -> None:
        self.is_on_callbacks.append(callback)

    def add_current_app_updated_callback(self, callback: Callable[[str], None]) -> None:
        self.app_callbacks.append(callback)

    def add_volume_info_updated_callback(self, callback: Callable[[dict[str, Any]], None]) -> None:
        self.volume_callbacks.append(callback)

    def _command(self, name: str, *args: object) -> None:
        self.command_calls.append((name, args, threading.get_ident()))
        if self.command_exception is not None:
            raise self.command_exception

    def send_key_command(self, code: int | str, direction: int | str = "SHORT") -> None:
        self._command("key", code, direction)

    def send_text(self, text: str) -> None:
        self._command("text", text)

    def send_launch_app_command(self, link: str) -> None:
        self._command("launch", link)


class FakeFactory:
    def __init__(self) -> None:
        self.remotes: list[FakeRemote] = []
        self.next_exception: Exception | None = None

    def __call__(self, **kwargs: Any) -> FakeRemote:
        remote = FakeRemote(**kwargs)
        remote.connect_exception = self.next_exception
        self.next_exception = None
        self.remotes.append(remote)
        return remote


def test_wait_remote_closed_supports_pinned_androidtvremote2_0_3_lifecycle() -> None:
    async def exercise() -> None:
        connection_lost: asyncio.Future[Exception | None] = asyncio.get_running_loop().create_future()
        remote = type(
            "ProtocolOnlyRemote",
            (),
            {"_remote_message_protocol": type("Protocol", (), {"on_con_lost": connection_lost})()},
        )()
        reason = ConnectionClosed("closed")
        connection_lost.set_result(reason)

        assert await _wait_remote_closed(remote) is reason  # type: ignore[arg-type]

    asyncio.run(exercise())


def make_store(tmp_path: Path) -> DeviceStore:
    return DeviceStore(tmp_path / "config", tmp_path / "data")


def add_managed_credentials(store: DeviceStore, device: DeviceConfig) -> None:
    cert_path, key_path = store.credential_paths(device, create=True)
    cert_path.write_text("test certificate", encoding="utf-8")
    key_path.write_text("test key", encoding="utf-8")


def make_controller(
    recorder: StateRecorder,
    factory: FakeFactory,
    store: DeviceStore,
    **kwargs: Any,
) -> RemoteController:
    return RemoteController(
        recorder.callback,
        ui_dispatcher=recorder.dispatch,
        remote_factory=factory,
        store=store,
        connect_timeout=1,
        **kwargs,
    )


def discovered_endpoint(host: str, *, identifier: str = "AA:BB:CC:DD:EE:FF") -> DiscoveredDevice:
    return DiscoveredDevice(
        name="TV",
        host=host,
        port=6466,
        service_name="TV._androidtvremote2._tcp.local.",
        service_target="android-tv.local.",
        service_identifier=identifier,
        addresses=(host,),
    )


def test_connect_commands_callbacks_disconnect_and_shutdown(tmp_path: Path) -> None:
    recorder = StateRecorder()
    factory = FakeFactory()
    store = make_store(tmp_path)
    device = DeviceConfig(id="tv", display_name="TV", host="tv.local", paired=True)
    add_managed_credentials(store, device)
    controller = make_controller(recorder, factory, store)
    main_thread = threading.get_ident()

    assert controller.connect(device).result(timeout=2) is True
    connected = recorder.wait_for(ConnectionStatus.CONNECTED)
    remote = factory.remotes[0]
    assert connected.device == device
    assert connected.manufacturer == "Example"
    assert connected.volume_level == 8
    assert connected.current_app == "com.example.home"
    assert remote.keep_reconnecting_calls == 0
    assert controller.send_key("HOME").result(timeout=2) is True
    assert controller.send_text("hello").result(timeout=2) is True
    assert controller.launch_app("https://example.test/app").result(timeout=2) is True
    assert all(call[2] != main_thread for call in remote.command_calls)

    remote.volume_callbacks[0]({"level": 10, "max": 20, "muted": True})
    with recorder.condition:
        assert recorder.condition.wait_for(lambda: controller.state.volume_level == 10, timeout=2)
    assert controller.state.is_muted is True
    remote.lose_connection(ConnectionClosed("simulated loss"))
    with recorder.condition:
        assert recorder.condition.wait_for(
            lambda: len(factory.remotes) == 2 and controller.state.status is ConnectionStatus.CONNECTED,
            timeout=2,
        )
    replacement = factory.remotes[1]
    assert replacement is not remote
    assert controller.send_key("HOME").result(timeout=2) is True

    factory.next_exception = InvalidAuth("invalid on reconnect")
    replacement.lose_connection(ConnectionClosed("simulated loss"))
    recorder.wait_for(ConnectionStatus.AUTH_REQUIRED)
    assert controller.send_key("HOME").result(timeout=2) is False

    assert controller.disconnect().result(timeout=2) is True
    recorder.wait_for(ConnectionStatus.DISCONNECTED)
    assert controller.send_key("HOME").result(timeout=2) is False
    controller.shutdown().result(timeout=2)
    assert controller.state.status is ConnectionStatus.SHUTDOWN
    assert not controller._thread.is_alive()
    assert recorder.dispatch_count == len(recorder.states)


def test_dns_sd_endpoint_is_resolved_before_connect_and_persisted_after_auth(tmp_path: Path) -> None:
    recorder = StateRecorder()
    factory = FakeFactory()
    store = make_store(tmp_path)
    device = DeviceConfig(
        id="tv",
        display_name="TV",
        host="192.0.2.10",
        paired=True,
        service_name="TV._androidtvremote2._tcp.local.",
        service_target="old-target.local.",
        service_identifier="AA:BB:CC:DD:EE:FF",
    )
    add_managed_credentials(store, device)
    store.upsert(device)
    calls: list[tuple[str, str | None, float]] = []

    async def resolver(name: str, identifier: str | None, timeout: float) -> DiscoveredDevice:
        calls.append((name, identifier, timeout))
        return discovered_endpoint("192.0.2.25")

    controller = make_controller(recorder, factory, store, service_resolver=resolver)

    assert controller.connect(device).result(timeout=2) is True

    assert calls == [(device.service_name, device.service_identifier, 3.0)]
    assert factory.remotes[0].kwargs["host"] == "192.0.2.25"
    assert store.load()[0].host == "192.0.2.25"
    assert store.load()[0].service_target == "android-tv.local."
    controller.shutdown().result(timeout=2)


def test_dns_sd_resolution_outage_uses_last_known_endpoint(tmp_path: Path) -> None:
    recorder = StateRecorder()
    factory = FakeFactory()
    store = make_store(tmp_path)
    device = DeviceConfig(
        id="tv",
        display_name="TV",
        host="192.0.2.10",
        paired=True,
        service_name="TV._androidtvremote2._tcp.local.",
        service_target="android-tv.local.",
    )
    add_managed_credentials(store, device)

    async def resolver(_: str, __: str | None, ___: float) -> None:
        return None

    controller = make_controller(recorder, factory, store, service_resolver=resolver)

    assert controller.connect(device).result(timeout=2) is True
    assert factory.remotes[0].kwargs["host"] == "192.0.2.10"
    controller.shutdown().result(timeout=2)


def test_dns_sd_identity_conflict_never_constructs_a_fallback_client(tmp_path: Path) -> None:
    recorder = StateRecorder()
    factory = FakeFactory()
    store = make_store(tmp_path)
    device = DeviceConfig(
        id="tv",
        display_name="TV",
        host="192.0.2.10",
        paired=True,
        service_name="TV._androidtvremote2._tcp.local.",
        service_target="android-tv.local.",
        service_identifier="AA:BB:CC:DD:EE:FF",
    )
    add_managed_credentials(store, device)

    async def resolver(_: str, __: str | None, ___: float) -> DiscoveredDevice:
        raise ServiceIdentityError("conflicting bt")

    controller = make_controller(recorder, factory, store, service_resolver=resolver)

    assert controller.connect(device).result(timeout=2) is False
    assert controller.state.status is ConnectionStatus.FAILED
    assert factory.remotes == []
    controller.shutdown().result(timeout=2)


def test_each_reconnect_attempt_reresolves_and_uses_a_new_client(tmp_path: Path) -> None:
    recorder = StateRecorder()
    store = make_store(tmp_path)
    device = DeviceConfig(
        id="tv",
        display_name="TV",
        host="192.0.2.10",
        paired=True,
        service_name="TV._androidtvremote2._tcp.local.",
        service_target="android-tv.local.",
        service_identifier="AA:BB:CC:DD:EE:FF",
    )
    add_managed_credentials(store, device)
    store.upsert(device)
    endpoints = ["192.0.2.21", "192.0.2.22", "192.0.2.23", "192.0.2.24"]
    events: list[tuple[str, str]] = []

    async def resolver(_: str, __: str | None, ___: float) -> DiscoveredDevice:
        host = endpoints[len([event for event in events if event[0] == "resolve"])]
        events.append(("resolve", host))
        return discovered_endpoint(host)

    remotes: list[FakeRemote] = []

    def factory(**kwargs: Any) -> FakeRemote:
        remote = FakeRemote(**kwargs)
        if len(remotes) in {1, 2}:
            remote.connect_exception = CannotConnect("simulated retry failure")
        remotes.append(remote)
        events.append(("construct", kwargs["host"]))
        return remote

    controller = RemoteController(
        recorder.callback,
        ui_dispatcher=recorder.dispatch,
        remote_factory=factory,
        service_resolver=resolver,
        store=store,
        connect_timeout=1,
    )
    assert controller.connect(device).result(timeout=2) is True

    remotes[0].lose_connection(ConnectionClosed("simulated loss"))
    with recorder.condition:
        assert recorder.condition.wait_for(
            lambda: len(remotes) == 4 and controller.state.status is ConnectionStatus.CONNECTED,
            timeout=3,
        )

    assert events == [
        item
        for pair in zip(
            [("resolve", host) for host in endpoints],
            [("construct", host) for host in endpoints],
            strict=True,
        )
        for item in pair
    ]
    assert len({id(remote) for remote in remotes}) == 4
    assert store.load()[0].host == "192.0.2.24"
    controller.shutdown().result(timeout=2)


def test_pairing_and_post_pair_authentication_resolve_independently(tmp_path: Path) -> None:
    recorder = StateRecorder()
    factory = FakeFactory()
    store = make_store(tmp_path)
    device = DeviceConfig(
        id="tv",
        display_name="TV",
        host="192.0.2.10",
        service_name="TV._androidtvremote2._tcp.local.",
        service_target="android-tv.local.",
        service_identifier="AA:BB:CC:DD:EE:FF",
    )
    calls = 0

    async def resolver(_: str, __: str | None, ___: float) -> DiscoveredDevice:
        nonlocal calls
        calls += 1
        return discovered_endpoint(f"192.0.2.{20 + calls}")

    controller = make_controller(recorder, factory, store, service_resolver=resolver)

    assert controller.connect(device).result(timeout=2) is True
    recorder.wait_for(ConnectionStatus.PAIRING)
    assert controller.finish_pairing("123456").result(timeout=2) is True

    assert calls == 2
    assert [remote.kwargs["host"] for remote in factory.remotes] == ["192.0.2.21", "192.0.2.22"]
    assert store.load()[0].paired is True
    assert store.load()[0].host == "192.0.2.22"
    controller.shutdown().result(timeout=2)


def test_device_switch_during_resolution_constructs_no_stale_remote(tmp_path: Path) -> None:
    recorder = StateRecorder()
    factory = FakeFactory()
    store = make_store(tmp_path)
    first = DeviceConfig(
        id="first",
        display_name="First",
        host="192.0.2.10",
        paired=True,
        service_name="First._androidtvremote2._tcp.local.",
        service_target="first.local.",
    )
    second = DeviceConfig(id="second", display_name="Second", host="second.local", paired=True)
    add_managed_credentials(store, first)
    add_managed_credentials(store, second)
    resolution_started = threading.Event()
    resolution_release = threading.Event()

    async def resolver(_: str, __: str | None, ___: float) -> DiscoveredDevice:
        resolution_started.set()
        await asyncio.to_thread(resolution_release.wait)
        return DiscoveredDevice(
            name="First",
            host="192.0.2.30",
            service_name=first.service_name,
            service_target=first.service_target,
        )

    controller = make_controller(recorder, factory, store, service_resolver=resolver)
    stale = controller.connect(first)
    assert resolution_started.wait(timeout=2)

    assert controller.connect(second).result(timeout=2) is True
    resolution_release.set()

    assert stale.cancelled()
    assert [remote.kwargs["host"] for remote in factory.remotes] == ["second.local"]
    assert controller.state.device_id == "second"
    controller.shutdown().result(timeout=2)


def test_generation_cannot_advance_during_endpoint_client_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = StateRecorder()
    factory = FakeFactory()
    store = make_store(tmp_path)
    first = DeviceConfig(id="first", display_name="First", host="first.local", paired=True)
    second = DeviceConfig(id="second", display_name="Second", host="second.local", paired=True)
    add_managed_credentials(store, first)
    add_managed_credentials(store, second)
    construction_started = threading.Event()
    construction_release = threading.Event()
    original_credential_paths = store.credential_paths

    def blocking_credential_paths(device: DeviceConfig, *, create: bool = False) -> tuple[Path, Path]:
        construction_started.set()
        assert construction_release.wait(timeout=2)
        return original_credential_paths(device, create=create)

    monkeypatch.setattr(store, "credential_paths", blocking_credential_paths)
    controller = make_controller(recorder, factory, store)
    first_future = controller.connect(first)
    assert construction_started.wait(timeout=2)

    switched: list[concurrent.futures.Future[bool]] = []
    switch_thread = threading.Thread(target=lambda: switched.append(controller.connect(second)))
    switch_thread.start()
    switch_thread.join(timeout=0.05)

    assert switch_thread.is_alive()
    assert factory.remotes == []
    assert controller._generation == 1

    construction_release.set()
    switch_thread.join(timeout=2)
    assert not switch_thread.is_alive()
    assert switched[0].result(timeout=2) is True
    assert first_future.done()
    if not first_future.cancelled():
        assert first_future.result(timeout=0) is False
    assert controller.state.device_id == "second"
    controller.shutdown().result(timeout=2)


def test_new_device_persists_unpaired_then_marks_paired_after_authenticated_connect(tmp_path: Path) -> None:
    recorder = StateRecorder()
    factory = FakeFactory()
    store = make_store(tmp_path)
    device = DeviceConfig(id="new-tv", display_name="New TV", host="192.0.2.20")
    controller = make_controller(recorder, factory, store)

    assert controller.connect(device).result(timeout=2) is True
    recorder.wait_for(ConnectionStatus.PAIRING)
    remote = factory.remotes[0]
    assert remote.generate_calls == 1
    assert remote.start_pairing_calls == 1
    assert store.load() == [device]

    assert controller.finish_pairing("123456").result(timeout=2) is True
    connected = recorder.wait_for(ConnectionStatus.CONNECTED)
    assert connected.device is not None and connected.device.paired is True
    assert store.load() == [connected.device]
    cert_path, key_path = store.credential_paths(device)
    assert stat.S_IMODE(cert_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    controller.shutdown().result(timeout=2)


def test_pairing_code_submission_is_single_flight(tmp_path: Path) -> None:
    recorder = StateRecorder()
    store = make_store(tmp_path)
    device = DeviceConfig(id="tv", display_name="TV", host="tv.local")
    finish_started = threading.Event()
    finish_release = threading.Event()

    class BlockingPairingRemote(FakeRemote):
        async def async_finish_pairing(self, code: str) -> None:
            self.finish_codes.append(code)
            finish_started.set()
            await asyncio.to_thread(finish_release.wait)

    remotes: list[BlockingPairingRemote] = []

    def factory(**kwargs: Any) -> BlockingPairingRemote:
        remote = BlockingPairingRemote(**kwargs)
        remotes.append(remote)
        return remote

    controller = RemoteController(
        recorder.callback,
        ui_dispatcher=recorder.dispatch,
        remote_factory=factory,
        store=store,
        connect_timeout=1,
    )
    assert controller.connect(device).result(timeout=2) is True
    recorder.wait_for(ConnectionStatus.PAIRING)

    first = controller.finish_pairing("123456")
    assert finish_started.wait(timeout=2)
    assert controller.state.status is ConnectionStatus.CONNECTING
    assert controller.finish_pairing("654321").result(timeout=2) is False
    finish_release.set()

    assert first.result(timeout=2) is True
    assert remotes[0].finish_codes == ["123456"]
    controller.shutdown().result(timeout=2)


def test_first_pairing_auth_failure_can_reset_and_restart_pairing(tmp_path: Path) -> None:
    recorder = StateRecorder()
    factory = FakeFactory()
    store = make_store(tmp_path)
    device = DeviceConfig(id="tv", display_name="TV", host="tv.local")
    controller = make_controller(recorder, factory, store)

    assert controller.connect(device).result(timeout=2) is True
    recorder.wait_for(ConnectionStatus.PAIRING)
    factory.next_exception = InvalidAuth("invalid after pairing")
    assert controller.finish_pairing("123456").result(timeout=2) is False
    recorder.wait_for(ConnectionStatus.AUTH_REQUIRED)
    assert store.load() == [device]

    assert controller.reset_pairing().result(timeout=2) is True
    pairing = recorder.wait_for(ConnectionStatus.PAIRING)
    assert pairing.device == device
    assert len(factory.remotes) == 3
    assert factory.remotes[2].generate_calls == 1
    assert factory.remotes[2].start_pairing_calls == 1
    controller.shutdown().result(timeout=2)


def test_saved_device_missing_credentials_requires_auth_without_generation(tmp_path: Path) -> None:
    recorder = StateRecorder()
    factory = FakeFactory()
    store = make_store(tmp_path)
    device = DeviceConfig(id="tv", display_name="TV", host="tv.local", paired=True)
    controller = make_controller(recorder, factory, store)

    assert controller.connect(device).result(timeout=2) is False
    recorder.wait_for(ConnectionStatus.AUTH_REQUIRED)
    assert factory.remotes == []
    controller.shutdown().result(timeout=2)


def test_saved_device_invalid_auth_never_regenerates_or_starts_pairing(tmp_path: Path) -> None:
    recorder = StateRecorder()
    factory = FakeFactory()
    factory.next_exception = InvalidAuth("invalid")
    store = make_store(tmp_path)
    device = DeviceConfig(id="tv", display_name="TV", host="tv.local", paired=True)
    add_managed_credentials(store, device)
    controller = make_controller(recorder, factory, store)

    assert controller.connect(device).result(timeout=2) is False
    recorder.wait_for(ConnectionStatus.AUTH_REQUIRED)
    remote = factory.remotes[0]
    assert remote.generate_calls == 0
    assert remote.start_pairing_calls == 0
    controller.shutdown().result(timeout=2)


def test_connection_monitor_does_not_start_before_pairing_authentication(tmp_path: Path) -> None:
    recorder = StateRecorder()
    factory = FakeFactory()
    store = make_store(tmp_path)
    device = DeviceConfig(id="tv", display_name="TV", host="tv.local")
    controller = make_controller(recorder, factory, store)

    assert controller.connect(device).result(timeout=2) is True
    recorder.wait_for(ConnectionStatus.PAIRING)
    factory.remotes[0].lose_connection(ConnectionClosed("pairing transport closed"))
    controller._submit(asyncio.sleep(0)).result(timeout=2)

    assert controller.state.status is ConnectionStatus.PAIRING
    assert controller.send_key("HOME").result(timeout=2) is False
    controller.shutdown().result(timeout=2)


def test_failed_connection_has_no_monitor_that_can_reopen_state(tmp_path: Path) -> None:
    recorder = StateRecorder()
    factory = FakeFactory()
    factory.next_exception = CannotConnect("unavailable")
    store = make_store(tmp_path)
    device = DeviceConfig(id="tv", display_name="TV", host="tv.local", paired=True)
    add_managed_credentials(store, device)
    controller = make_controller(recorder, factory, store)

    assert controller.connect(device).result(timeout=2) is False
    recorder.wait_for(ConnectionStatus.FAILED)
    factory.remotes[0].lose_connection(ConnectionClosed("failed transport closed"))
    controller._submit(asyncio.sleep(0)).result(timeout=2)

    assert controller.state.status is ConnectionStatus.FAILED
    assert controller.send_key("HOME").result(timeout=2) is False
    controller.shutdown().result(timeout=2)


def test_explicit_pairing_reset_replaces_managed_identity_and_starts_pairing(tmp_path: Path) -> None:
    recorder = StateRecorder()
    factory = FakeFactory()
    factory.next_exception = InvalidAuth("invalid")
    store = make_store(tmp_path)
    device = DeviceConfig(id="tv", display_name="TV", host="tv.local", paired=True)
    add_managed_credentials(store, device)
    store.upsert(device)
    cert_path, key_path = store.credential_paths(device)
    controller = make_controller(recorder, factory, store)

    assert controller.connect(device).result(timeout=2) is False
    recorder.wait_for(ConnectionStatus.AUTH_REQUIRED)
    assert controller.reset_pairing().result(timeout=2) is True
    pairing = recorder.wait_for(ConnectionStatus.PAIRING)

    assert pairing.device is not None and pairing.device.paired is False
    assert factory.remotes[0].disconnect_calls == 1
    replacement = factory.remotes[1]
    assert replacement.generate_calls == 1
    assert replacement.start_pairing_calls == 1
    assert cert_path.read_text(encoding="utf-8") == "generated certificate"
    assert key_path.read_text(encoding="utf-8") == "generated key"
    assert store.load() == [pairing.device]

    assert controller.finish_pairing("123456").result(timeout=2) is True
    connected = recorder.wait_for(ConnectionStatus.CONNECTED)
    assert connected.device is not None and connected.device.paired is True
    assert store.load() == [connected.device]
    controller.shutdown().result(timeout=2)


def test_pairing_reset_is_rejected_without_auth_required_state(tmp_path: Path) -> None:
    recorder = StateRecorder()
    factory = FakeFactory()
    store = make_store(tmp_path)
    device = DeviceConfig(id="tv", display_name="TV", host="tv.local", paired=True)
    add_managed_credentials(store, device)
    store.upsert(device)
    controller = make_controller(recorder, factory, store)

    assert controller.connect(device).result(timeout=2) is True
    recorder.wait_for(ConnectionStatus.CONNECTED)
    assert controller.reset_pairing().result(timeout=2) is False
    assert store.load() == [device]
    assert len(factory.remotes) == 1
    controller.shutdown().result(timeout=2)


def test_pairing_reset_commit_is_atomic_with_generation_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = StateRecorder()
    factory = FakeFactory()
    factory.next_exception = InvalidAuth("invalid")
    store = make_store(tmp_path)
    device = DeviceConfig(id="tv", display_name="TV", host="tv.local", paired=True)
    replacement = DeviceConfig(id="other", display_name="Other", host="other.local", paired=True)
    add_managed_credentials(store, device)
    add_managed_credentials(store, replacement)
    store.upsert(device)
    controller = make_controller(recorder, factory, store)
    assert controller.connect(device).result(timeout=2) is False
    recorder.wait_for(ConnectionStatus.AUTH_REQUIRED)
    reset_started = threading.Event()
    reset_release = threading.Event()
    original_reset = store.reset_pairing

    def blocking_reset(device_id: str) -> DeviceConfig:
        reset_started.set()
        assert reset_release.wait(timeout=2)
        return original_reset(device_id)

    monkeypatch.setattr(store, "reset_pairing", blocking_reset)
    reset_future = controller.reset_pairing()
    assert reset_started.wait(timeout=2)
    switched: list[concurrent.futures.Future[bool]] = []
    switch_thread = threading.Thread(target=lambda: switched.append(controller.connect(replacement)))
    switch_thread.start()
    switch_thread.join(timeout=0.05)

    assert switch_thread.is_alive()
    assert controller._generation == 2

    reset_release.set()
    switch_thread.join(timeout=2)
    assert not switch_thread.is_alive()
    assert switched[0].result(timeout=2) is True
    assert reset_future.done()
    if not reset_future.cancelled():
        assert reset_future.result(timeout=0) is False
    assert store.load()[0].paired is False
    assert controller.state.device_id == "other"
    controller.shutdown().result(timeout=2)


def test_device_switch_ignores_stale_callbacks(tmp_path: Path) -> None:
    recorder = StateRecorder()
    factory = FakeFactory()
    store = make_store(tmp_path)
    first = DeviceConfig(id="first", display_name="First", host="first.local", paired=True)
    second = DeviceConfig(id="second", display_name="Second", host="second.local", paired=True)
    add_managed_credentials(store, first)
    add_managed_credentials(store, second)
    controller = make_controller(recorder, factory, store)

    assert controller.connect(first).result(timeout=2) is True
    first_remote = factory.remotes[0]
    assert controller.connect(second).result(timeout=2) is True
    recorder.wait_for(ConnectionStatus.CONNECTED)
    assert controller.state.device_id == "second"

    first_remote.app_callbacks[0]("stale.app")
    first_remote.lose_connection(ConnectionClosed("stale transport closed"))
    controller._submit(asyncio.sleep(0)).result(timeout=2)
    assert controller.send_key("HOME").result(timeout=2) is True
    assert controller.state.current_app == "com.example.home"
    assert controller.state.status is ConnectionStatus.CONNECTED
    controller.shutdown().result(timeout=2)


def test_device_switch_drops_state_queued_for_delayed_ui_delivery(tmp_path: Path) -> None:
    states: list[RemoteState] = []
    pending_callbacks: list[Callable[[], bool]] = []
    factory = FakeFactory()
    store = make_store(tmp_path)
    first = DeviceConfig(id="first", display_name="First", host="first.local", paired=True)
    second = DeviceConfig(id="second", display_name="Second", host="second.local", paired=True)
    add_managed_credentials(store, first)
    add_managed_credentials(store, second)
    controller = RemoteController(
        states.append,
        ui_dispatcher=pending_callbacks.append,
        remote_factory=factory,
        store=store,
        connect_timeout=1,
    )

    assert controller.connect(first).result(timeout=2) is True
    assert controller.connect(second).result(timeout=2) is True
    for callback in pending_callbacks:
        callback()

    assert states
    assert all(state.device_id != "first" for state in states)
    assert states[-1].device_id == "second"
    controller.shutdown().result(timeout=2)


def test_connection_closed_command_reports_state_instead_of_raising(tmp_path: Path) -> None:
    recorder = StateRecorder()
    factory = FakeFactory()
    store = make_store(tmp_path)
    device = DeviceConfig(id="tv", display_name="TV", host="tv.local", paired=True)
    add_managed_credentials(store, device)
    controller = make_controller(recorder, factory, store)
    assert controller.connect(device).result(timeout=2) is True
    factory.remotes[0].command_exception = ConnectionClosed("closed")

    assert controller.send_key("HOME").result(timeout=2) is False
    recorder.wait_for(ConnectionStatus.RECONNECTING)
    assert controller.send_text("not queued").result(timeout=2) is False
    assert [call[0] for call in factory.remotes[0].command_calls] == ["key"]
    controller.shutdown().result(timeout=2)


def test_discovery_callback_is_dispatched(tmp_path: Path) -> None:
    recorder = StateRecorder()
    discovered = [DiscoveredDevice("TV", "192.0.2.30")]
    callback_results: list[tuple[list[DiscoveredDevice], str | None]] = []

    async def fake_discovery(timeout: float) -> list[DiscoveredDevice]:
        assert timeout == 0.5
        return discovered

    controller = RemoteController(
        recorder.callback,
        lambda devices, error: callback_results.append((devices, error)),
        recorder.dispatch,
        remote_factory=FakeFactory(),
        discovery_function=fake_discovery,
        store=make_store(tmp_path),
    )

    assert controller.discover(0.5).result(timeout=2) == discovered
    assert callback_results == [(discovered, None)]
    assert recorder.dispatch_count == 1
    controller.shutdown().result(timeout=2)


def test_overlapping_discovery_only_delivers_and_reconciles_latest_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = StateRecorder()
    first_started = threading.Event()
    callback_results: list[tuple[list[DiscoveredDevice], str | None]] = []
    reconciled: list[list[DiscoveredDevice]] = []
    store = make_store(tmp_path)
    original_reconcile = store.reconcile_discovery

    def record_reconcile(devices: list[DiscoveredDevice]) -> list[DeviceConfig]:
        reconciled.append(list(devices))
        return original_reconcile(devices)

    monkeypatch.setattr(store, "reconcile_discovery", record_reconcile)

    async def fake_discovery(timeout: float) -> list[DiscoveredDevice]:
        if timeout == 1.0:
            first_started.set()
            await asyncio.Event().wait()
        return [DiscoveredDevice("Latest", "192.0.2.40")]

    controller = RemoteController(
        recorder.callback,
        lambda devices, error: callback_results.append((devices, error)),
        recorder.dispatch,
        remote_factory=FakeFactory(),
        discovery_function=fake_discovery,
        store=store,
    )

    first = controller.discover(1.0)
    assert first_started.wait(timeout=2)
    assert controller.discover(2.0).result(timeout=2) == [DiscoveredDevice("Latest", "192.0.2.40")]

    assert first.cancelled()
    assert callback_results == [([DiscoveredDevice("Latest", "192.0.2.40")], None)]
    assert reconciled == [[DiscoveredDevice("Latest", "192.0.2.40")]]
    controller.shutdown().result(timeout=2)


def test_discovery_failure_is_reported_to_ui(tmp_path: Path) -> None:
    recorder = StateRecorder()
    callback_results: list[tuple[list[DiscoveredDevice], str | None]] = []

    async def fake_discovery(_: float) -> list[DiscoveredDevice]:
        raise OSError("simulated resolver failure")

    controller = RemoteController(
        recorder.callback,
        lambda devices, error: callback_results.append((devices, error)),
        recorder.dispatch,
        remote_factory=FakeFactory(),
        discovery_function=fake_discovery,
        store=make_store(tmp_path),
    )

    assert controller.discover(0.5).result(timeout=2) == []
    assert callback_results == [([], "Unable to discover Android TV devices")]
    controller.shutdown().result(timeout=2)


def test_shutdown_cancels_an_inflight_connect_without_sleeping(tmp_path: Path) -> None:
    recorder = StateRecorder()
    store = make_store(tmp_path)
    device = DeviceConfig(id="tv", display_name="TV", host="tv.local", paired=True)
    add_managed_credentials(store, device)
    connect_started = threading.Event()

    class BlockingRemote(FakeRemote):
        async def async_connect(self) -> None:
            connect_started.set()
            await asyncio.Event().wait()

    remotes: list[BlockingRemote] = []

    def factory(**kwargs: Any) -> BlockingRemote:
        remote = BlockingRemote(**kwargs)
        remotes.append(remote)
        return remote

    controller = RemoteController(
        recorder.callback,
        ui_dispatcher=recorder.dispatch,
        remote_factory=factory,
        store=store,
        connect_timeout=30,
    )
    connect_future = controller.connect(device)
    assert connect_started.wait(timeout=2)

    controller.shutdown().result(timeout=2)

    assert connect_future.done()
    assert remotes[0].disconnect_calls == 1
    assert not controller._thread.is_alive()


def test_shutdown_reports_disconnect_failure_and_still_stops_worker(tmp_path: Path) -> None:
    recorder = StateRecorder()
    factory = FakeFactory()
    store = make_store(tmp_path)
    device = DeviceConfig(id="tv", display_name="TV", host="tv.local", paired=True)
    add_managed_credentials(store, device)
    controller = make_controller(recorder, factory, store)
    assert controller.connect(device).result(timeout=2) is True
    factory.remotes[0].disconnect_exception = RuntimeError("simulated disconnect failure")

    with pytest.raises(RuntimeError, match="simulated disconnect failure"):
        controller.shutdown().result(timeout=2)

    assert controller.state.status is ConnectionStatus.SHUTDOWN
    assert not controller._thread.is_alive()


@pytest.mark.parametrize("method", ["send_key", "send_text", "launch_app"])
def test_commands_are_rejected_before_connect(method: str, tmp_path: Path) -> None:
    recorder = StateRecorder()
    controller = make_controller(recorder, FakeFactory(), make_store(tmp_path))

    command = getattr(controller, method)
    assert command("value").result(timeout=2) is False
    controller.shutdown().result(timeout=2)
