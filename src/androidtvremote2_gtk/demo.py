"""Deterministic in-process controller for UI development and screenshots."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from .models import ConnectionStatus, DeviceConfig, DiscoveredDevice, RemoteState


class DemoController:
    """Implement the controller surface without network access."""

    def __init__(
        self,
        state_callback: Any,
        discovery_callback: Any,
        ui_dispatcher: Any,
        **_: Any,
    ) -> None:
        self._state_callback = state_callback
        self._discovery_callback = discovery_callback
        self._dispatcher = ui_dispatcher
        self._state = RemoteState()

    def _emit(self, state: RemoteState) -> None:
        self._state = state
        self._dispatcher(lambda: self._state_callback(state))

    def connect(self, device: DeviceConfig) -> None:
        self._emit(RemoteState(status=ConnectionStatus.CONNECTING, device=device))
        connected = replace(device, paired=True)
        self._emit(
            RemoteState(
                status=ConnectionStatus.CONNECTED,
                device=connected,
                manufacturer="Demo",
                model="Google TV",
                software_version="1.0",
                is_on=True,
                volume_level=18,
                volume_max=50,
                is_muted=False,
                current_app="com.google.android.youtube.tv",
            )
        )

    def finish_pairing(self, _: str) -> None:
        if self._state.device is not None:
            self.connect(self._state.device)

    def reset_pairing(self) -> None:
        if self._state.device is not None:
            device = replace(self._state.device, paired=False, credential_directory=None)
            self._emit(RemoteState(status=ConnectionStatus.PAIRING, device=device))

    def disconnect(self) -> None:
        self._emit(RemoteState())

    def send_key(self, code: str, direction: str = "SHORT") -> None:
        del direction
        state = self._state
        if state.status is not ConnectionStatus.CONNECTED:
            return
        if code == "POWER":
            self._emit(replace(state, is_on=not bool(state.is_on)))
        elif code in {"MUTE", "VOLUME_MUTE"}:
            self._emit(replace(state, is_muted=not bool(state.is_muted)))
        elif code == "VOLUME_UP" and state.volume_level is not None and state.volume_max is not None:
            self._emit(replace(state, volume_level=min(state.volume_level + 1, state.volume_max)))
        elif code == "VOLUME_DOWN" and state.volume_level is not None:
            self._emit(replace(state, volume_level=max(state.volume_level - 1, 0)))

    def send_text(self, _: str) -> None:
        return

    def launch_app(self, _: str) -> None:
        return

    def discover(self, timeout: float = 3.0) -> None:
        del timeout
        devices = [
            DiscoveredDevice(name="Living Room TV", host="192.0.2.10"),
            DiscoveredDevice(name="TV Box", host="2001:db8::20"),
        ]
        self._dispatcher(lambda: self._discovery_callback(devices, None))

    def shutdown(self) -> None:
        self._emit(RemoteState(status=ConnectionStatus.SHUTDOWN))
