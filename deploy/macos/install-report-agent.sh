#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SOURCE_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
APP_DIR="$HOME/Library/Application Support/WorkHistoryReportAgent"
CONFIG_PATH="$APP_DIR/config.toml"

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
  echo "Usage: $0 SERVER_URL [DEVICE_ID]" >&2
  exit 2
fi
DEVICE_ID=${2:-codex-report-agent}

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

if [ -L "$APP_DIR" ] || [ -L "$CONFIG_PATH" ]; then
  echo "Refusing to install through a symbolic link." >&2
  exit 1
fi
mkdir -p "$APP_DIR"
chmod 700 "$APP_DIR"
"$PYTHON_BIN" -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --upgrade pip
"$APP_DIR/venv/bin/pip" install "$SOURCE_DIR"
"$APP_DIR/venv/bin/work-history-report-agent" --config "$CONFIG_PATH" init \
  --server-url "$1" --device-id "$DEVICE_ID"

echo "Register the printed public key with deploy/server/register-report-device.sh."
