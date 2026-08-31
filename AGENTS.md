# Project Instructions

- Never commit Android TV certificate or private-key files, device addresses,
  pairing codes, or generated user configuration.
- Keep the GTK main loop and the asyncio protocol loop separated. GTK widgets
  may only be updated on the GTK thread.
- Keep the protocol library behind the controller boundary so unit tests can
  use deterministic fakes without a physical television.
- Run `pytest`, `ruff check`, package builds, desktop-file validation, and
  AppStream validation before release commits.
- A successful socket write is not a physical-device verification. Record real
  device checks separately.
