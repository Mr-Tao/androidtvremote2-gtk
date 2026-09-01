"""Main GTK window and focused-window input handling."""

from __future__ import annotations

import os
import re
import unicodedata
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from .models import ConnectionStatus, DeviceConfig, DiscoveredDevice, RemoteState
from .storage import DeviceStore, DeviceStoreError

_PAIRING_CODE_RE = re.compile(r"[0-9A-Fa-f]{6}\Z")


def _device_slug(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    base = re.sub(r"[^a-z0-9]+", "-", normalized.casefold()).strip("-") or "tv"
    return f"{base[:48].rstrip('-')}-{uuid.uuid4().hex[:8]}"


def _initial_identity_folder(identity_text: str, config_home: Path, managed_root: Path, home: Path) -> Path:
    current = Path(identity_text.strip()).expanduser() if identity_text.strip() else None
    candidates = (current, config_home / "androidtvremote2", managed_root)
    return next((candidate for candidate in candidates if candidate is not None and candidate.is_dir()), home)


class AddDeviceDialog(Adw.Dialog):
    """Discovery and manual-address dialog."""

    def __init__(self, window: RemoteWindow) -> None:
        super().__init__(title="Add TV", content_width=430, content_height=520)
        self._window = window
        self._selected: DiscoveredDevice | None = None

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        add_button = Gtk.Button(label="Add")
        add_button.add_css_class("suggested-action")
        add_button.connect("clicked", self._add)
        header.pack_end(add_button)
        toolbar.add_top_bar(header)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.add_css_class("remote-content")

        discovered_header = Gtk.Box(spacing=8)
        discovered_label = Gtk.Label(label="Discovered", xalign=0, hexpand=True)
        discovered_label.add_css_class("heading")
        self._spinner = Gtk.Spinner()
        scan_button = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Scan for TVs")
        scan_button.connect("clicked", self._scan)
        discovered_header.append(discovered_label)
        discovered_header.append(self._spinner)
        discovered_header.append(scan_button)
        content.append(discovered_header)

        self._discovered = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self._discovered.add_css_class("boxed-list")
        self._discovered.connect("row-activated", self._select_discovered)
        content.append(self._discovered)

        content.append(Gtk.Separator())
        manual_label = Gtk.Label(label="Device", xalign=0)
        manual_label.add_css_class("heading")
        content.append(manual_label)

        form = Gtk.Grid(column_spacing=12, row_spacing=10)
        name_label = Gtk.Label(label="Name", xalign=1)
        host_label = Gtk.Label(label="Address", xalign=1)
        self._name = Gtk.Entry(hexpand=True, placeholder_text="Living Room TV")
        self._host = Gtk.Entry(hexpand=True, placeholder_text="tv.local or 192.0.2.10")
        self._ime = Gtk.Switch(active=True, halign=Gtk.Align.START, valign=Gtk.Align.CENTER)
        ime_label = Gtk.Label(label="Text input and app state", xalign=1)
        identity_label = Gtk.Label(label="Pairing identity", xalign=1)
        identity_box = Gtk.Box(spacing=6)
        self._identity = Gtk.Entry(hexpand=True, placeholder_text="Optional existing identity")
        identity_browse = Gtk.Button(
            icon_name="document-open-symbolic",
            tooltip_text="Select directory containing cert.pem and key.pem",
        )
        identity_browse.connect("clicked", self._select_identity)
        identity_box.append(self._identity)
        identity_box.append(identity_browse)
        form.attach(name_label, 0, 0, 1, 1)
        form.attach(self._name, 1, 0, 1, 1)
        form.attach(host_label, 0, 1, 1, 1)
        form.attach(self._host, 1, 1, 1, 1)
        form.attach(ime_label, 0, 2, 1, 1)
        form.attach(self._ime, 1, 2, 1, 1)
        form.attach(identity_label, 0, 3, 1, 1)
        form.attach(identity_box, 1, 3, 1, 1)
        content.append(form)

        self._error = Gtk.Label(xalign=0, wrap=True)
        self._error.add_css_class("error")
        self._error.set_visible(False)
        content.append(self._error)

        toolbar.set_content(content)
        self.set_child(toolbar)
        self.connect("closed", self._closed)
        self._scan()

    def _closed(self, *_: Any) -> None:
        self._window.add_dialog_closed(self)

    def _scan(self, *_: Any) -> None:
        self._spinner.start()
        self._window.controller.discover(3.0)

    def update_discovery(self, devices: Sequence[DiscoveredDevice], error: str | None = None) -> None:
        self._spinner.stop()
        while row := self._discovered.get_row_at_index(0):
            self._discovered.remove(row)
        if error:
            self._set_error(error)
            return
        for device in devices:
            row = Gtk.ListBoxRow()
            row._remote_device = device  # type: ignore[attr-defined]
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=8, margin_bottom=8)
            name = Gtk.Label(label=device.name, xalign=0)
            name.add_css_class("device-title")
            address = Gtk.Label(label=f"{device.host}:{device.port}", xalign=0)
            address.add_css_class("device-detail")
            box.append(name)
            box.append(address)
            row.set_child(box)
            self._discovered.append(row)

    def _select_discovered(self, _: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        self._selected = row._remote_device  # type: ignore[attr-defined]
        self._name.set_text(self._selected.name)
        self._host.set_text(self._selected.host)

    def _set_error(self, message: str) -> None:
        self._error.set_label(message)
        self._error.set_visible(True)

    def _select_identity(self, *_: Any) -> None:
        dialog = Gtk.FileDialog(title="Select Pairing Identity")
        home = Path.home()
        config_home = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config").expanduser()
        initial_folder = _initial_identity_folder(
            self._identity.get_text(), config_home, self._window.store.managed_root, home
        )
        dialog.set_initial_folder(Gio.File.new_for_path(str(initial_folder)))
        dialog.select_folder(self._window, None, self._identity_selected)

    def _identity_selected(self, dialog: Gtk.FileDialog, result: Any) -> None:
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        path = folder.get_path()
        if path is not None:
            self._identity.set_text(path)

    def _add(self, *_: Any) -> None:
        name = self._name.get_text().strip()
        host = self._host.get_text().strip()
        identity_text = self._identity.get_text().strip()
        selected = self._selected if self._selected and self._selected.host == host else None
        port = selected.port if selected is not None else 6466
        identity = Path(identity_text).expanduser() if identity_text else None
        if identity is not None and not all((identity / filename).is_file() for filename in ("cert.pem", "key.pem")):
            self._set_error("Select an identity directory containing cert.pem and key.pem.")
            return
        try:
            device = DeviceConfig(
                id=_device_slug(name),
                display_name=name,
                host=host,
                api_port=port,
                enable_ime=self._ime.get_active(),
                paired=identity is not None,
                credential_directory=identity,
                service_name=selected.service_name if selected is not None else None,
                service_target=selected.service_target if selected is not None else None,
                service_identifier=selected.service_identifier if selected is not None else None,
            )
        except ValueError:
            self._set_error("Check the device name and address.")
            return
        self._window.begin_new_device(device)
        self.close()


class RemoteWindow(Adw.ApplicationWindow):
    """Render immutable controller state and translate local input to commands."""

    def __init__(self, application: Adw.Application, controller: Any, store: DeviceStore) -> None:
        super().__init__(application=application, title="Android TV Remote")
        self.controller = controller
        self.store = store
        self._devices: list[DeviceConfig] = []
        self._state = RemoteState()
        self._refreshing_devices = False
        self._add_dialog: AddDeviceDialog | None = None
        self._command_widgets: list[Gtk.Widget] = []
        self.set_default_size(430, 700)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self._device_model = Gtk.StringList.new([])
        self._device_dropdown = Gtk.DropDown(model=self._device_model, tooltip_text="Select TV")
        self._device_dropdown.set_size_request(190, -1)
        self._device_dropdown.connect("notify::selected", self._device_selected)
        header.set_title_widget(self._device_dropdown)

        add_button = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Add TV")
        add_button.connect("clicked", self._show_add_dialog)
        self._reconnect = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Reconnect")
        self._reconnect.connect("clicked", self._reconnect_selected)
        header.pack_start(add_button)
        header.pack_end(self._reconnect)
        toolbar.add_top_bar(header)

        scrolled = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        clamp = Adw.Clamp(maximum_size=510, tightening_threshold=360)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        content.add_css_class("remote-content")
        clamp.set_child(content)
        scrolled.set_child(clamp)
        toolbar.set_content(scrolled)
        self.set_content(toolbar)

        content.append(self._build_status())
        content.append(Gtk.Separator())
        content.append(self._build_navigation())
        content.append(self._build_dpad())
        content.append(Gtk.Separator())
        content.append(self._build_volume())
        content.append(self._build_media())
        content.append(Gtk.Separator())
        content.append(self._build_text_input())
        content.append(self._build_pairing())
        content.append(self._build_auth_recovery())

        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._key_pressed)
        self.add_controller(keys)
        self._set_controls_enabled(False)
        self.refresh_devices()

    def _build_status(self) -> Gtk.Widget:
        row = Gtk.Box(spacing=10, margin_bottom=4)
        self._status_dot = Gtk.Box()
        self._status_dot.add_css_class("status-dot")
        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, hexpand=True)
        self._status = Gtk.Label(label="Disconnected", xalign=0)
        self._status.add_css_class("device-title")
        self._detail = Gtk.Label(label="No TV selected", xalign=0, ellipsize=3)
        self._detail.add_css_class("device-detail")
        labels.append(self._status)
        labels.append(self._detail)
        row.append(self._status_dot)
        row.append(labels)
        self._volume_value = Gtk.Label(label="--", xalign=1)
        self._volume_value.add_css_class("volume-value")
        row.append(self._volume_value)
        return row

    def _button(self, icon: str, tooltip: str, command: str, *classes: str) -> Gtk.Button:
        button = Gtk.Button(icon_name=icon, tooltip_text=tooltip)
        button.add_css_class("remote-button")
        for css_class in classes:
            button.add_css_class(css_class)
        button.connect("clicked", lambda *_: self.controller.send_key(command))
        self._command_widgets.append(button)
        return button

    def _build_navigation(self) -> Gtk.Widget:
        row = Gtk.Box(spacing=6, homogeneous=True)
        row.add_css_class("remote-section")
        row.append(self._button("go-previous-symbolic", "Back", "BACK"))
        row.append(self._button("go-home-symbolic", "Home", "HOME"))
        row.append(self._button("open-menu-symbolic", "Menu", "MENU"))
        row.append(self._button("system-shutdown-symbolic", "Power", "POWER"))
        return row

    def _build_dpad(self) -> Gtk.Widget:
        grid = Gtk.Grid(column_spacing=6, row_spacing=6, halign=Gtk.Align.CENTER)
        grid.add_css_class("remote-section")
        up = self._button("pan-up-symbolic", "Up", "DPAD_UP", "dpad-button")
        left = self._button("pan-start-symbolic", "Left", "DPAD_LEFT", "dpad-button")
        center = self._button("object-select-symbolic", "Select", "DPAD_CENTER", "dpad-center")
        right = self._button("pan-end-symbolic", "Right", "DPAD_RIGHT", "dpad-button")
        down = self._button("pan-down-symbolic", "Down", "DPAD_DOWN", "dpad-button")
        grid.attach(up, 1, 0, 1, 1)
        grid.attach(left, 0, 1, 1, 1)
        grid.attach(center, 1, 1, 1, 1)
        grid.attach(right, 2, 1, 1, 1)
        grid.attach(down, 1, 2, 1, 1)
        return grid

    def _build_volume(self) -> Gtk.Widget:
        row = Gtk.Box(spacing=6, homogeneous=True)
        row.add_css_class("remote-section")
        row.append(self._button("audio-volume-low-symbolic", "Volume down", "VOLUME_DOWN"))
        self._mute = self._button("audio-volume-muted-symbolic", "Mute", "MUTE")
        row.append(self._mute)
        row.append(self._button("audio-volume-high-symbolic", "Volume up", "VOLUME_UP"))
        return row

    def _build_media(self) -> Gtk.Widget:
        row = Gtk.Box(spacing=6, homogeneous=True)
        row.append(self._button("media-skip-backward-symbolic", "Previous", "MEDIA_PREVIOUS"))
        row.append(self._button("media-playback-start-symbolic", "Play or pause", "MEDIA_PLAY_PAUSE"))
        row.append(self._button("media-playback-stop-symbolic", "Stop", "MEDIA_STOP"))
        row.append(self._button("media-skip-forward-symbolic", "Next", "MEDIA_NEXT"))
        return row

    def _build_text_input(self) -> Gtk.Widget:
        row = Gtk.Box(spacing=6)
        self._text = Gtk.Entry(hexpand=True, placeholder_text="Send text")
        self._text.connect("activate", self._send_text)
        send = Gtk.Button(icon_name="mail-send-symbolic", tooltip_text="Send text")
        send.connect("clicked", self._send_text)
        self._command_widgets.extend((self._text, send))
        row.append(self._text)
        row.append(send)
        return row

    def _build_pairing(self) -> Gtk.Widget:
        self._pairing = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        row = Gtk.Box(spacing=8)
        row.add_css_class("pairing-strip")
        label = Gtk.Label(label="Code shown on TV", xalign=0)
        self._pairing_code = Gtk.Entry(max_length=6, width_chars=8, placeholder_text="000000")
        self._pairing_code.connect("activate", self._finish_pairing)
        submit = Gtk.Button(label="Pair")
        submit.add_css_class("suggested-action")
        submit.connect("clicked", self._finish_pairing)
        row.append(label)
        row.append(self._pairing_code)
        row.append(submit)
        self._pairing.set_child(row)
        return self._pairing

    def _build_auth_recovery(self) -> Gtk.Widget:
        self._auth_recovery = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        row = Gtk.Box(spacing=8)
        row.add_css_class("pairing-strip")
        label = Gtk.Label(
            label="The saved pairing identity is no longer accepted.",
            xalign=0,
            hexpand=True,
            wrap=True,
        )
        reset = Gtk.Button(label="Pair Again")
        reset.add_css_class("destructive-action")
        reset.connect("clicked", self._confirm_pairing_reset)
        row.append(label)
        row.append(reset)
        self._auth_recovery.set_child(row)
        return self._auth_recovery

    def refresh_devices(self, select_id: str | None = None) -> None:
        try:
            self._devices = self.store.load()
        except DeviceStoreError as exc:
            self._render_error(str(exc))
            self._devices = []
        self._refreshing_devices = True
        self._device_model.splice(
            0, self._device_model.get_n_items(), [device.display_name for device in self._devices]
        )
        self._device_dropdown.set_sensitive(bool(self._devices))
        if self._devices:
            index = next((i for i, device in enumerate(self._devices) if device.id == select_id), 0)
            self._device_dropdown.set_selected(index)
        self._refreshing_devices = False

    def begin_new_device(self, device: DeviceConfig) -> None:
        self._status.set_label(f"Connecting to {device.display_name}")
        if device.credential_directory is not None:
            try:
                self.store.upsert(device)
            except DeviceStoreError as exc:
                self._render_error(str(exc))
                return
            self.refresh_devices(device.id)
        self.controller.connect(device)

    def _device_selected(self, *_: Any) -> None:
        if self._refreshing_devices or not self._devices:
            return
        selected = self._device_dropdown.get_selected()
        if selected < len(self._devices):
            self.controller.connect(self._devices[selected])

    def _reconnect_selected(self, *_: Any) -> None:
        if self._state.device is not None:
            current = next(
                (device for device in self._devices if device.id == self._state.device.id),
                self._state.device,
            )
            self.controller.connect(current)
        elif self._devices:
            self.controller.connect(self._devices[self._device_dropdown.get_selected()])

    def _show_add_dialog(self, *_: Any) -> None:
        self._add_dialog = AddDeviceDialog(self)
        self._add_dialog.present(self)

    def add_dialog_closed(self, dialog: AddDeviceDialog) -> None:
        if self._add_dialog is dialog:
            self._add_dialog = None

    def update_discovery(self, devices: Sequence[DiscoveredDevice], error: str | None = None) -> None:
        self.refresh_devices(self._state.device_id)
        if self._add_dialog is not None:
            self._add_dialog.update_discovery(devices, error)

    def update_state(self, state: RemoteState) -> None:
        self._state = state
        connected = state.status is ConnectionStatus.CONNECTED
        self._set_controls_enabled(connected)
        self._pairing.set_reveal_child(state.status is ConnectionStatus.PAIRING)
        self._auth_recovery.set_reveal_child(state.status is ConnectionStatus.AUTH_REQUIRED)
        status_text = {
            ConnectionStatus.DISCONNECTED: "Disconnected",
            ConnectionStatus.CONNECTING: "Connecting",
            ConnectionStatus.PAIRING: "Waiting for pairing code",
            ConnectionStatus.CONNECTED: "Connected",
            ConnectionStatus.RECONNECTING: "Reconnecting",
            ConnectionStatus.AUTH_REQUIRED: "Pairing required",
            ConnectionStatus.FAILED: "Connection failed",
            ConnectionStatus.SHUTDOWN: "Closing",
        }[state.status]
        self._status.set_label(status_text)
        self._status_dot.set_css_classes(["status-dot"])
        if connected:
            self._status_dot.add_css_class("connected")
        elif state.status in {ConnectionStatus.CONNECTING, ConnectionStatus.PAIRING, ConnectionStatus.RECONNECTING}:
            self._status_dot.add_css_class("pending")
        elif state.status in {ConnectionStatus.AUTH_REQUIRED, ConnectionStatus.FAILED}:
            self._status_dot.add_css_class("error")

        details = [value for value in (state.manufacturer, state.model, state.current_app) if value]
        if not details and state.device is not None:
            details.append(state.device.display_name)
        self._detail.set_label(" · ".join(details) if details else "No TV selected")
        if state.volume_level is not None and state.volume_max is not None:
            suffix = " muted" if state.is_muted else ""
            self._volume_value.set_label(f"{state.volume_level}/{state.volume_max}{suffix}")
        else:
            self._volume_value.set_label("--")
        self._mute.set_icon_name("audio-volume-muted-symbolic" if state.is_muted else "audio-volume-medium-symbolic")
        if state.error:
            self._detail.set_label(state.error)
        if connected and state.device is not None and state.device.paired:
            self.refresh_devices(state.device.id)

    def _render_error(self, message: str) -> None:
        self._status.set_label("Configuration error")
        self._detail.set_label(message)
        self._status_dot.add_css_class("error")

    def _set_controls_enabled(self, enabled: bool) -> None:
        for widget in self._command_widgets:
            widget.set_sensitive(enabled)
        self._reconnect.set_sensitive(self._state.device is not None or bool(self._devices))

    def _finish_pairing(self, *_: Any) -> None:
        code = self._pairing_code.get_text().strip()
        if not _PAIRING_CODE_RE.fullmatch(code):
            self._pairing_code.add_css_class("error")
            return
        self._pairing_code.remove_css_class("error")
        self.controller.finish_pairing(code)
        self._pairing_code.set_text("")

    def _confirm_pairing_reset(self, *_: Any) -> None:
        dialog = Adw.AlertDialog.new(
            "Pair this TV again?",
            "The saved pairing identity will be forgotten and replaced. External credential files will not be deleted.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("reset", "Pair Again")
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("reset", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.choose(self, None, self._pairing_reset_response)

    def _pairing_reset_response(self, dialog: Adw.AlertDialog, result: Any) -> None:
        if dialog.choose_finish(result) == "reset":
            self.controller.reset_pairing()

    def _send_text(self, *_: Any) -> None:
        text = self._text.get_text()
        if text:
            self.controller.send_text(text)
            self._text.set_text("")

    def _key_pressed(self, _: Gtk.EventControllerKey, keyval: int, __: int, ___: Gdk.ModifierType) -> bool:
        if isinstance(self.get_focus(), (Gtk.Entry, Gtk.TextView)):
            return False
        mapping = {
            Gdk.KEY_Up: "DPAD_UP",
            Gdk.KEY_Down: "DPAD_DOWN",
            Gdk.KEY_Left: "DPAD_LEFT",
            Gdk.KEY_Right: "DPAD_RIGHT",
            Gdk.KEY_Return: "DPAD_CENTER",
            Gdk.KEY_KP_Enter: "DPAD_CENTER",
            Gdk.KEY_Escape: "BACK",
            Gdk.KEY_Home: "HOME",
            Gdk.KEY_Delete: "POWER",
            Gdk.KEY_space: "MEDIA_PLAY_PAUSE",
            Gdk.KEY_m: "MUTE",
            Gdk.KEY_M: "MUTE",
            Gdk.KEY_plus: "VOLUME_UP",
            Gdk.KEY_KP_Add: "VOLUME_UP",
            Gdk.KEY_minus: "VOLUME_DOWN",
            Gdk.KEY_KP_Subtract: "VOLUME_DOWN",
        }
        command = mapping.get(keyval)
        if command is None or self._state.status is not ConnectionStatus.CONNECTED:
            return False
        self.controller.send_key(command)
        return True
