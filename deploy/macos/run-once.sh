#!/bin/sh
set -eu

APP_DIR="$HOME/Library/Application Support/WorkHistoryAgent"
exec "$APP_DIR/venv/bin/work-history-agent" \
  --config "$APP_DIR/config.toml" catch-up
