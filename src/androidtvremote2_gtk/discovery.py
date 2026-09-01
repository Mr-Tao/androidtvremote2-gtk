"""Asynchronous Android TV Remote service discovery and resolution."""

from __future__ import annotations

import asyncio
import ipaddress
import math
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from zeroconf import ServiceStateChange, Zeroconf
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

from .models import DiscoveredDevice

SERVICE_TYPE = "_androidtvremote2._tcp.local."
_ESCAPED_CHARACTER_RE = re.compile(r"\\(\d{3})")


class ServiceIdentityError(RuntimeError):
    """Raised when a saved DNS-SD identity conflicts with current advertising."""


@dataclass(frozen=True, slots=True)
class DiscoveryRecord:
    """Resolved zeroconf record used by the deterministic reduction layer."""

    service_name: str
    service_target: str
    addresses: tuple[str, ...]
    port: int
    service_identifier: str | None = None


BrowseFunction = Callable[[str, float], Awaitable[Iterable[DiscoveryRecord]]]
ResolveFunction = Callable[[str, str, float], Awaitable[DiscoveryRecord | None]]
DeviceBrowseFunction = Callable[[float], Awaitable[list[DiscoveredDevice]]]


def _validate_timeout(timeout: float) -> float:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a positive number")
    return float(timeout)


def _txt_identifier(properties: Mapping[bytes, bytes | None] | None) -> str | None:
    if not properties:
        return None
    raw = properties.get(b"bt")
    if raw is None:
        return None
    try:
        value = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    return value or None


def _record_from_info(info: Any) -> DiscoveryRecord:
    return DiscoveryRecord(
        service_name=info.name,
        service_target=info.server,
        addresses=tuple(info.parsed_scoped_addresses()),
        port=info.port,
        service_identifier=_txt_identifier(info.properties),
    )


async def _browse_zeroconf(service_type: str, timeout: float) -> list[DiscoveryRecord]:
    zeroconf = AsyncZeroconf()
    records: list[DiscoveryRecord] = []
    requests: set[asyncio.Task[None]] = set()

    async def resolve(zeroconf: Zeroconf, service_type: str, name: str) -> None:
        info = AsyncServiceInfo(service_type, name)
        request_timeout = max(100, int(timeout * 1000))
        if await info.async_request(zeroconf, request_timeout):
            records.append(_record_from_info(info))

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

    browser: AsyncServiceBrowser | None = None
    try:
        browser = AsyncServiceBrowser(zeroconf.zeroconf, service_type, handlers=[on_state_change])
        await asyncio.sleep(timeout)
    finally:
        try:
            if browser is not None:
                await browser.async_cancel()
        finally:
            try:
                if requests:
                    done, pending = await asyncio.wait(requests, timeout=0)
                    for task in pending:
                        task.cancel()
                    if done or pending:
                        await asyncio.gather(*done, *pending, return_exceptions=True)
            finally:
                await zeroconf.async_close()
    return records


async def _resolve_zeroconf(service_type: str, service_name: str, timeout: float) -> DiscoveryRecord | None:
    zeroconf = AsyncZeroconf()
    try:
        info = AsyncServiceInfo(service_type, service_name)
        resolved = await info.async_request(zeroconf.zeroconf, max(100, int(timeout * 1000)))
        return _record_from_info(info) if resolved else None
    finally:
        await zeroconf.async_close()


def _display_name(service_name: str) -> str:
    suffix_position = service_name.casefold().find(f".{SERVICE_TYPE}".casefold())
    name = service_name[:suffix_position] if suffix_position >= 0 else service_name.rstrip(".")
    return _ESCAPED_CHARACTER_RE.sub(lambda match: chr(int(match.group(1))), name)


def _address_key(address: str) -> tuple[int, str]:
    parsed = ipaddress.ip_address(address)
    return (0 if parsed.version == 4 else 1, str(parsed))


def _device_from_record(record: DiscoveryRecord) -> DiscoveredDevice | None:
    try:
        addresses = tuple(sorted(set(record.addresses), key=_address_key))
        if not addresses:
            return None
        return DiscoveredDevice(
            name=_display_name(record.service_name),
            host=addresses[0],
            port=record.port,
            service_name=record.service_name,
            service_target=record.service_target,
            service_identifier=record.service_identifier,
            addresses=addresses,
        )
    except (TypeError, ValueError):
        return None


async def discover_devices(timeout: float = 3.0, *, browse: BrowseFunction | None = None) -> list[DiscoveredDevice]:
    """Discover the latest deterministic endpoint for each DNS-SD service."""
    timeout = _validate_timeout(timeout)
    records = await (browse or _browse_zeroconf)(SERVICE_TYPE, timeout)
    latest: dict[str, DiscoveryRecord] = {}
    for record in records:
        latest[record.service_name.casefold()] = record

    devices = [device for record in latest.values() if (device := _device_from_record(record)) is not None]
    return sorted(
        devices,
        key=lambda device: (device.name.casefold(), device.service_name.casefold(), _address_key(device.host)),
    )


async def resolve_service(
    service_name: str,
    service_identifier: str | None = None,
    timeout: float = 3.0,
    *,
    resolve: ResolveFunction | None = None,
    browse: DeviceBrowseFunction | None = None,
) -> DiscoveredDevice | None:
    """Resolve a saved service, using its TXT identity if its PTR name changed."""
    timeout = _validate_timeout(timeout)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    direct_timeout = timeout if service_identifier is None else min(1.0, max(0.1, timeout / 2))
    record = await (resolve or _resolve_zeroconf)(SERVICE_TYPE, service_name, direct_timeout)
    direct = _device_from_record(record) if record is not None else None
    if service_identifier is None:
        return direct

    expected = service_identifier.casefold()
    identity_conflict = record is not None and (
        record.service_identifier is None or record.service_identifier.casefold() != expected
    )
    if (
        direct is not None
        and direct.service_identifier is not None
        and direct.service_identifier.casefold() == expected
    ):
        return direct

    remaining = deadline - loop.time()
    matches: list[DiscoveredDevice] = []
    browse_identity_conflict = False
    if remaining > 0:
        try:
            candidates = await (browse or discover_devices)(remaining)
        except (OSError, asyncio.TimeoutError) as exc:
            if identity_conflict:
                raise ServiceIdentityError("The advertised DNS-SD identity does not match the saved device") from exc
            raise
        matches = [
            candidate
            for candidate in candidates
            if candidate.service_identifier is not None and candidate.service_identifier.casefold() == expected
        ]
        browse_identity_conflict = any(
            candidate.service_name is not None
            and candidate.service_name.casefold() == service_name.casefold()
            and (candidate.service_identifier is None or candidate.service_identifier.casefold() != expected)
            for candidate in candidates
        )
    if len(matches) == 1:
        return matches[0]
    if identity_conflict or browse_identity_conflict or len(matches) > 1:
        raise ServiceIdentityError("The advertised DNS-SD identity does not match the saved device")
    return None
