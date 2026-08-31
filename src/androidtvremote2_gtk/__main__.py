"""Command-line entry point."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__
from .application import AndroidTVRemoteApplication


def main(argv: list[str] | None = None) -> int:
    """Run the GTK application."""
    os.umask(0o077)
    parser = argparse.ArgumentParser(prog="androidtvremote2-gtk")
    parser.add_argument("--demo", action="store_true", help="use deterministic local demo data")
    parser.add_argument("--state-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--version", action="version", version=__version__)
    options, gtk_args = parser.parse_known_args(argv)
    application = AndroidTVRemoteApplication(demo=options.demo, state_root=options.state_root)
    return application.run([parser.prog, *gtk_args])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
