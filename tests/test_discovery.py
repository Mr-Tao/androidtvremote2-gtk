import asyncio

import pytest

from androidtvremote2_gtk import discovery
from androidtvremote2_gtk.discovery import SERVICE_TYPE, DiscoveryRecord, discover_devices
from androidtvremote2_gtk.models import DiscoveredDevice


def test_discovery_dedupes_dual_stack_records_and_sorts_results() -> None:
    calls: list[tuple[str, float]] = []

    async def browse(service_type: str, timeout: float) -> list[DiscoveryRecord]:
        calls.append((service_type, timeout))
        return [
            DiscoveryRecord(f"Living\\032Room.{SERVICE_TYPE}", ("2001:db8::2",), 6466),
            DiscoveryRecord(f"bedroom.{SERVICE_TYPE}", ("2001:db8::1",), 6466),
            DiscoveryRecord(f"Living\\032Room.{SERVICE_TYPE}", ("192.0.2.5", "192.0.2.5"), 6466),
            DiscoveryRecord(f"Bedroom.{SERVICE_TYPE}", ("2001:db8::1",), 6466),
        ]

    result = asyncio.run(discover_devices(1.25, browse=browse))

    assert calls == [(SERVICE_TYPE, 1.25)]
    assert result == [
        DiscoveredDevice(name="Bedroom", host="2001:db8::1", port=6466),
        DiscoveredDevice(name="Living Room", host="192.0.2.5", port=6466),
    ]


def test_discovery_skips_invalid_records() -> None:
    async def browse(service_type: str, timeout: float) -> list[DiscoveryRecord]:
        return [
            DiscoveryRecord(f"valid.{service_type}", ("192.0.2.9",), 6466),
            DiscoveryRecord(f"invalid.{service_type}", ("not an address",), 6466),
            DiscoveryRecord(f"empty.{service_type}", (), 6466),
        ]

    assert asyncio.run(discover_devices(browse=browse)) == [DiscoveredDevice("valid", "192.0.2.9")]


def test_zeroconf_handler_accepts_current_keyword_callback_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeAsyncZeroconf:
        def __init__(self) -> None:
            self.zeroconf = object()

        async def async_close(self) -> None:
            return

    class FakeServiceInfo:
        def __init__(self, service_type: str, name: str) -> None:
            assert service_type == SERVICE_TYPE
            assert name == f"TV.{SERVICE_TYPE}"
            self.port = 6466

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

    assert records == [DiscoveryRecord(f"TV.{SERVICE_TYPE}", ("192.0.2.50",), 6466)]


@pytest.mark.parametrize("timeout", [0, -1, True])
def test_discovery_rejects_invalid_timeout(timeout: object) -> None:
    with pytest.raises(ValueError, match="timeout"):
        asyncio.run(discover_devices(timeout))  # type: ignore[arg-type]
