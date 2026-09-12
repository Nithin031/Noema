"""Noema entry point: ``python -m noema`` serves the local daemon."""

from .cli.daemon import main

if __name__ == "__main__":
    raise SystemExit(main())
