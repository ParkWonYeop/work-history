#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root inside the work-history LXC." >&2
  exit 1
fi

ENV_FILE=/etc/work-history/archive.env
ACCESS_FILE=/etc/work-history/credentials/r2-access-key-id
SECRET_FILE=/etc/work-history/credentials/r2-secret-access-key
if [ ! -f /etc/work-history/server.env ] || [ ! -f "$ENV_FILE" ]; then
  echo "Install the server first." >&2
  exit 1
fi

printf 'Cloudflare Account ID: '
IFS= read -r ACCOUNT_ID
printf 'R2 bucket [work-history-archive]: '
IFS= read -r BUCKET
BUCKET=${BUCKET:-work-history-archive}
printf 'age recipient (age1...): '
IFS= read -r AGE_RECIPIENT

restore_terminal() {
  stty echo 2>/dev/null || true
}
trap restore_terminal EXIT HUP INT TERM
printf 'R2 access key ID: '
stty -echo
IFS= read -r ACCESS_KEY
stty echo
printf '\nR2 secret access key: '
stty -echo
IFS= read -r SECRET_KEY
stty echo
trap - EXIT HUP INT TERM
printf '\n'

case "$ACCOUNT_ID" in
  *[!0-9a-fA-F]*|'') echo "Cloudflare Account ID must be hexadecimal." >&2; exit 1 ;;
esac
if [ "${#ACCOUNT_ID}" -ne 32 ]; then
  echo "Cloudflare Account ID must contain 32 characters." >&2
  exit 1
fi
case "$BUCKET" in
  *[!a-z0-9.-]*|'') echo "Invalid R2 bucket name." >&2; exit 1 ;;
esac
case "$AGE_RECIPIENT" in
  age1*) ;;
  *) echo "Expected an age X25519 recipient beginning with age1." >&2; exit 1 ;;
esac
if [ -z "$ACCESS_KEY" ] || [ -z "$SECRET_KEY" ]; then
  echo "R2 credentials may not be empty." >&2
  exit 1
fi

TMP_ENV=$(mktemp)
TMP_ACCESS=$(mktemp)
TMP_SECRET=$(mktemp)
trap 'rm -f "$TMP_ENV" "$TMP_ACCESS" "$TMP_SECRET"' EXIT
cat > "$TMP_ENV" <<EOF
RAW_ARCHIVE_DIR=/var/lib/work-history/raw-archive
RAW_ARCHIVE_AGE_RECIPIENT=$AGE_RECIPIENT
RAW_ARCHIVE_R2_ENDPOINT=https://$ACCOUNT_ID.r2.cloudflarestorage.com
RAW_ARCHIVE_R2_BUCKET=$BUCKET
RAW_ARCHIVE_R2_PREFIX=raw/v1
RAW_ARCHIVE_LOOKAHEAD_DAYS=7
RAW_ARCHIVE_BATCH_SIZE=5000
EOF
printf '%s' "$ACCESS_KEY" > "$TMP_ACCESS"
printf '%s' "$SECRET_KEY" > "$TMP_SECRET"
install -o root -g workhistory -m 0640 "$TMP_ENV" "$ENV_FILE"
install -o root -g root -m 0600 "$TMP_ACCESS" "$ACCESS_FILE"
install -o root -g root -m 0600 "$TMP_SECRET" "$SECRET_FILE"
unset ACCESS_KEY SECRET_KEY

systemctl daemon-reload
systemctl enable --now work-history-archive.timer work-history-archive-verify.timer
systemctl start work-history-archive.service
systemctl --no-pager --full status work-history-archive.service || true
test "$(systemctl show work-history-archive.service --property=Result --value)" = success

echo "Raw archive configured. Run an initial archive with:"
echo "  systemctl start work-history-archive-initial.service"
