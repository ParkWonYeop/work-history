#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SOURCE_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
APP_DIR="$HOME/Library/Application Support/WorkHistoryAgent"
CONFIG_PATH="$APP_DIR/config.toml"
LOG_DIR="$HOME/Library/Logs/WorkHistoryAgent"
PLIST_PATH="$HOME/Library/LaunchAgents/com.workhistory.gitlab-agent.plist"
AGENT_BIN="$APP_DIR/venv/bin/work-history-agent"

if [ "$#" -gt 1 ]; then
  echo "Usage: $0 [HISTORY_START]" >&2
  exit 2
fi
HISTORY_START=${1:-}

if [ -L "$APP_DIR" ] || [ -L "$CONFIG_PATH" ] || [ ! -x "$AGENT_BIN" ] || [ ! -f "$CONFIG_PATH" ]; then
  echo "The existing agent installation is missing or unsafe to update." >&2
  exit 1
fi
if [ -L "$PLIST_PATH" ]; then
  echo "Refusing to replace a symbolic-link LaunchAgent." >&2
  exit 1
fi

"$APP_DIR/venv/bin/pip" install --no-deps --force-reinstall "$SOURCE_DIR"
if [ -n "$HISTORY_START" ]; then
  "$AGENT_BIN" --config "$CONFIG_PATH" set-history-start \
    --history-start "$HISTORY_START"
fi

TMP_PLIST=$(mktemp)
trap 'rm -f "$TMP_PLIST"' EXIT
sed \
  -e "s|__AGENT_BIN__|$AGENT_BIN|g" \
  -e "s|__CONFIG_PATH__|$CONFIG_PATH|g" \
  -e "s|__LOG_DIR__|$LOG_DIR|g" \
  "$SCRIPT_DIR/com.workhistory.gitlab-agent.plist.template" > "$TMP_PLIST"
plutil -lint "$TMP_PLIST"
install -m 0644 "$TMP_PLIST" "$PLIST_PATH"

launchctl bootout "gui/$(id -u)" "$PLIST_PATH" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
launchctl print "gui/$(id -u)/com.workhistory.gitlab-agent"
