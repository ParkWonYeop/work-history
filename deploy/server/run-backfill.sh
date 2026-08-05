#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root inside the work-history LXC." >&2
  exit 1
fi
if [ "$#" -ne 2 ]; then
  echo "Usage: $0 FROM_RFC3339 TO_RFC3339" >&2
  exit 2
fi
case "$1" in
  *[!0-9T:+-]*) echo "Invalid FROM timestamp." >&2; exit 1 ;;
esac
case "$2" in
  *[!0-9T:+-]*) echo "Invalid TO timestamp." >&2; exit 1 ;;
esac

TMP_FILE=$(mktemp)
trap 'rm -f "$TMP_FILE"' EXIT
printf 'BACKFILL_FROM=%s\nBACKFILL_TO=%s\n' "$1" "$2" > "$TMP_FILE"
install -o root -g workhistory -m 0640 "$TMP_FILE" /etc/work-history/backfill.env
systemctl start work-history-backfill.service
systemctl --no-pager --full status work-history-backfill.service || true
test "$(systemctl show work-history-backfill.service --property=Result --value)" = "success"
