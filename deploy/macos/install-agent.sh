#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SOURCE_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
APP_DIR="$HOME/Library/Application Support/WorkHistoryAgent"
CONFIG_PATH="$APP_DIR/config.toml"
LOG_DIR="$HOME/Library/Logs/WorkHistoryAgent"
PLIST_PATH="$HOME/Library/LaunchAgents/com.workhistory.gitlab-agent.plist"

if [ "$#" -lt 3 ] || [ "$#" -gt 4 ]; then
  echo "Usage: $0 SERVER_URL GITLAB_URL DEVICE_ID [HISTORY_START]" >&2
  exit 2
fi
HISTORY_START=${4:-}

PYTHON_BIN=
for candidate in python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    if "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
      PYTHON_BIN=$(command -v "$candidate")
      break
    fi
  fi
done
if [ -z "$PYTHON_BIN" ]; then
  echo "Python 3.11 or newer is required." >&2
  exit 1
fi

mkdir -p "$APP_DIR" "$LOG_DIR" "$HOME/Library/LaunchAgents"
chmod 700 "$APP_DIR"
"$PYTHON_BIN" -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --upgrade pip
"$APP_DIR/venv/bin/pip" install "$SOURCE_DIR"

if [ -n "$HISTORY_START" ]; then
  "$APP_DIR/venv/bin/work-history-agent" --config "$CONFIG_PATH" init \
    --server-url "$1" --gitlab-url "$2" --device-id "$3" \
    --history-start "$HISTORY_START"
else
  "$APP_DIR/venv/bin/work-history-agent" --config "$CONFIG_PATH" init \
    --server-url "$1" --gitlab-url "$2" --device-id "$3"
fi

AGENT_BIN="$APP_DIR/venv/bin/work-history-agent"
sed \
  -e "s|__AGENT_BIN__|$AGENT_BIN|g" \
  -e "s|__CONFIG_PATH__|$CONFIG_PATH|g" \
  -e "s|__LOG_DIR__|$LOG_DIR|g" \
  "$SCRIPT_DIR/com.workhistory.gitlab-agent.plist.template" > "$PLIST_PATH"
chmod 644 "$PLIST_PATH"
plutil -lint "$PLIST_PATH"

echo "Register the printed device public key on the server, then run enable-agent.sh."
