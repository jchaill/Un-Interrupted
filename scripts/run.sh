#!/bin/bash
# Start the web UI and the worker (one process, two threads) on macOS.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

if [ ! -x ".venv/bin/python" ] || [ ! -f ".venv/.bootstrap-ok" ]; then
  "$ROOT/scripts/bootstrap.sh"
fi

exec "$ROOT/.venv/bin/python" -m app "$@"
