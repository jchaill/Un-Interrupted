#!/bin/bash
# Development: auto-reloading web process + a separate worker.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

if [ ! -x ".venv/bin/python" ]; then
  ./scripts/bootstrap.sh
fi

trap 'kill 0' EXIT INT TERM
UNINTERUPTED_NO_WORKER=1 .venv/bin/python -m uvicorn app.web:app --host 127.0.0.1 --port 8090 --reload &
.venv/bin/python -m app.worker &
wait
