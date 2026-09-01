import asyncio

import pytest

from androidtvremote2_gtk import discovery
from androidtvremote2_gtk.discovery import (
    SERVICE_TYPE,
    DiscoveryRecord,
    ServiceIdentityError,
    discover_devices,
    resolve_service,
)
from androidtvremote2_gtk.models import DiscoveredDevice


def record(
    name: str,
    target: str,
    addresses: tuple[str, ...],
    port: int = 6466,
    identifier: str | None = None,
) -> DiscoveryRecord:
    return DiscoveryRecord(name, target, addresses, port, identifier)


def test_discovery_dedupes_dual_stack_records_and_sorts_results() -> None:
    calls: list[tuple[str, float]] = []

    async def browse(service_type: str, timeout: float) -> list[DiscoveryRecord]:
        calls.append((service_type, timeout))
        return [
            record(
                f"Living\\032Room.{SERVICE_TYPE}",
                "living-room.local.",
                ("2001:db8::2", "192.0.2.5", "192.0.2.5"),
                identifier="aa:bb:cc:dd:ee:02",
            ),
            record(f"bedroom.{SERVICE_TYPE}", "bedroom.local.", ("2001:db8::1",)),
            record(
                f"Bedroom.{SERVICE_TYPE}",
                "bedroom-new.local.",
                ("192.0.2.4", "2001:db8::1"),
                identifier="aa:bb:cc:dd:ee:01",
            ),
        ]

    result = asyncio.run(discover_devices(1.25, browse=browse))

    assert calls == [(SERVICE_TYPE, 1.25)]
    assert result == [
        DiscoveredDevice(
            name="Bedroom",
            host="192.0.2.4",
            port=6466,
            service_name=f"Bedroom.{SERVICE_TYPE}",
            service_target="bedroom-new.local.",
            service_identifier="AA:BB:CC:DD:EE:01",
            addresses=("192.0.2.4", "2001:db8::1"),
        ),
        DiscoveredDevice(
            name="Living Room",
            host="192.0.2.5",
            port=6466,
            service_name=f"Living\\032Room.{SERVICE_TYPE}",
            service_target="living-room.local.",
            service_identifier="AA:BB:CC:DD:EE:02",
            addresses=("192.0.2.5", "2001:db8::2"),
        ),
    ]


def test_discovery_keeps_duplicate_display_names_with_distinct_service_names() -> None:
    async def browse(service_type: str, timeout: float) -> list[DiscoveryRecord]:
        return [
            record(f"Living\\032Room.{service_type}", "first.local.", ("192.0.2.10",)),
            record(f"Living Room.{service_type}", "second.local.", ("192.0.2.11",)),
        ]

    result = asyncio.run(discover_devices(browse=browse))

    assert [device.name for device in result] == ["Living Room", "Living Room"]
    assert {device.service_name for device in result} == {
        f"Living\\032Room.{SERVICE_TYPE}",
        f"Living Room.{SERVICE_TYPE}",
    }


def test_discovery_skips_invalid_records() -> None:
    async def browse(service_type: str, timeout: float) -> list[DiscoveryRecord]:
        return [
            record(f"valid.{service_type}", "valid.local.", ("192.0.2.9",)),
            record(f"invalid.{service_type}", "invalid.local.", ("not an address",)),
            record(f"empty.{service_type}", "empty.local.", ()),
        ]

    assert asyncio.run(discover_devices(browse=browse)) == [
        DiscoveredDevice(
            "valid",
            "192.0.2.9",
            service_name=f"valid.{SERVICE_TYPE}",
            service_target="valid.local.",
        )
    ]


def test_zeroconf_handler_accepts_current_keyword_callback_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeAsyncZeroconf:
        closed = False

        def __init__(self) -> None:
            self.zeroconf = object()

        async def async_close(self) -> None:
            type(self).closed = True

    class FakeServiceInfo:
        def __init__(self, service_type: str, name: str) -> None:
            assert service_type == SERVICE_TYPE
            assert name == f"TV.{SERVICE_TYPE}"
            self.name = name
            self.server = "tv.local."
            self.port = 6466
            self.properties = {b"bt": b"aa:bb:cc:dd:ee:ff"}

        async def async_request(self, zeroconf: object, timeout: int) -> bool:
            assert zeroconf is not None
            assert timeout >= 100
            return True

        def parsed_scoped_addresses(self) -> list[str]:
            return ["192.0.2.50"]

    class FakeServiceBrowser:
        def __init__(self, zeroconf: object, service_type: str, handlers: list[object]) -> None:
            assert zeroconf is not None
            assert service_type == SERVICE_TYPE
            handlers[0](
                zeroconf=zeroconf,
                service_type=service_type,
                name=f"TV.{SERVICE_TYPE}",
                state_change=discovery.ServiceStateChange.Added,
            )

        async def async_cancel(self) -> None:
            return

    monkeypatch.setattr(discovery, "AsyncZeroconf", FakeAsyncZeroconf)
    monkeypatch.setattr(discovery, "AsyncServiceInfo", FakeServiceInfo)
    monkeypatch.setattr(discovery, "AsyncServiceBrowser", FakeServiceBrowser)

    records = asyncio.run(discovery._browse_zeroconf(SERVICE_TYPE, 0.001))

    assert records == [
        record(
            f"TV.{SERVICE_TYPE}",
            "tv.local.",
            ("192.0.2.50",),
            identifier="aa:bb:cc:dd:ee:ff",
        )
    ]
    assert FakeAsyncZeroconf.closed is True


def test_exact_zeroconf_resolution_returns_metadata_and_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    service_name = f"TV.{SERVICE_TYPE}"

    class FakeInfo:
        def __init__(self, service_type: str, name: str) -> None:
            assert (service_type, name) == (SERVICE_TYPE, service_name)
            self.name = name
            self.server = "tv.local."
            self.port = 6466
            self.properties = {b"bt": b"aa:bb:cc:dd:ee:ff"}

        async def async_request(self, zeroconf: object, timeout: int) -> bool:
            assert zeroconf is FakeAsyncZeroconf.instance.zeroconf
            assert timeout == 1250
            return True

        def parsed_scoped_addresses(self) -> list[str]:
            return ["192.0.2.50", "2001:db8::50"]

    class FakeAsyncZeroconf:
        instance: "FakeAsyncZeroconf"

        def __init__(self) -> None:
            self.zeroconf = object()
            self.closed = False
            type(self).instance = self

        async def async_close(self) -> None:
            self.closed = True

    monkeypatch.setattr(discovery, "AsyncZeroconf", FakeAsyncZeroconf)
    monkeypatch.setattr(discovery, "AsyncServiceInfo", FakeInfo)

    resolved = asyncio.run(discovery._resolve_zeroconf(SERVICE_TYPE, service_name, 1.25))

    assert resolved == record(
        service_name,
        "tv.local.",
        ("192.0.2.50", "2001:db8::50"),
        identifier="aa:bb:cc:dd:ee:ff",
    )
    assert FakeAsyncZeroconf.instance.closed is True


def test_zeroconf_resolution_closes_after_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingInfo:
        def __init__(self, service_type: str, name: str) -> None:
            return

        async def async_request(self, zeroconf: object, timeout: int) -> bool:
            raise RuntimeError("resolution failed")

    class FakeAsyncZeroconf:
        instance: "FakeAsyncZeroconf"

        def __init__(self) -> None:
            self.zeroconf = object()
            self.closed = False
            type(self).instance = self

        async def async_close(self) -> None:
            self.closed = True

    monkeypatch.setattr(discovery, "AsyncZeroconf", FakeAsyncZeroconf)
    monkeypatch.setattr(discovery, "AsyncServiceInfo", FailingInfo)

    with pytest.raises(RuntimeError, match="resolution failed"):
        asyncio.run(discovery._resolve_zeroconf(SERVICE_TYPE, f"TV.{SERVICE_TYPE}", 1.0))

    assert FakeAsyncZeroconf.instance.closed is True


def test_browse_zeroconf_closes_when_browser_creation_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeAsyncZeroconf:
        instance: "FakeAsyncZeroconf"

        def __init__(self) -> None:
            self.zeroconf = object()
            self.closed = False
            type(self).instance = self

        async def async_close(self) -> None:
            self.closed = True

    class FailingServiceBrowser:
        def __init__(self, zeroconf: object, service_type: str, handlers: list[object]) -> None:
            raise RuntimeError("browser creation failed")

    monkeypatch.setattr(discovery, "AsyncZeroconf", FakeAsyncZeroconf)
    monkeypatch.setattr(discovery, "AsyncServiceBrowser", FailingServiceBrowser)

    with pytest.raises(RuntimeError, match="browser creation failed"):
        asyncio.run(discovery._browse_zeroconf(SERVICE_TYPE, 0.001))

    assert FakeAsyncZeroconf.instance.closed is True


def test_browse_zeroconf_closes_when_browser_cancellation_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeAsyncZeroconf:
        instance: "FakeAsyncZeroconf"

        def __init__(self) -> None:
            self.zeroconf = object()
            self.closed = False
            type(self).instance = self

        async def async_close(self) -> None:
            self.closed = True

    class FailingServiceBrowser:
        def __init__(self, zeroconf: object, service_type: str, handlers: list[object]) -> None:
            return

        async def async_cancel(self) -> None:
            raise RuntimeError("browser cancellation failed")

    monkeypatch.setattr(discovery, "AsyncZeroconf", FakeAsyncZeroconf)
    monkeypatch.setattr(discovery, "AsyncServiceBrowser", FailingServiceBrowser)

    with pytest.raises(RuntimeError, match="browser cancellation failed"):
        asyncio.run(discovery._browse_zeroconf(SERVICE_TYPE, 0.001))

    assert FakeAsyncZeroconf.instance.closed is True


def test_resolve_service_without_saved_identifier_uses_exact_service_name() -> None:
    service_name = f"TV.{SERVICE_TYPE}"
    exact = record(service_name, "tv.local.", ("192.0.2.50",))

    async def resolve(service_type: str, name: str, timeout: float) -> DiscoveryRecord:
        assert (service_type, name, timeout) == (SERVICE_TYPE, service_name, 1.25)
        return exact

    async def browse(timeout: float) -> list[DiscoveredDevice]:
        pytest.fail("resolution without a saved identifier must not trigger a browse")

    result = asyncio.run(resolve_service(service_name, timeout=1.25, resolve=resolve, browse=browse))

    assert result == DiscoveredDevice(
        "TV",
        "192.0.2.50",
        service_name=service_name,
        service_target="tv.local.",
    )


def test_resolve_service_returns_exact_identifier_match_without_browsing() -> None:
    service_name = f"TV.{SERVICE_TYPE}"
    exact = record(service_name, "tv.local.", ("192.0.2.50",), identifier="AA:BB:CC:DD:EE:FF")

    async def resolve(service_type: str, name: str, timeout: float) -> DiscoveryRecord:
        assert (service_type, name, timeout) == (SERVICE_TYPE, service_name, 1.0)
        return exact

    async def browse(timeout: float) -> list[DiscoveredDevice]:
        pytest.fail("an exact identifier match must not trigger a browse")

    result = asyncio.run(
        resolve_service(service_name, "aa:bb:cc:dd:ee:ff", timeout=3.0, resolve=resolve, browse=browse)
    )

    assert result == DiscoveredDevice(
        "TV",
        "192.0.2.50",
        service_name=service_name,
        service_target="tv.local.",
        service_identifier="AA:BB:CC:DD:EE:FF",
    )


def test_resolve_service_follows_unique_identifier_after_service_rename() -> None:
    old_name = f"Old TV.{SERVICE_TYPE}"
    renamed = DiscoveredDevice(
        "New TV",
        "192.0.2.51",
        service_name=f"New TV.{SERVICE_TYPE}",
        service_target="new-tv.local.",
        service_identifier="AA:BB:CC:DD:EE:FF",
    )

    async def resolve(service_type: str, name: str, timeout: float) -> None:
        return None

    async def browse(timeout: float) -> list[DiscoveredDevice]:
        return [renamed]

    assert asyncio.run(resolve_service(old_name, "aa:bb:cc:dd:ee:ff", resolve=resolve, browse=browse)) == renamed


def test_resolve_service_rejects_identifier_mismatch() -> None:
    service_name = f"TV.{SERVICE_TYPE}"

    async def resolve(service_type: str, name: str, timeout: float) -> DiscoveryRecord:
        return record(service_name, "replacement.local.", ("192.0.2.52",), identifier="11:22:33:44:55:66")

    async def browse(timeout: float) -> list[DiscoveredDevice]:
        return []

    with pytest.raises(ServiceIdentityError, match="does not match"):
        asyncio.run(resolve_service(service_name, "AA:BB:CC:DD:EE:FF", resolve=resolve, browse=browse))


def test_resolve_service_never_falls_back_when_browse_fails_after_identifier_mismatch() -> None:
    service_name = f"TV.{SERVICE_TYPE}"

    async def resolve(service_type: str, name: str, timeout: float) -> DiscoveryRecord:
        return record(service_name, "replacement.local.", ("192.0.2.52",), identifier="11:22:33:44:55:66")

    async def browse(timeout: float) -> list[DiscoveredDevice]:
        raise OSError("browse unavailable")

    with pytest.raises(ServiceIdentityError, match="does not match"):
        asyncio.run(resolve_service(service_name, "AA:BB:CC:DD:EE:FF", resolve=resolve, browse=browse))


def test_resolve_service_preserves_outage_when_no_conflicting_service_was_observed() -> None:
    service_name = f"TV.{SERVICE_TYPE}"

    async def resolve(service_type: str, name: str, timeout: float) -> None:
        return None

    async def browse(timeout: float) -> list[DiscoveredDevice]:
        raise OSError("browse unavailable")

    with pytest.raises(OSError, match="browse unavailable"):
        asyncio.run(resolve_service(service_name, "AA:BB:CC:DD:EE:FF", resolve=resolve, browse=browse))


@pytest.mark.parametrize("advertised_identifier", [None, "11:22:33:44:55:66"])
def test_resolve_service_rejects_browsed_saved_name_with_missing_or_mismatched_identifier(
    advertised_identifier: str | None,
) -> None:
    service_name = f"TV.{SERVICE_TYPE}"

    async def resolve(service_type: str, name: str, timeout: float) -> None:
        return None

    async def browse(timeout: float) -> list[DiscoveredDevice]:
        return [
            DiscoveredDevice(
                "TV",
                "192.0.2.52",
                service_name=service_name,
                service_target="replacement.local.",
                service_identifier=advertised_identifier,
            )
        ]

    with pytest.raises(ServiceIdentityError, match="does not match"):
        asyncio.run(resolve_service(service_name, "AA:BB:CC:DD:EE:FF", resolve=resolve, browse=browse))


def test_resolve_service_returns_none_when_saved_identifier_is_missing() -> None:
    async def resolve(service_type: str, name: str, timeout: float) -> None:
        return None

    async def browse(timeout: float) -> list[DiscoveredDevice]:
        return []

    assert (
        asyncio.run(
            resolve_service(
                f"Missing TV.{SERVICE_TYPE}",
                "AA:BB:CC:DD:EE:FF",
                resolve=resolve,
                browse=browse,
            )
        )
        is None
    )


@pytest.mark.parametrize("timeout", [0, -1, True])
def test_discovery_rejects_invalid_timeout(timeout: object) -> None:
    with pytest.raises(ValueError, match="timeout"):
        asyncio.run(discover_devices(timeout))  # type: ignore[arg-type]
