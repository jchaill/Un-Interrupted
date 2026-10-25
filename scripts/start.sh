#!/bin/bash
# Start Un-Interupted in the background and wait until the web UI answers.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

PORT="${UNINTERUPTED_PORT:-8090}"
URL="http://127.0.0.1:${PORT}/"
HEALTH="http://127.0.0.1:${PORT}/health"
LOG="$HOME/Library/Logs/Un-Interupted.log"
mkdir -p "$(dirname "$LOG")"

if curl -sf --max-time 2 "$HEALTH" >/dev/null 2>&1; then
  echo "Un-Interupted is already running at $URL"
  exit 0
fi

if [ ! -x ".venv/bin/python" ] || [ ! -f ".venv/.bootstrap-ok" ]; then
  echo "First run: setting up the Python 3.12 environment."
  echo "This can take a few minutes. Leave this window open."
  "$ROOT/scripts/bootstrap.sh"
fi

echo "Starting Un-Interupted…"
nohup "$ROOT/scripts/run.sh" >>"$LOG" 2>&1 </dev/null &
PID=$!

for _ in $(seq 1 40); do
  if curl -sf --max-time 2 "$HEALTH" >/dev/null 2>&1; then
    echo "Running at $URL"
    exit 0
  fi
  if ! kill -0 "$PID" 2>/dev/null; then
    echo
    echo "The server exited before it was ready. Last log lines:"
    tail -40 "$LOG" 2>/dev/null || true
    echo
    echo "Full log: $LOG"
    exit 1
  fi
  sleep 1
done

echo "Timed out waiting for $URL. Last log lines:"
tail -40 "$LOG" 2>/dev/null || true
echo
echo "Full log: $LOG"
exit 1
