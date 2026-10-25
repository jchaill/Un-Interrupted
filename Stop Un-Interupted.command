#!/bin/bash
# Double-click in Finder to stop the background server.
cd "$(dirname "$0")"
./scripts/stop.sh
echo
read -n 1 -s -r -p "Press any key to close…"
