from pathlib import Path
from types import SimpleNamespace

import pytest

from androidtvremote2_gtk import window
from androidtvremote2_gtk.window import AddDeviceDialog, _initial_identity_folder


def test_initial_identity_folder_prefers_expanded_current_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    current = home / "current"
    config_identity = tmp_path / "config" / "androidtvremote2"
    managed_root = tmp_path / "managed"
    for directory in (current, config_identity, managed_root, home):
        directory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))

    assert _initial_identity_folder("~/current", config_identity.parent, managed_root, home) == current


def test_initial_identity_folder_falls_back_to_config_identity(tmp_path: Path) -> None:
    config_identity = tmp_path / "config" / "androidtvremote2"
    managed_root = tmp_path / "managed"
    home = tmp_path / "home"
    for directory in (config_identity, managed_root, home):
        directory.mkdir(parents=True)

    assert (
        _initial_identity_folder(str(tmp_path / "missing"), config_identity.parent, managed_root, home)
        == config_identity
    )


def test_initial_identity_folder_falls_back_to_managed_root(tmp_path: Path) -> None:
    managed_root = tmp_path / "managed"
    home = tmp_path / "home"
    managed_root.mkdir()
    home.mkdir()

    assert _initial_identity_folder("", tmp_path / "config", managed_root, home) == managed_root


def test_initial_identity_folder_falls_back_to_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()

    assert _initial_identity_folder("", tmp_path / "config", tmp_path / "managed", home) == home


def test_identity_picker_uses_application_window_as_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    managed_root = tmp_path / "managed"
    home.mkdir()
    managed_root.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "missing-config"))
    captured: dict[str, object] = {}

    class FakeFileDialog:
        def __init__(self, *, title: str) -> None:
            captured["title"] = title

        def set_initial_folder(self, folder: object) -> None:
            captured["folder"] = folder

        def select_folder(self, parent: object, cancellable: object, callback: object) -> None:
            captured["parent"] = parent
            captured["cancellable"] = cancellable
            captured["callback"] = callback

    monkeypatch.setattr(window.Gtk, "FileDialog", FakeFileDialog)
    parent = SimpleNamespace(store=SimpleNamespace(managed_root=managed_root))
    callback = object()
    dialog = SimpleNamespace(
        _window=parent,
        _identity=SimpleNamespace(get_text=lambda: ""),
        _identity_selected=callback,
    )

    AddDeviceDialog._select_identity(dialog)

    assert captured["parent"] is parent
    assert captured["cancellable"] is None
    assert captured["callback"] is callback
    assert captured["folder"].get_path() == str(managed_root)  # type: ignore[union-attr]
