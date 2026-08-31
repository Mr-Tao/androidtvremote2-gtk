"""Secure persistence for device metadata and managed pairing identities."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import stat
import tempfile
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .models import DeviceConfig

_STORE_VERSION = 1
_LOGGER = logging.getLogger(__name__)
_SLUG_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?\Z")
_DEVICE_KEYS = {
    "id",
    "display_name",
    "host",
    "api_port",
    "pair_port",
    "enable_ime",
    "paired",
    "credential_directory",
}


class DeviceStoreError(RuntimeError):
    """Raised when persisted metadata or managed credentials are unsafe."""


def _default_config_root() -> Path:
    configured = os.environ.get("XDG_CONFIG_HOME")
    base = Path(configured) if configured else Path.home() / ".config"
    return base / "androidtvremote2-gtk"


def _default_data_root() -> Path:
    configured = os.environ.get("XDG_DATA_HOME")
    base = Path(configured) if configured else Path.home() / ".local" / "share"
    return base / "androidtvremote2-gtk"


class DeviceStore:
    """Store versioned device metadata separately from managed identities."""

    def __init__(self, config_root: Path | None = None, data_root: Path | None = None) -> None:
        self.config_root = Path(config_root) if config_root is not None else _default_config_root()
        self.data_root = Path(data_root) if data_root is not None else _default_data_root()
        self.path = self.config_root / "devices.json"
        self.managed_root = self.data_root / "devices"
        self._ensure_private_directory(self.config_root)
        self._ensure_private_directory(self.data_root)
        self._ensure_private_directory(self.managed_root)

    @staticmethod
    def _ensure_private_directory(path: Path) -> None:
        try:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.chmod(0o700)
        except OSError as exc:
            raise DeviceStoreError("Unable to prepare private application storage") from exc

    @staticmethod
    def _validate_id(device_id: str) -> None:
        if not isinstance(device_id, str) or not _SLUG_RE.fullmatch(device_id):
            raise ValueError("device_id must be a lowercase ASCII slug")

    def credential_directory(self, device: DeviceConfig, *, create: bool = False) -> Path:
        """Return the referenced external or managed identity directory."""
        if device.credential_directory is not None:
            return device.credential_directory
        directory = self.managed_root / device.id
        if create:
            self._ensure_private_directory(directory)
        return directory

    def credential_paths(self, device: DeviceConfig, *, create: bool = False) -> tuple[Path, Path]:
        """Return certificate and key paths for a device identity."""
        directory = self.credential_directory(device, create=create)
        return directory / "cert.pem", directory / "key.pem"

    def credentials_available(self, device: DeviceConfig) -> bool:
        """Return whether both referenced credential files are regular files."""
        cert_path, key_path = self.credential_paths(device)
        if device.credential_directory is not None:
            return cert_path.is_file() and key_path.is_file()
        directory = cert_path.parent
        if not directory.is_dir() or directory.is_symlink():
            return False
        try:
            directory.chmod(0o700)
            for path in (cert_path, key_path):
                self._secure_managed_file(path)
        except DeviceStoreError:
            return False
        return True

    @staticmethod
    def _secure_managed_file(path: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise DeviceStoreError("Managed credential is not a regular file")
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)
        except DeviceStoreError:
            raise
        except OSError as exc:
            raise DeviceStoreError("Managed credentials are missing or unsafe") from exc

    def secure_credentials(self, device: DeviceConfig) -> None:
        """Restrict newly generated managed certificate and key files to mode 0600."""
        if device.credential_directory is not None:
            raise DeviceStoreError("External credentials are not managed by this application")
        directory = self.credential_directory(device, create=True)
        if directory.is_symlink():
            raise DeviceStoreError("Managed credential directory is unsafe")
        self._ensure_private_directory(directory)
        for path in self.credential_paths(device):
            self._secure_managed_file(path)

    def load(self) -> list[DeviceConfig]:
        """Load and validate devices, returning them in stable ID order."""
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            devices = self._decode(raw)
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
            raise DeviceStoreError("Device metadata is malformed or schema-invalid") from exc
        return sorted(devices, key=lambda device: device.id)

    @staticmethod
    def _decode(raw: Any) -> list[DeviceConfig]:
        if not isinstance(raw, dict) or set(raw) != {"version", "devices"}:
            raise ValueError("invalid root schema")
        if type(raw["version"]) is not int or raw["version"] != _STORE_VERSION:
            raise ValueError("unsupported store version")
        if not isinstance(raw["devices"], list):
            raise ValueError("devices must be a list")
        devices: list[DeviceConfig] = []
        seen: set[str] = set()
        for item in raw["devices"]:
            if not isinstance(item, dict) or not {"id", "display_name", "host"} <= set(item) <= _DEVICE_KEYS:
                raise ValueError("invalid device schema")
            credential_directory = item.get("credential_directory")
            if credential_directory is not None and not isinstance(credential_directory, str):
                raise ValueError("invalid credential directory")
            device = DeviceConfig(
                id=item["id"],
                display_name=item["display_name"],
                host=item["host"],
                api_port=item.get("api_port", 6466),
                pair_port=item.get("pair_port", 6467),
                enable_ime=item.get("enable_ime", True),
                paired=item.get("paired", False),
                credential_directory=(Path(credential_directory) if credential_directory is not None else None),
            )
            if device.id in seen:
                raise ValueError("duplicate device id")
            seen.add(device.id)
            devices.append(device)
        return devices

    @staticmethod
    def _encode(device: DeviceConfig) -> dict[str, object]:
        return {
            "api_port": device.api_port,
            "credential_directory": str(device.credential_directory) if device.credential_directory else None,
            "display_name": device.display_name,
            "enable_ime": device.enable_ime,
            "host": device.host,
            "id": device.id,
            "pair_port": device.pair_port,
            "paired": device.paired,
        }

    def save(self, devices: Iterable[DeviceConfig]) -> None:
        """Atomically replace the metadata file with deterministic JSON."""
        ordered = sorted(devices, key=lambda device: device.id)
        if len({device.id for device in ordered}) != len(ordered):
            raise ValueError("device IDs must be unique")
        payload = {"devices": [self._encode(device) for device in ordered], "version": _STORE_VERSION}
        content = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        descriptor = -1
        temporary_path: Path | None = None
        try:
            descriptor, name = tempfile.mkstemp(prefix=".devices.", suffix=".tmp", dir=self.config_root)
            temporary_path = Path(name)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                descriptor = -1
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self.path)
            temporary_path = None
            self.path.chmod(0o600)
            directory_descriptor = os.open(self.config_root, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except OSError as exc:
            raise DeviceStoreError("Unable to save device metadata") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def upsert(self, device: DeviceConfig) -> None:
        """Insert or replace one device while preserving stable ordering."""
        devices = {existing.id: existing for existing in self.load()}
        devices[device.id] = device
        self.save(devices.values())

    def remove(self, device_id: str) -> bool:
        """Remove only device metadata, retaining all credential files."""
        self._validate_id(device_id)
        devices = self.load()
        retained = [device for device in devices if device.id != device_id]
        if len(retained) == len(devices):
            return False
        self.save(retained)
        return True

    def reset_pairing(self, device_id: str) -> DeviceConfig:
        """Forget a saved pairing identity after an explicit user action.

        External credentials are never modified. A managed identity is first
        moved aside so a metadata write failure can restore it without loss.
        """
        self._validate_id(device_id)
        devices = self.load()
        try:
            device = next(existing for existing in devices if existing.id == device_id)
        except StopIteration as exc:
            raise DeviceStoreError("The selected device is not saved") from exc

        reset_device = DeviceConfig(
            id=device.id,
            display_name=device.display_name,
            host=device.host,
            api_port=device.api_port,
            pair_port=device.pair_port,
            enable_ime=device.enable_ime,
        )
        replacement = [reset_device if existing.id == device_id else existing for existing in devices]

        quarantined: Path | None = None
        if device.credential_directory is None:
            directory = self.managed_root / device.id
            try:
                directory_stat = directory.lstat()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise DeviceStoreError("Unable to inspect the managed pairing identity") from exc
            else:
                if not stat.S_ISDIR(directory_stat.st_mode) or stat.S_ISLNK(directory_stat.st_mode):
                    raise DeviceStoreError("Managed credential directory is unsafe")
                quarantined = self.managed_root / f".{device.id}.forgotten-{uuid.uuid4().hex}"
                try:
                    directory.rename(quarantined)
                except OSError as exc:
                    raise DeviceStoreError("Unable to retire the managed pairing identity") from exc

        try:
            self.save(replacement)
        except Exception:
            if quarantined is not None:
                try:
                    quarantined.rename(self.managed_root / device.id)
                except OSError as rollback_exc:
                    raise DeviceStoreError("Unable to restore the managed pairing identity") from rollback_exc
            raise

        if quarantined is not None:
            try:
                shutil.rmtree(quarantined)
            except OSError:
                _LOGGER.warning("The retired managed pairing identity could not be removed", exc_info=True)
        return reset_device
