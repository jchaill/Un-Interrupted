#!/bin/bash
# Stop the background server started by scripts/start.sh or Un-Interupted.command.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${UNINTERUPTED_PORT:-8090}"

PIDS="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)"
if [ -z "$PIDS" ]; then
  echo "Un-Interupted is not running on port $PORT."
  exit 0
fi

# Refuse to kill an unrelated process that happens to use the port.
for pid in $PIDS; do
  if ! ps -p "$pid" -o command= 2>/dev/null | grep -q -- "-m app"; then
    echo "Port $PORT is used by PID $pid, which does not look like Un-Interupted:"
    ps -p "$pid" -o command= 2>/dev/null || true
    exit 1
  fi
done

if launchctl print "gui/$(id -u)/com.uninterupted.app" >/dev/null 2>&1; then
  echo "Note: launchd autostart is installed and may restart the app."
  echo "      Run scripts/uninstall-launchd.sh to disable autostart."
fi

echo "Stopping Un-Interupted (PID $PIDS)…"
kill $PIDS 2>/dev/null || true

# The engine starts Whisper/ffmpeg in their own session so a timeout can kill
# the whole pipeline. That also means they survive when the app is stopped;
# clean up only processes tied to this app's model or vendored scripts.
pkill -f "$HOME/Library/Application Support/Un-Interupted/models/ggml-" 2>/dev/null || true
pkill -f "$ROOT/app/engine/upstream/scripts" 2>/dev/null || true
for _ in $(seq 1 20); do
  if ! lsof -tiTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "Stopped."
    exit 0
  fi
  sleep 0.5
done

echo "Still running; sending SIGKILL."
kill -9 $PIDS 2>/dev/null || true
echo "Stopped."
