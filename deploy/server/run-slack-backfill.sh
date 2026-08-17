#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root inside the work-history LXC." >&2
  exit 1
fi
if [ "$#" -lt 2 ] || [ "$#" -gt 3 ]; then
  echo "Usage: $0 FROM_RFC3339 TO_RFC3339 [--no-block]" >&2
  exit 2
fi
case "$1" in
  *[!0-9T:+-]*) echo "Invalid FROM timestamp." >&2; exit 1 ;;
esac
case "$2" in
  *[!0-9T:+-]*) echo "Invalid TO timestamp." >&2; exit 1 ;;
esac
if [ "$#" -eq 3 ] && [ "$3" != "--no-block" ]; then
  echo "The optional third argument must be --no-block." >&2
  exit 2
fi

TMP_FILE=$(mktemp)
trap 'rm -f "$TMP_FILE"' EXIT
printf 'SLACK_BACKFILL_FROM=%s\nSLACK_BACKFILL_TO=%s\n' "$1" "$2" > "$TMP_FILE"
install -o root -g workhistory -m 0640 "$TMP_FILE" /etc/work-history/slack-backfill.env
if [ "$#" -eq 3 ]; then
  systemctl start --no-block work-history-slack-backfill.service
else
  systemctl start work-history-slack-backfill.service
  systemctl --no-pager --full status work-history-slack-backfill.service || true
  test "$(systemctl show work-history-slack-backfill.service --property=Result --value)" = "success"
fi
