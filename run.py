"""Start TimeCheat: ``python run.py`` (or ``python run.py --demo`` without Jira)."""

import argparse

from timecheat import __version__
from timecheat.app import main

if __name__ in {"__main__", "__mp_main__"}:
    parser = argparse.ArgumentParser(description="TimeCheat - book Jira worklogs on a calendar.")
    parser.add_argument("--demo", action="store_true", help="use an in-memory fake Jira instead of a server")
    parser.add_argument("--port", type=int, default=8765, help="local port (default: 8765)")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser tab")
    parser.add_argument("--version", action="version", version=f"TimeCheat {__version__}")
    args = parser.parse_args()
    main(demo=args.demo, port=args.port, show=not args.no_browser)
