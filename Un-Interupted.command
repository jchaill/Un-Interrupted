#!/bin/bash
# Double-click in Finder to start Un-Interupted in the background.
cd "$(dirname "$0")"

if ./scripts/start.sh; then
  PORT="${UNINTERUPTED_PORT:-8090}"
  open "http://127.0.0.1:${PORT}/" >/dev/null 2>&1 || true
  echo
  echo "You can close this window. The server keeps running in the background."
  echo "To stop it, double-click Stop Un-Interupted.command."
else
  echo
  read -n 1 -s -r -p "Startup failed. Press any key to close…"
fi
