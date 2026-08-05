#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root inside the work-history LXC." >&2
  exit 1
fi
if [ "$#" -ne 2 ]; then
  echo "Usage: $0 DEVICE_ID PUBLIC_KEY" >&2
  exit 2
fi

runuser -u workhistory -- env \
  DATABASE_URL='postgresql+psycopg:///workhistory?host=/var/run/postgresql' \
  /opt/work-history/venv/bin/work-history register-device \
  --device-id "$1" --public-key "$2" --purpose report_agent
