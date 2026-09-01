"""Immutable application models and their boundary validation."""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

_SLUG_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?\Z")
_HOST_LABEL_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")
_SERVICE_SUFFIX = "._androidtvremote2._tcp.local."
_MAC_IDENTIFIER_RE = re.compile(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\Z")


def _validate_name(value: str, field: str) -> None:
    if not isinstance(value, str) or value != value.strip() or not value or len(value) > 128:
        raise ValueError(f"{field} must be a non-empty, trimmed string of at most 128 characters")
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise ValueError(f"{field} must not contain control characters")


def _validate_host(host: str) -> None:
    if not isinstance(host, str) or host != host.strip() or not host or len(host) > 253:
        raise ValueError("host must be a non-empty, trimmed hostname or IP address")
    if any(character.isspace() for character in host) or any(character in host for character in "/[]"):
        raise ValueError("host must not contain whitespace, brackets, or a path")
    try:
        ipaddress.ip_address(host)
        return
    except ValueError:
        pass
    if ":" in host:
        raise ValueError("host must be an unbracketed IP address or hostname without a port")
    hostname = host[:-1] if host.endswith(".") else host
    if not hostname or any(not _HOST_LABEL_RE.fullmatch(label) for label in hostname.split(".")):
        raise ValueError("host is not a valid hostname or IP address")


def _validate_port(port: int, field: str) -> None:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError(f"{field} must be an integer between 1 and 65535")


def _validate_service_name(value: str) -> None:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not value
        or len(value) > 1024
        or not value.casefold().endswith(_SERVICE_SUFFIX)
        or not value[: -len(_SERVICE_SUFFIX)]
    ):
        raise ValueError("service_name must be a full Android TV Remote DNS-SD instance name")
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise ValueError("service_name must not contain control characters")


def _normalize_service_identifier(value: str) -> str:
    _validate_name(value, "service_identifier")
    return value.upper() if _MAC_IDENTIFIER_RE.fullmatch(value) else value


class ConnectionStatus(str, Enum):
    """Controller connection lifecycle states."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    PAIRING = "pairing"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    AUTH_REQUIRED = "auth-required"
    FAILED = "failed"
    SHUTDOWN = "shutdown"


@dataclass(frozen=True, slots=True)
class DeviceConfig:
    """Persisted configuration for one television and pairing identity."""

    id: str
    display_name: str
    host: str
    api_port: int = 6466
    pair_port: int = 6467
    enable_ime: bool = True
    paired: bool = False
    credential_directory: Path | None = None
    service_name: str | None = None
    service_target: str | None = None
    service_identifier: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not _SLUG_RE.fullmatch(self.id):
            raise ValueError("id must be a lowercase ASCII slug of at most 64 characters")
        _validate_name(self.display_name, "display_name")
        _validate_host(self.host)
        _validate_port(self.api_port, "api_port")
        _validate_port(self.pair_port, "pair_port")
        if not isinstance(self.enable_ime, bool):
            raise ValueError("enable_ime must be a boolean")
        if not isinstance(self.paired, bool):
            raise ValueError("paired must be a boolean")
        if (self.service_name is None) != (self.service_target is None):
            raise ValueError("service_name and service_target must be provided together")
        if self.service_name is not None:
            _validate_service_name(self.service_name)
            _validate_host(self.service_target)  # type: ignore[arg-type]
            if not self.service_target.endswith("."):  # type: ignore[union-attr]
                raise ValueError("service_target must be a fully qualified hostname")
        if self.service_identifier is not None:
            if self.service_name is None:
                raise ValueError("service_identifier requires a DNS-SD service")
            object.__setattr__(
                self,
                "service_identifier",
                _normalize_service_identifier(self.service_identifier),
            )
        if self.credential_directory is not None:
            path = Path(self.credential_directory)
            if "\x00" in str(path) or not path.is_absolute() or ".." in path.parts:
                raise ValueError("credential_directory must be an absolute path without traversal")
            if not self.paired:
                raise ValueError("an external credential_directory requires paired=True")
            object.__setattr__(self, "credential_directory", path)


@dataclass(frozen=True, slots=True)
class DiscoveredDevice:
    """A deterministic endpoint found through Android TV Remote mDNS."""

    name: str
    host: str
    port: int = 6466
    service_name: str | None = None
    service_target: str | None = None
    service_identifier: str | None = None
    addresses: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_name(self.name, "name")
        _validate_host(self.host)
        _validate_port(self.port, "port")
        if (self.service_name is None) != (self.service_target is None):
            raise ValueError("service_name and service_target must be provided together")
        if self.service_name is not None:
            _validate_service_name(self.service_name)
            _validate_host(self.service_target)  # type: ignore[arg-type]
            if not self.service_target.endswith("."):  # type: ignore[union-attr]
                raise ValueError("service_target must be a fully qualified hostname")
        if self.service_identifier is not None:
            if self.service_name is None:
                raise ValueError("service_identifier requires a DNS-SD service")
            object.__setattr__(
                self,
                "service_identifier",
                _normalize_service_identifier(self.service_identifier),
            )
        if not isinstance(self.addresses, tuple):
            raise ValueError("addresses must be a tuple")
        addresses = self.addresses or (self.host,)
        for address in addresses:
            _validate_host(address)
        addresses = tuple(dict.fromkeys(addresses))
        if self.host not in addresses:
            raise ValueError("host must be one of the advertised addresses")
        object.__setattr__(self, "addresses", addresses)

    @property
    def display_name(self) -> str:
        """Return the discovery name using the configuration naming vocabulary."""
        return self.name

    @property
    def api_port(self) -> int:
        """Return the advertised remote API port."""
        return self.port


@dataclass(frozen=True, slots=True)
class RemoteState:
    """Immutable snapshot delivered from the protocol worker to the UI."""

    status: ConnectionStatus = ConnectionStatus.DISCONNECTED
    device: DeviceConfig | None = None
    manufacturer: str | None = None
    model: str | None = None
    software_version: str | None = None
    is_on: bool | None = None
    volume_level: int | None = None
    volume_max: int | None = None
    is_muted: bool | None = None
    current_app: str | None = None
    error: str | None = None

    @property
    def device_id(self) -> str | None:
        """Return the active device ID, if any."""
        return self.device.id if self.device else None
