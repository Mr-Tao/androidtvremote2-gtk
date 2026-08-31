"""Threaded asyncio boundary around the Android TV Remote protocol client."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any, Protocol

try:
    from androidtvremote2 import AndroidTVRemote, CannotConnect, ConnectionClosed, InvalidAuth
except ModuleNotFoundError:  # pragma: no cover - exercised only without the runtime dependency
    AndroidTVRemote = None  # type: ignore[assignment,misc]

    class CannotConnect(Exception):
        """Fallback used by injected fakes when androidtvremote2 is unavailable."""

    class ConnectionClosed(Exception):
        """Fallback used by injected fakes when androidtvremote2 is unavailable."""

    class InvalidAuth(Exception):
        """Fallback used by injected fakes when androidtvremote2 is unavailable."""


from .discovery import discover_devices
from .models import ConnectionStatus, DeviceConfig, DiscoveredDevice, RemoteState
from .storage import DeviceStore, DeviceStoreError

_LOGGER = logging.getLogger(__name__)
_CLIENT_NAME = "Android TV Remote 2 GTK"


class RemoteClient(Protocol):
    """Subset of androidtvremote2 used by the controller."""

    device_info: dict[str, Any] | None
    is_on: bool | None
    current_app: str | None
    volume_info: dict[str, Any] | None

    async def async_generate_cert_if_missing(self) -> bool: ...

    async def async_start_pairing(self) -> None: ...

    async def async_finish_pairing(self, pairing_code: str) -> None: ...

    async def async_connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def keep_reconnecting(self, invalid_auth_callback: Callable[[], None] | None = None) -> None: ...

    def add_is_on_updated_callback(self, callback: Callable[[bool], None]) -> None: ...

    def add_current_app_updated_callback(self, callback: Callable[[str], None]) -> None: ...

    def add_volume_info_updated_callback(self, callback: Callable[[dict[str, Any]], None]) -> None: ...

    def add_is_available_updated_callback(self, callback: Callable[[bool], None]) -> None: ...

    def send_key_command(self, code: int | str, direction: int | str = "SHORT") -> None: ...

    def send_text(self, text: str) -> None: ...

    def send_launch_app_command(self, link: str) -> None: ...


RemoteFactory = Callable[..., RemoteClient]
StateCallback = Callable[[RemoteState], None]
DiscoveryCallback = Callable[[list[DiscoveredDevice], str | None], None]
UIDispatcher = Callable[[Callable[[], bool]], object]
DiscoveryFunction = Callable[[float], Awaitable[list[DiscoveredDevice]]]


def _default_remote_factory(**kwargs: Any) -> RemoteClient:
    if AndroidTVRemote is None:
        raise RuntimeError("androidtvremote2 is required unless remote_factory is injected")
    return AndroidTVRemote(**kwargs)


class RemoteController:
    """Own one protocol client and a dedicated, non-UI asyncio loop thread."""

    def __init__(
        self,
        state_callback: StateCallback,
        discovery_callback: DiscoveryCallback | None = None,
        ui_dispatcher: UIDispatcher | None = None,
        *,
        remote_factory: RemoteFactory | None = None,
        discovery_function: DiscoveryFunction | None = None,
        store: DeviceStore | None = None,
        connect_timeout: float = 10.0,
    ) -> None:
        if connect_timeout <= 0:
            raise ValueError("connect_timeout must be positive")
        self._state_callback = state_callback
        self._discovery_callback = discovery_callback
        self._dispatcher = ui_dispatcher or (lambda callback: callback())
        self._remote_factory = remote_factory or _default_remote_factory
        self._discovery_function = discovery_function or discover_devices
        self._store = store or DeviceStore()
        self._connect_timeout = connect_timeout

        self._state_lock = threading.Lock()
        self._state = RemoteState()
        self._generation = 0
        self._authenticated_generation: int | None = None
        self._connected_generation: int | None = None
        self._pairing_submission_generation: int | None = None
        self._discovery_generation = 0
        self._shutdown_started = False
        self._shutdown_completion: concurrent.futures.Future[None] | None = None
        self._shutdown_error: BaseException | None = None

        self._loop = asyncio.new_event_loop()
        self._remote: RemoteClient | None = None
        self._remote_generation: int | None = None
        self._device: DeviceConfig | None = None
        self._connect_future: concurrent.futures.Future[bool] | None = None
        self._discovery_future: concurrent.futures.Future[list[DiscoveredDevice]] | None = None
        self._thread_ready = threading.Event()
        self._thread = threading.Thread(target=self._run_loop, name="androidtvremote2-worker", daemon=True)
        self._thread.start()
        self._thread_ready.wait()

    @property
    def state(self) -> RemoteState:
        """Return the most recently published immutable state snapshot."""
        with self._state_lock:
            return self._state

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._thread_ready.set()
        try:
            self._loop.run_forever()
        finally:
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            if pending:
                self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self._loop.close()

    def _dispatch(self, callback: Callable[[], None]) -> None:
        def invoke() -> bool:
            callback()
            return False

        try:
            self._dispatcher(invoke)
        except Exception:
            _LOGGER.exception("UI callback dispatcher failed")

    def _publish(self, generation: int, **changes: object) -> None:
        with self._state_lock:
            if generation != self._generation:
                return
            self._state = replace(self._state, **changes)
            state = self._state

        def deliver() -> None:
            with self._state_lock:
                if generation != self._generation:
                    return
            self._state_callback(state)

        self._dispatch(deliver)

    def _new_generation(self) -> int:
        with self._state_lock:
            if self._shutdown_started:
                raise RuntimeError("controller is shut down")
            self._generation += 1
            self._authenticated_generation = None
            self._connected_generation = None
            self._pairing_submission_generation = None
            return self._generation

    def _submit(self, coroutine: Awaitable[Any]) -> concurrent.futures.Future[Any]:
        with self._state_lock:
            if self._shutdown_started:
                if asyncio.iscoroutine(coroutine):
                    coroutine.close()
                future: concurrent.futures.Future[Any] = concurrent.futures.Future()
                future.set_exception(RuntimeError("controller is shut down"))
                return future
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop)

    def connect(self, device: DeviceConfig) -> concurrent.futures.Future[bool]:
        """Begin connecting or pairing a device without blocking the caller."""
        generation = self._new_generation()
        previous = self._connect_future
        if previous is not None and not previous.done():
            previous.cancel()
        future = self._submit(self._connect_device(device, generation))
        self._connect_future = future
        return future

    async def _connect_device(self, device: DeviceConfig, generation: int) -> bool:
        await self._disconnect_remote()
        self._publish(
            generation,
            status=ConnectionStatus.CONNECTING,
            device=device,
            manufacturer=None,
            model=None,
            software_version=None,
            is_on=None,
            volume_level=None,
            volume_max=None,
            is_muted=None,
            current_app=None,
            error=None,
        )
        cert_path, key_path = self._store.credential_paths(device, create=not device.paired)
        if device.paired and not self._store.credentials_available(device):
            self._publish(
                generation, status=ConnectionStatus.AUTH_REQUIRED, error="Saved pairing credentials are unavailable"
            )
            return False

        try:
            remote = self._remote_factory(
                client_name=_CLIENT_NAME,
                certfile=str(cert_path),
                keyfile=str(key_path),
                host=device.host,
                api_port=device.api_port,
                pair_port=device.pair_port,
                loop=self._loop,
                enable_ime=device.enable_ime,
            )
            self._remote = remote
            self._remote_generation = generation
            self._device = device
            self._register_callbacks(remote, generation)
            if not device.paired:
                await remote.async_generate_cert_if_missing()
                self._store.secure_credentials(device)
                self._store.upsert(device)
                await remote.async_start_pairing()
                self._publish(generation, status=ConnectionStatus.PAIRING, error=None)
                return True
            return await self._authenticate(remote, device, generation, newly_paired=False)
        except asyncio.CancelledError:
            if self._remote_generation == generation:
                await self._disconnect_remote()
            raise
        except InvalidAuth:
            self._mark_auth_failed(generation)
            self._disconnect_generation_remote(generation)
            self._publish(generation, status=ConnectionStatus.AUTH_REQUIRED, error="Pairing authentication is required")
        except (CannotConnect, ConnectionClosed, asyncio.TimeoutError):
            self._mark_not_connected(generation)
            self._disconnect_generation_remote(generation)
            self._publish(generation, status=ConnectionStatus.FAILED, error="Unable to connect to the device")
        except (DeviceStoreError, OSError, ValueError):
            self._mark_not_connected(generation)
            self._disconnect_generation_remote(generation)
            self._publish(generation, status=ConnectionStatus.FAILED, error="Unable to prepare the device identity")
        except Exception:
            _LOGGER.exception("Unexpected controller connection failure")
            self._mark_not_connected(generation)
            self._disconnect_generation_remote(generation)
            self._publish(generation, status=ConnectionStatus.FAILED, error="Unexpected controller error")
        return False

    def _register_callbacks(self, remote: RemoteClient, generation: int) -> None:
        remote.add_is_on_updated_callback(lambda value: self._queue_remote_update(generation, is_on=value))
        remote.add_current_app_updated_callback(lambda value: self._queue_remote_update(generation, current_app=value))
        remote.add_volume_info_updated_callback(lambda value: self._queue_volume_update(generation, value))
        remote.add_is_available_updated_callback(lambda value: self._queue_availability_update(generation, value))

    def _queue_on_loop(self, callback: Callable[..., None], *args: object) -> None:
        if threading.current_thread() is self._thread:
            callback(*args)
        elif not self._loop.is_closed():
            self._loop.call_soon_threadsafe(callback, *args)

    def _queue_remote_update(self, generation: int, **changes: object) -> None:
        self._queue_on_loop(lambda: self._publish(generation, **changes))

    def _queue_volume_update(self, generation: int, volume: dict[str, Any]) -> None:
        self._queue_on_loop(
            lambda: self._publish(
                generation,
                volume_level=volume.get("level"),
                volume_max=volume.get("max"),
                is_muted=volume.get("muted"),
            )
        )

    def _queue_availability_update(self, generation: int, available: bool) -> None:
        self._queue_on_loop(self._availability_update, generation, available)

    def _availability_update(self, generation: int, available: bool) -> None:
        with self._state_lock:
            if generation != self._generation or self._authenticated_generation != generation:
                return
            status = self._state.status
            if available:
                if status is not ConnectionStatus.RECONNECTING:
                    return
                self._connected_generation = generation
            else:
                if status is not ConnectionStatus.CONNECTED:
                    return
                self._connected_generation = None
        if available:
            self._publish(generation, status=ConnectionStatus.CONNECTED, error=None, **self._snapshot())
            return
        self._publish(generation, status=ConnectionStatus.RECONNECTING, error="Connection interrupted")

    def _invalid_auth_update(self, generation: int) -> None:
        with self._state_lock:
            if generation != self._generation:
                return
            self._authenticated_generation = None
            self._connected_generation = None
        self._disconnect_generation_remote(generation)
        self._publish(generation, status=ConnectionStatus.AUTH_REQUIRED, error="Pairing authentication is required")

    def _snapshot(self) -> dict[str, object]:
        remote = self._remote
        if remote is None:
            return {}
        device_info = remote.device_info or {}
        volume_info = remote.volume_info or {}
        return {
            "manufacturer": device_info.get("manufacturer"),
            "model": device_info.get("model"),
            "software_version": device_info.get("sw_version"),
            "is_on": remote.is_on,
            "volume_level": volume_info.get("level"),
            "volume_max": volume_info.get("max"),
            "is_muted": volume_info.get("muted"),
            "current_app": remote.current_app,
        }

    async def _authenticate(
        self,
        remote: RemoteClient,
        device: DeviceConfig,
        generation: int,
        *,
        newly_paired: bool,
    ) -> bool:
        try:
            await asyncio.wait_for(remote.async_connect(), timeout=self._connect_timeout)
        except InvalidAuth:
            self._mark_auth_failed(generation)
            self._disconnect_generation_remote(generation)
            self._publish(generation, status=ConnectionStatus.AUTH_REQUIRED, error="Pairing authentication is required")
            return False
        except (CannotConnect, ConnectionClosed, asyncio.TimeoutError):
            self._mark_not_connected(generation)
            self._disconnect_generation_remote(generation)
            self._publish(generation, status=ConnectionStatus.FAILED, error="Unable to connect to the device")
            return False
        if generation != self._generation:
            remote.disconnect()
            return False

        connected_device = device
        if newly_paired:
            connected_device = replace(device, paired=True)
            try:
                self._store.secure_credentials(device)
                self._store.upsert(connected_device)
            except DeviceStoreError:
                remote.disconnect()
                self._publish(generation, status=ConnectionStatus.FAILED, error="Unable to persist the paired identity")
                return False
            self._device = connected_device
        remote.keep_reconnecting(lambda: self._queue_on_loop(self._invalid_auth_update, generation))
        with self._state_lock:
            if generation != self._generation:
                remote.disconnect()
                return False
            self._authenticated_generation = generation
            self._connected_generation = generation
        self._publish(
            generation,
            status=ConnectionStatus.CONNECTED,
            device=connected_device,
            error=None,
            **self._snapshot(),
        )
        return True

    def finish_pairing(self, code: str) -> concurrent.futures.Future[bool]:
        """Submit the displayed pairing code without blocking the caller."""
        if not isinstance(code, str) or not code.strip():
            future: concurrent.futures.Future[bool] = concurrent.futures.Future()
            future.set_result(False)
            return future
        with self._state_lock:
            generation = self._generation
            pairing = (
                self._state.status is ConnectionStatus.PAIRING and self._pairing_submission_generation != generation
            )
            if pairing:
                self._pairing_submission_generation = generation
        if not pairing:
            future = concurrent.futures.Future()
            future.set_result(False)
            return future
        self._publish(generation, status=ConnectionStatus.CONNECTING, error=None)
        future = self._submit(self._finish_pairing(code.strip(), generation))
        self._connect_future = future
        return future

    async def _finish_pairing(self, code: str, generation: int) -> bool:
        remote = self._remote
        device = self._device
        if remote is None or device is None or generation != self._remote_generation:
            self._release_pairing_submission(generation)
            return False
        try:
            await remote.async_finish_pairing(code)
        except InvalidAuth:
            self._release_pairing_submission(generation)
            self._publish(generation, status=ConnectionStatus.PAIRING, error="The pairing code was rejected")
            return False
        except (CannotConnect, ConnectionClosed, asyncio.TimeoutError):
            self._release_pairing_submission(generation)
            self._publish(generation, status=ConnectionStatus.FAILED, error="Pairing did not complete")
            return False
        except Exception:
            _LOGGER.exception("Unexpected pairing completion failure")
            self._release_pairing_submission(generation)
            self._publish(generation, status=ConnectionStatus.FAILED, error="Unexpected pairing error")
            return False
        return await self._authenticate(remote, device, generation, newly_paired=True)

    def _release_pairing_submission(self, generation: int) -> None:
        with self._state_lock:
            if self._pairing_submission_generation == generation:
                self._pairing_submission_generation = None

    def reset_pairing(self) -> concurrent.futures.Future[bool]:
        """Forget the selected identity and start pairing again after confirmation."""
        with self._state_lock:
            device = self._state.device
            reset_allowed = self._state.status is ConnectionStatus.AUTH_REQUIRED and device is not None
        if not reset_allowed or device is None:
            future: concurrent.futures.Future[bool] = concurrent.futures.Future()
            future.set_result(False)
            return future

        generation = self._new_generation()
        previous = self._connect_future
        if previous is not None and not previous.done():
            previous.cancel()
        future = self._submit(self._reset_pairing(device, generation))
        self._connect_future = future
        return future

    async def _reset_pairing(self, device: DeviceConfig, generation: int) -> bool:
        await self._disconnect_remote()
        try:
            reset_device = self._store.reset_pairing(device.id)
        except (DeviceStoreError, OSError, ValueError):
            self._publish(
                generation,
                status=ConnectionStatus.FAILED,
                device=device,
                error="Unable to forget the saved pairing identity",
            )
            return False
        return await self._connect_device(reset_device, generation)

    def disconnect(self) -> concurrent.futures.Future[bool]:
        """Disconnect the active generation without blocking the caller."""
        generation = self._new_generation()
        previous = self._connect_future
        if previous is not None and not previous.done():
            previous.cancel()
        return self._submit(self._disconnect(generation))

    async def _disconnect(self, generation: int) -> bool:
        await self._disconnect_remote()
        self._publish(
            generation,
            status=ConnectionStatus.DISCONNECTED,
            device=None,
            manufacturer=None,
            model=None,
            software_version=None,
            is_on=None,
            volume_level=None,
            volume_max=None,
            is_muted=None,
            current_app=None,
            error=None,
        )
        return True

    async def _disconnect_remote(self, *, propagate: bool = False) -> None:
        remote = self._remote
        self._remote = None
        self._remote_generation = None
        self._device = None
        if remote is not None:
            try:
                remote.disconnect()
            except Exception:
                if propagate:
                    raise
                _LOGGER.exception("Unable to disconnect the protocol client")

    def _disconnect_generation_remote(self, generation: int) -> None:
        if self._remote_generation != generation:
            return
        remote = self._remote
        self._remote = None
        self._remote_generation = None
        self._device = None
        if remote is not None:
            try:
                remote.disconnect()
            except Exception:
                _LOGGER.exception("Unable to disconnect an unauthenticated protocol client")

    def _mark_not_connected(self, generation: int) -> None:
        with self._state_lock:
            if generation == self._generation:
                self._connected_generation = None

    def _mark_auth_failed(self, generation: int) -> None:
        with self._state_lock:
            if generation == self._generation:
                self._authenticated_generation = None
                self._connected_generation = None

    def _command(self, method: str, *args: object) -> concurrent.futures.Future[bool]:
        with self._state_lock:
            generation = self._generation
            connected = self._connected_generation == generation and self._state.status is ConnectionStatus.CONNECTED
        if not connected:
            future: concurrent.futures.Future[bool] = concurrent.futures.Future()
            future.set_result(False)
            return future
        return self._submit(self._run_command(generation, method, *args))

    async def _run_command(self, generation: int, method: str, *args: object) -> bool:
        with self._state_lock:
            connected = self._connected_generation == generation and self._state.status is ConnectionStatus.CONNECTED
        remote = self._remote
        if not connected or remote is None or self._remote_generation != generation:
            return False
        try:
            getattr(remote, method)(*args)
        except ConnectionClosed:
            self._mark_not_connected(generation)
            self._publish(generation, status=ConnectionStatus.RECONNECTING, error="Connection interrupted")
            return False
        except (TypeError, ValueError):
            self._publish(generation, error="The command was rejected")
            return False
        return True

    def send_key(self, code: int | str, direction: int | str = "SHORT") -> concurrent.futures.Future[bool]:
        """Send a key command only if the current generation is connected."""
        return self._command("send_key_command", code, direction)

    def send_text(self, text: str) -> concurrent.futures.Future[bool]:
        """Send text only if the current generation is connected."""
        return self._command("send_text", text)

    def launch_app(self, link: str) -> concurrent.futures.Future[bool]:
        """Launch an app link only if the current generation is connected."""
        return self._command("send_launch_app_command", link)

    def discover(self, timeout: float = 3.0) -> concurrent.futures.Future[list[DiscoveredDevice]]:
        """Run injected or zeroconf discovery on the worker loop."""
        with self._state_lock:
            if self._shutdown_started:
                future: concurrent.futures.Future[list[DiscoveredDevice]] = concurrent.futures.Future()
                future.set_exception(RuntimeError("controller is shut down"))
                return future
            self._discovery_generation += 1
            generation = self._discovery_generation
        previous = self._discovery_future
        if previous is not None and not previous.done():
            previous.cancel()
        future = self._submit(self._discover(timeout, generation))
        self._discovery_future = future
        return future

    async def _discover(self, timeout: float, generation: int) -> list[DiscoveredDevice]:
        error: str | None = None
        try:
            devices = await self._discovery_function(timeout)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("Android TV discovery failed")
            devices = []
            error = "Unable to discover Android TV devices"
        if self._discovery_callback is not None:

            def deliver() -> None:
                with self._state_lock:
                    if generation != self._discovery_generation or self._shutdown_started:
                        return
                self._discovery_callback(devices, error)

            self._dispatch(deliver)
        return devices

    def shutdown(self) -> concurrent.futures.Future[None]:
        """Gracefully stop the protocol client and worker loop without blocking."""
        with self._state_lock:
            if self._shutdown_completion is not None:
                return self._shutdown_completion
            self._shutdown_started = True
            self._generation += 1
            self._authenticated_generation = None
            self._connected_generation = None
            self._pairing_submission_generation = None
            self._discovery_generation += 1
            generation = self._generation
            completion: concurrent.futures.Future[None] = concurrent.futures.Future()
            self._shutdown_completion = completion
        task = asyncio.run_coroutine_threadsafe(self._shutdown(generation), self._loop)
        task.add_done_callback(self._shutdown_finished)
        threading.Thread(
            target=self._complete_shutdown_after_join,
            args=(completion,),
            name="androidtvremote2-shutdown",
            daemon=True,
        ).start()
        return completion

    def _complete_shutdown_after_join(self, completion: concurrent.futures.Future[None]) -> None:
        self._thread.join()
        if not completion.done():
            if self._shutdown_error is not None:
                completion.set_exception(self._shutdown_error)
            else:
                completion.set_result(None)

    def _shutdown_finished(self, task: concurrent.futures.Future[None]) -> None:
        try:
            task.result()
        except BaseException as exc:
            self._shutdown_error = exc
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)

    async def _shutdown(self, generation: int) -> None:
        disconnect_error: Exception | None = None
        try:
            await self._disconnect_remote(propagate=True)
        except Exception as exc:
            disconnect_error = exc
        self._publish(
            generation,
            status=ConnectionStatus.SHUTDOWN,
            device=None,
            manufacturer=None,
            model=None,
            software_version=None,
            is_on=None,
            volume_level=None,
            volume_max=None,
            is_muted=None,
            current_app=None,
            error=None,
        )
        current = asyncio.current_task()
        pending = [task for task in asyncio.all_tasks() if task is not current]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if disconnect_error is not None:
            raise disconnect_error
