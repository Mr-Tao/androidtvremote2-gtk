"""Asynchronous Android TV Remote service discovery."""

from __future__ import annotations

import asyncio
import ipaddress
import math
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

from zeroconf import ServiceStateChange, Zeroconf
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

from .models import DiscoveredDevice

SERVICE_TYPE = "_androidtvremote2._tcp.local."
_ESCAPED_CHARACTER_RE = re.compile(r"\\(\d{3})")


@dataclass(frozen=True, slots=True)
class DiscoveryRecord:
    """Resolved zeroconf record used by the deterministic reduction layer."""

    service_name: str
    addresses: tuple[str, ...]
    port: int


BrowseFunction = Callable[[str, float], Awaitable[Iterable[DiscoveryRecord]]]


async def _browse_zeroconf(service_type: str, timeout: float) -> list[DiscoveryRecord]:
    zeroconf = AsyncZeroconf()
    records: list[DiscoveryRecord] = []
    requests: set[asyncio.Task[None]] = set()

    async def resolve(zeroconf: Zeroconf, service_type: str, name: str) -> None:
        info = AsyncServiceInfo(service_type, name)
        request_timeout = max(100, int(timeout * 1000))
        if await info.async_request(zeroconf, request_timeout):
            records.append(DiscoveryRecord(name, tuple(info.parsed_scoped_addresses()), info.port))

    def on_state_change(
        zeroconf: Zeroconf,
        service_type: str,
        name: str,
        state_change: ServiceStateChange,
    ) -> None:
        if state_change not in {ServiceStateChange.Added, ServiceStateChange.Updated}:
            return
        task = asyncio.create_task(resolve(zeroconf, service_type, name))
        requests.add(task)

        def request_done(done: asyncio.Task[None]) -> None:
            requests.discard(done)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(request_done)

    browser = AsyncServiceBrowser(zeroconf.zeroconf, service_type, handlers=[on_state_change])
    try:
        await asyncio.sleep(timeout)
    finally:
        await browser.async_cancel()
        if requests:
            done, pending = await asyncio.wait(requests, timeout=0)
            for task in pending:
                task.cancel()
            if done or pending:
                await asyncio.gather(*done, *pending, return_exceptions=True)
        await zeroconf.async_close()
    return records


def _display_name(service_name: str) -> str:
    suffix_position = service_name.casefold().find(f".{SERVICE_TYPE}".casefold())
    name = service_name[:suffix_position] if suffix_position >= 0 else service_name.rstrip(".")
    return _ESCAPED_CHARACTER_RE.sub(lambda match: chr(int(match.group(1))), name)


def _address_key(address: str) -> tuple[int, str]:
    parsed = ipaddress.ip_address(address)
    return (0 if parsed.version == 4 else 1, str(parsed))


async def discover_devices(timeout: float = 3.0, *, browse: BrowseFunction | None = None) -> list[DiscoveredDevice]:
    """Discover one deterministic preferred endpoint per mDNS service."""
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a positive number")
    records = await (browse or _browse_zeroconf)(SERVICE_TYPE, float(timeout))
    grouped: dict[str, tuple[str, set[str], set[int]]] = {}
    for record in records:
        display_name = _display_name(record.service_name)
        identity = display_name.casefold()
        if not display_name or not record.addresses:
            continue
        name, addresses, ports = grouped.setdefault(identity, (display_name, set(), set()))
        addresses.update(record.addresses)
        ports.add(record.port)
        if (display_name.casefold(), display_name) < (name.casefold(), name):
            grouped[identity] = (display_name, addresses, ports)

    devices: list[DiscoveredDevice] = []
    for name, addresses, ports in grouped.values():
        try:
            host = min(addresses, key=_address_key)
            port = min(ports)
            devices.append(DiscoveredDevice(name=name, host=host, port=port))
        except (ValueError, TypeError):
            continue
    return sorted(devices, key=lambda device: (device.name.casefold(), _address_key(device.host), device.port))
