#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SOURCE_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
APP_DIR="$HOME/Library/Application Support/WorkHistoryReportAgent"
CONFIG_PATH="$APP_DIR/config.toml"
AGENT_BIN="$APP_DIR/venv/bin/work-history-report-agent"

if [ -L "$APP_DIR" ] || [ -L "$CONFIG_PATH" ] || [ ! -x "$AGENT_BIN" ]; then
  echo "The existing report-agent installation is missing or unsafe to update." >&2
  exit 1
fi
# Reinstalls the package and adds new dependencies such as the MCP extra; installed
# dependencies are left alone while they satisfy pyproject.toml.
"$APP_DIR/venv/bin/pip" install -c "$SOURCE_DIR/constraints.txt" "$SOURCE_DIR[mcp]"
"$AGENT_BIN" --help >/dev/null
"$APP_DIR/venv/bin/work-history-mcp" --help >/dev/null
