# Android TV Remote 2 GTK

A native GTK 4 remote control for Android TV and Google TV devices. It uses
the same Remote Protocol v2 as Google's mobile remote, so it does not require
ADB, developer mode, or a debug port on the television.

## Features

- Automatic mDNS discovery with manual host entry as a fallback.
- Per-device pairing identities stored outside the application package.
- Optional reuse of an existing certificate and key without copying them.
- Multiple saved televisions with automatic reconnect.
- D-pad, navigation, volume, power, and media controls.
- Text input and keyboard shortcuts.
- Live connection, foreground-app, power, and volume state when reported by
  the device.

## Runtime requirements

The Python dependencies are declared in `pyproject.toml`. Linux distributions
must additionally provide GTK 4, libadwaita, and PyGObject. On Arch Linux these
are `gtk4`, `libadwaita`, and `python-gobject`.

Run from a source checkout with system GTK bindings:

```sh
python -m venv --system-site-packages .venv
.venv/bin/pip install -e ../androidtvremote2 -e .
.venv/bin/python -m androidtvremote2_gtk
```

Use `--demo` to exercise the interface without connecting to a television.

## Security

Pairing creates a client certificate and private key. Device metadata lives in
`${XDG_CONFIG_HOME:-~/.config}/androidtvremote2-gtk/devices.json`; managed
identities live below
`${XDG_DATA_HOME:-~/.local/share}/androidtvremote2-gtk/devices` in mode-0700
directories with mode-0600 files. Device metadata does not contain private-key
material. An existing identity can be referenced by its directory without
copying it.

The application never silently replaces a saved pairing identity. If a TV no
longer accepts it, the interface requires confirmation before pairing again.
That action removes an application-managed identity; externally referenced
credential files are left untouched.

The application never sends credentials to a discovery service. The underlying
protocol authenticates with the paired client certificate; like the upstream
library, it does not validate the television's TLS certificate or hostname.

## Packaging

Arch packaging lives in `packaging/arch`. The protocol library is packaged
separately as `python-androidtvremote2`, while this application is packaged as
`androidtvremote2-gtk`.

## License

Apache-2.0. See `LICENSE`.
