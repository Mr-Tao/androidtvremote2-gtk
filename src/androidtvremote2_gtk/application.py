"""Application lifecycle and GTK-to-controller ownership boundary."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from .controller import RemoteController
from .demo import DemoController
from .models import DiscoveredDevice, RemoteState
from .storage import DeviceStore, DeviceStoreError
from .window import RemoteWindow

APP_ID = "io.github.mrtao.androidtvremote2"
_LOGGER = logging.getLogger(__name__)


class AndroidTVRemoteApplication(Adw.Application):
    """Own the GTK window, protocol controller, and bounded shutdown."""

    def __init__(self, *, demo: bool = False, state_root: Path | None = None) -> None:
        flags = Gio.ApplicationFlags.NON_UNIQUE if demo else Gio.ApplicationFlags.DEFAULT_FLAGS
        super().__init__(application_id=APP_ID, flags=flags)
        self._window: RemoteWindow | None = None
        config_root = state_root / "config" if state_root else None
        data_root = state_root / "data" if state_root else None
        self._store = DeviceStore(config_root=config_root, data_root=data_root)
        controller_type = DemoController if demo else RemoteController
        self._controller = controller_type(
            store=self._store,
            state_callback=self._state_updated,
            discovery_callback=self._discovery_updated,
            ui_dispatcher=self._dispatch,
        )

    @staticmethod
    def _dispatch(callback: Any) -> None:
        GLib.idle_add(AndroidTVRemoteApplication._run_dispatched, callback)

    @staticmethod
    def _run_dispatched(callback: Any) -> bool:
        callback()
        return GLib.SOURCE_REMOVE

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        provider = Gtk.CssProvider()
        provider.load_from_path(str(Path(__file__).with_name("style.css")))
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def do_activate(self) -> None:
        if self._window is None:
            self._window = RemoteWindow(self, self._controller, self._store)
            try:
                devices = self._store.load()
            except DeviceStoreError:
                devices = []
            if devices:
                self._controller.connect(devices[0])
        self._window.present()

    def do_shutdown(self) -> None:
        try:
            completion = self._controller.shutdown()
            if completion is not None:
                completion.result(timeout=2)
        except Exception:
            _LOGGER.exception("Protocol controller did not shut down cleanly")
        finally:
            Adw.Application.do_shutdown(self)

    def _state_updated(self, state: RemoteState) -> None:
        if self._window is not None:
            self._window.update_state(state)

    def _discovery_updated(self, devices: list[DiscoveredDevice], error: str | None = None) -> None:
        if self._window is not None:
            self._window.update_discovery(devices, error)
