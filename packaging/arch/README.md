# Arch packaging

Each subdirectory is an independent AUR packagebase:

- `python-androidtvremote2` packages the protocol library required at runtime.
- `androidtvremote2-gtk` packages this application from an immutable upstream
  revision.

Regenerate each `.SRCINFO` from its package directory after changing its
`PKGBUILD`:

```sh
makepkg --printsrcinfo > .SRCINFO
```
