#!/bin/sh
# TimeCheat launcher for macOS / Linux: double-click it in Finder, or run ./start.command [options].
# The first start creates a Python environment in .venv; packages are reinstalled when requirements.txt changes.
cd "$(dirname "$0")" || exit 1

fail() {
  echo
  echo "$1"
  printf "Press Enter to close this window..."
  read -r _
  exit 1
}

if [ ! -x .venv/bin/python ]; then
  PY=""
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 &&
      "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
      PY=$candidate
      break
    fi
  done
  [ -n "$PY" ] || fail "TimeCheat needs Python 3.10 or newer: https://www.python.org/downloads/"
  echo "Creating the Python environment (first start only)..."
  "$PY" -m venv .venv || fail "Could not create the Python environment in .venv."
fi

if ! cmp -s requirements.txt .venv/requirements.installed; then
  echo "Installing the required packages..."
  .venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt ||
    fail "Installing the packages failed. Is https://pypi.org reachable?"
  cp requirements.txt .venv/requirements.installed
fi

.venv/bin/python run.py "$@" || fail "TimeCheat stopped with an error (see above)."
