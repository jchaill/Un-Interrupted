#!/bin/bash
set -euo pipefail
LABEL="com.uninterupted.app"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
rm -f "$PLIST"
echo "Removed $LABEL. Your library and app data are untouched."
