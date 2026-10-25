#!/bin/bash
# Create the Python 3.12 virtualenv and install dependencies.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

echo "== Un-Interupted bootstrap =="

if ! command -v brew >/dev/null 2>&1; then
  echo "Homebrew is required. Install it from https://brew.sh and run this again." >&2
  exit 1
fi

PYTHON="${UNINTERUPTED_PYTHON:-}"
if [ -z "$PYTHON" ]; then
  if command -v python3.12 >/dev/null 2>&1; then
    PYTHON="$(command -v python3.12)"
  elif [ -x "$(brew --prefix)/opt/python@3.12/bin/python3.12" ]; then
    PYTHON="$(brew --prefix)/opt/python@3.12/bin/python3.12"
  else
    echo "Installing Python 3.12 with Homebrew..."
    if ! brew install python@3.12; then
      cat >&2 <<'MSG'

Could not install python@3.12. On a fresh Mac this usually means the Xcode
license has not been accepted. Run this once in Terminal, then retry:

    sudo xcodebuild -license accept

MSG
      exit 1
    fi
    PYTHON="$(brew --prefix)/opt/python@3.12/bin/python3.12"
  fi
fi

if [ ! -x "$PYTHON" ]; then
  echo "Python 3.12 not found at $PYTHON" >&2
  exit 1
fi

if [ -x ".venv/bin/python" ]; then
  if ! .venv/bin/python -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)' 2>/dev/null; then
    echo "Existing .venv is not Python 3.12; replacing it."
    rm -rf .venv
  fi
fi

if [ ! -x ".venv/bin/python" ]; then
  echo "Creating .venv with $($PYTHON --version)"
  "$PYTHON" -m venv .venv
fi

echo "Installing Python dependencies..."
.venv/bin/python -m pip install --disable-pip-version-check --upgrade pip setuptools wheel >/dev/null
.venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt

touch .venv/.bootstrap-ok
echo "Bootstrap complete."
