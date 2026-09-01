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
    assert device.service_name is None
    assert device.service_target is None
    assert device.service_identifier is None
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
    assert discovered.addresses == ("fe80::1%eth0",)
    assert state.device_id == "bedroom"
    with pytest.raises(FrozenInstanceError):
        state.status = ConnectionStatus.CONNECTED  # type: ignore[misc]


@pytest.mark.parametrize("model", [DeviceConfig, DiscoveredDevice])
def test_dns_sd_metadata_requires_complete_valid_service(model: object) -> None:
    base: dict[str, object]
    if model is DeviceConfig:
        base = {"id": "tv", "display_name": "TV", "host": "192.0.2.8"}
    else:
        base = {"name": "TV", "host": "192.0.2.8"}

    invalid_metadata = [
        {"service_name": "TV._androidtvremote2._tcp.local."},
        {"service_target": "tv.local."},
        {"service_name": "TV._other._tcp.local.", "service_target": "tv.local."},
        {"service_name": "._androidtvremote2._tcp.local.", "service_target": "tv.local."},
        {"service_name": "TV._androidtvremote2._tcp.local.", "service_target": "tv.local"},
        {"service_identifier": "AA:BB:CC:DD:EE:FF"},
    ]
    for metadata in invalid_metadata:
        with pytest.raises(ValueError):
            model(**base, **metadata)  # type: ignore[operator]


@pytest.mark.parametrize("model", [DeviceConfig, DiscoveredDevice])
def test_dns_sd_identifier_normalizes_mac_but_preserves_other_identifiers(model: object) -> None:
    base: dict[str, object]
    if model is DeviceConfig:
        base = {"id": "tv", "display_name": "TV", "host": "192.0.2.8"}
    else:
        base = {"name": "TV", "host": "192.0.2.8"}
    service = {
        "service_name": "TV._androidtvremote2._tcp.local.",
        "service_target": "tv.local.",
    }

    mac = model(**base, **service, service_identifier="aa:bb:cc:dd:ee:ff")  # type: ignore[operator]
    opaque = model(**base, **service, service_identifier="Tv-Identity")  # type: ignore[operator]

    assert mac.service_identifier == "AA:BB:CC:DD:EE:FF"
    assert opaque.service_identifier == "Tv-Identity"


@pytest.mark.parametrize("identifier", ["", " padded", "line\nbreak"])
def test_dns_sd_identifier_rejects_invalid_values(identifier: str) -> None:
    with pytest.raises(ValueError, match="service_identifier"):
        DeviceConfig(
            id="tv",
            display_name="TV",
            host="192.0.2.8",
            service_name="TV._androidtvremote2._tcp.local.",
            service_target="tv.local.",
            service_identifier=identifier,
        )


def test_discovered_device_validates_and_deduplicates_advertised_addresses() -> None:
    device = DiscoveredDevice(
        "TV",
        "192.0.2.8",
        addresses=("192.0.2.8", "2001:db8::8", "192.0.2.8"),
    )

    assert device.addresses == ("192.0.2.8", "2001:db8::8")
    with pytest.raises(ValueError, match="addresses must be a tuple"):
        DiscoveredDevice("TV", "192.0.2.8", addresses=["192.0.2.8"])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="host must be one"):
        DiscoveredDevice("TV", "192.0.2.8", addresses=("192.0.2.9",))
