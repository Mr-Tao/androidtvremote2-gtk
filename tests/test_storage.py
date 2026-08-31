import json
import stat
from pathlib import Path

import pytest

from androidtvremote2_gtk.models import DeviceConfig
from androidtvremote2_gtk.storage import DeviceStore, DeviceStoreError


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_default_roots_use_separate_xdg_locations(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config_home = tmp_path / "config"
    data_home = tmp_path / "data"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))

    store = DeviceStore()

    assert store.path == config_home / "androidtvremote2-gtk" / "devices.json"
    assert store.managed_root == data_home / "androidtvremote2-gtk" / "devices"


def test_atomic_roundtrip_is_deterministic_and_private(tmp_path: Path) -> None:
    store = DeviceStore(tmp_path / "config", tmp_path / "data")
    bedroom = DeviceConfig(id="bedroom", display_name="Bedroom", host="192.0.2.2")
    living = DeviceConfig(id="living-room", display_name="Living Room", host="tv.local", paired=True)

    store.save([living, bedroom])
    first_content = store.path.read_bytes()
    store.save(store.load())

    assert store.load() == [bedroom, living]
    assert store.path.read_bytes() == first_content
    assert mode(store.config_root) == 0o700
    assert mode(store.data_root) == 0o700
    assert mode(store.managed_root) == 0o700
    assert mode(store.path) == 0o600
    assert not list(store.config_root.glob(".devices.*.tmp"))


def test_external_credentials_are_serialized_as_a_reference(tmp_path: Path) -> None:
    external = tmp_path / "existing-identity"
    external.mkdir(mode=0o755)
    device = DeviceConfig(
        id="office",
        display_name="Office",
        host="2001:db8::4",
        paired=True,
        credential_directory=external,
    )
    store = DeviceStore(tmp_path / "config", tmp_path / "data")

    store.save([device])

    assert store.load() == [device]
    assert store.credential_directory(device) == external
    assert mode(external) == 0o755
    assert not (store.managed_root / device.id).exists()


def test_secure_credentials_sets_managed_permissions(tmp_path: Path) -> None:
    store = DeviceStore(tmp_path / "config", tmp_path / "data")
    device = DeviceConfig(id="tv", display_name="TV", host="tv.local")
    cert_path, key_path = store.credential_paths(device, create=True)
    cert_path.write_text("test certificate", encoding="utf-8")
    key_path.write_text("test private key", encoding="utf-8")
    cert_path.chmod(0o644)
    key_path.chmod(0o644)

    store.secure_credentials(device)

    assert mode(cert_path.parent) == 0o700
    assert mode(cert_path) == 0o600
    assert mode(key_path) == 0o600


def test_existing_managed_credentials_are_hardened_without_replacement(tmp_path: Path) -> None:
    store = DeviceStore(tmp_path / "config", tmp_path / "data")
    device = DeviceConfig(id="tv", display_name="TV", host="tv.local", paired=True)
    cert_path, key_path = store.credential_paths(device, create=True)
    cert_path.write_text("certificate", encoding="utf-8")
    key_path.write_text("key", encoding="utf-8")
    cert_path.parent.chmod(0o755)
    cert_path.chmod(0o644)
    key_path.chmod(0o644)

    assert store.credentials_available(device) is True
    assert mode(cert_path.parent) == 0o700
    assert mode(cert_path) == 0o600
    assert mode(key_path) == 0o600


def test_remove_retains_managed_credentials(tmp_path: Path) -> None:
    store = DeviceStore(tmp_path / "config", tmp_path / "data")
    device = DeviceConfig(id="tv", display_name="TV", host="tv.local")
    cert_path, key_path = store.credential_paths(device, create=True)
    cert_path.write_text("certificate", encoding="utf-8")
    key_path.write_text("key", encoding="utf-8")
    store.upsert(device)

    assert store.remove("tv") is True
    assert store.load() == []
    assert cert_path.exists()
    assert key_path.exists()
    assert store.remove("tv") is False


def test_reset_pairing_removes_managed_identity_and_preserves_device(tmp_path: Path) -> None:
    store = DeviceStore(tmp_path / "config", tmp_path / "data")
    device = DeviceConfig(id="tv", display_name="TV", host="tv.local", paired=True)
    other = DeviceConfig(id="other", display_name="Other", host="other.local", paired=True)
    cert_path, key_path = store.credential_paths(device, create=True)
    cert_path.write_text("certificate", encoding="utf-8")
    key_path.write_text("key", encoding="utf-8")
    store.save([device, other])

    reset = store.reset_pairing("tv")

    assert reset == DeviceConfig(id="tv", display_name="TV", host="tv.local")
    assert store.load() == [other, reset]
    assert not cert_path.parent.exists()
    assert not list(store.managed_root.glob(".tv.forgotten-*"))


def test_reset_pairing_drops_external_reference_without_deleting_files(tmp_path: Path) -> None:
    store = DeviceStore(tmp_path / "config", tmp_path / "data")
    external = tmp_path / "external"
    external.mkdir()
    cert_path = external / "cert.pem"
    key_path = external / "key.pem"
    cert_path.write_text("certificate", encoding="utf-8")
    key_path.write_text("key", encoding="utf-8")
    device = DeviceConfig(
        id="tv",
        display_name="TV",
        host="tv.local",
        paired=True,
        credential_directory=external,
    )
    store.upsert(device)

    reset = store.reset_pairing("tv")

    assert reset.credential_directory is None
    assert reset.paired is False
    assert cert_path.read_text(encoding="utf-8") == "certificate"
    assert key_path.read_text(encoding="utf-8") == "key"


def test_reset_pairing_restores_managed_identity_when_save_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = DeviceStore(tmp_path / "config", tmp_path / "data")
    device = DeviceConfig(id="tv", display_name="TV", host="tv.local", paired=True)
    cert_path, key_path = store.credential_paths(device, create=True)
    cert_path.write_text("certificate", encoding="utf-8")
    key_path.write_text("key", encoding="utf-8")
    store.upsert(device)

    def fail_save(_: object) -> None:
        raise DeviceStoreError("simulated save failure")

    monkeypatch.setattr(store, "save", fail_save)
    with pytest.raises(DeviceStoreError, match="simulated save failure"):
        store.reset_pairing("tv")

    assert cert_path.read_text(encoding="utf-8") == "certificate"
    assert key_path.read_text(encoding="utf-8") == "key"
    assert not list(store.managed_root.glob(".tv.forgotten-*"))


def test_reset_pairing_rejects_symlinked_managed_identity(tmp_path: Path) -> None:
    store = DeviceStore(tmp_path / "config", tmp_path / "data")
    device = DeviceConfig(id="tv", display_name="TV", host="tv.local", paired=True)
    external = tmp_path / "external"
    external.mkdir()
    marker = external / "marker"
    marker.write_text("untouched", encoding="utf-8")
    (store.managed_root / device.id).symlink_to(external, target_is_directory=True)
    store.upsert(device)

    with pytest.raises(DeviceStoreError, match="unsafe"):
        store.reset_pairing("tv")

    assert store.load() == [device]
    assert marker.read_text(encoding="utf-8") == "untouched"


def test_failed_replace_preserves_previous_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    store = DeviceStore(tmp_path / "config", tmp_path / "data")
    original = DeviceConfig(id="tv", display_name="TV", host="tv.local")
    store.save([original])
    original_bytes = store.path.read_bytes()

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr("androidtvremote2_gtk.storage.os.replace", fail_replace)
    with pytest.raises(DeviceStoreError, match="Unable to save"):
        store.save([DeviceConfig(id="other", display_name="Other", host="other.local")])

    assert store.path.read_bytes() == original_bytes
    assert not list(store.config_root.glob(".devices.*.tmp"))


@pytest.mark.parametrize(
    "contents",
    [
        "not json",
        json.dumps({"version": 99, "devices": []}),
        json.dumps({"version": 1, "devices": [{"id": "../tv", "display_name": "TV", "host": "tv.local"}]}),
        json.dumps(
            {"version": 1, "devices": [{"id": "tv", "display_name": "TV", "host": "tv.local", "key": "secret"}]}
        ),
    ],
)
def test_load_rejects_malformed_data_without_echoing_contents(tmp_path: Path, contents: str) -> None:
    store = DeviceStore(tmp_path / "config", tmp_path / "data")
    store.path.write_text(contents, encoding="utf-8")

    with pytest.raises(DeviceStoreError) as caught:
        store.load()

    assert contents not in str(caught.value)


def test_remove_rejects_path_traversal(tmp_path: Path) -> None:
    store = DeviceStore(tmp_path / "config", tmp_path / "data")
    with pytest.raises(ValueError, match="device_id"):
        store.remove("../tv")
