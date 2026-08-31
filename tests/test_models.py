from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from androidtvremote2_gtk.models import ConnectionStatus, DeviceConfig, DiscoveredDevice, RemoteState


def test_device_config_defaults_and_immutability() -> None:
    device = DeviceConfig(id="living-room", display_name="Living Room", host="tv.local")

    assert device.api_port == 6466
    assert device.pair_port == 6467
    assert device.enable_ime is True
    assert device.paired is False
    with pytest.raises(FrozenInstanceError):
        device.host = "other.local"  # type: ignore[misc]


@pytest.mark.parametrize("device_id", ["", "Living-Room", "../tv", "tv/bedroom", "-tv", "tv-"])
def test_device_config_rejects_unsafe_ids(device_id: str) -> None:
    with pytest.raises(ValueError, match="id"):
        DeviceConfig(id=device_id, display_name="TV", host="tv.local")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("display_name", "  "),
        ("display_name", "TV\n"),
        ("host", "https://tv.local"),
        ("host", "tv.local:6466"),
        ("host", "bad host"),
        ("api_port", 0),
        ("pair_port", 65536),
        ("enable_ime", 1),
        ("paired", 1),
    ],
)
def test_device_config_validates_fields(field: str, value: object) -> None:
    values: dict[str, object] = {"id": "tv", "display_name": "TV", "host": "192.0.2.8"}
    values[field] = value
    with pytest.raises(ValueError):
        DeviceConfig(**values)  # type: ignore[arg-type]


def test_external_credentials_require_safe_absolute_path_and_paired_state() -> None:
    with pytest.raises(ValueError, match="absolute path"):
        DeviceConfig(
            id="tv",
            display_name="TV",
            host="2001:db8::1",
            paired=True,
            credential_directory=Path("../identity"),
        )
    with pytest.raises(ValueError, match="paired=True"):
        DeviceConfig(
            id="tv",
            display_name="TV",
            host="2001:db8::1",
            credential_directory=Path("/srv/identities/tv"),
        )

    device = DeviceConfig(
        id="tv",
        display_name="TV",
        host="2001:db8::1",
        paired=True,
        credential_directory=Path("/srv/identities/tv"),
    )
    assert device.credential_directory == Path("/srv/identities/tv")


def test_discovered_device_and_remote_state_are_immutable() -> None:
    discovered = DiscoveredDevice("Bedroom", "fe80::1%eth0")
    configured = DeviceConfig(id="bedroom", display_name="Bedroom", host=discovered.host)
    state = RemoteState(status=ConnectionStatus.CONNECTING, device=configured)

    assert discovered.api_port == 6466
    assert state.device_id == "bedroom"
    with pytest.raises(FrozenInstanceError):
        state.status = ConnectionStatus.CONNECTED  # type: ignore[misc]
