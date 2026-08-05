#!/bin/sh
set -eu

PLIST_PATH="$HOME/Library/LaunchAgents/com.workhistory.gitlab-agent.plist"
if [ ! -f "$PLIST_PATH" ]; then
  echo "Install the agent first." >&2
  exit 1
fi

launchctl bootout "gui/$(id -u)" "$PLIST_PATH" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
launchctl print "gui/$(id -u)/com.workhistory.gitlab-agent"
