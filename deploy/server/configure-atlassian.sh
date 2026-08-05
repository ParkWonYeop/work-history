#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root inside the work-history LXC." >&2
  exit 1
fi

ENV_FILE=/etc/work-history/server.env
CREDENTIAL_FILE=/etc/work-history/credentials/atlassian-api-token
if [ ! -f "$ENV_FILE" ]; then
  echo "Install the server first." >&2
  exit 1
fi

printf 'Atlassian site URL (https://example.atlassian.net): '
IFS= read -r SITE_URL
printf 'Atlassian account email: '
IFS= read -r ACCOUNT_EMAIL
printf 'Atlassian API token: '
restore_terminal() {
  stty echo 2>/dev/null || true
}
trap restore_terminal EXIT HUP INT TERM
stty -echo
IFS= read -r API_TOKEN
stty echo
trap - EXIT HUP INT TERM
printf '\n'

case "$SITE_URL" in
  https://*.atlassian.net) ;;
  *) echo "Expected an https://*.atlassian.net URL." >&2; exit 1 ;;
esac
case "$ACCOUNT_EMAIL" in
  *@*) ;;
  *) echo "Invalid email address." >&2; exit 1 ;;
esac
if [ -z "$API_TOKEN" ]; then
  echo "Token may not be empty." >&2
  exit 1
fi

TMP_ENV=$(mktemp)
trap 'rm -f "$TMP_ENV"' EXIT
awk '!/^ATLASSIAN_SITE_URL=/ && !/^ATLASSIAN_EMAIL=/' "$ENV_FILE" > "$TMP_ENV"
printf 'ATLASSIAN_SITE_URL=%s\nATLASSIAN_EMAIL=%s\n' "$SITE_URL" "$ACCOUNT_EMAIL" >> "$TMP_ENV"
install -o root -g workhistory -m 0640 "$TMP_ENV" "$ENV_FILE"

TMP_TOKEN=$(mktemp)
trap 'rm -f "$TMP_ENV" "$TMP_TOKEN"' EXIT
printf '%s' "$API_TOKEN" > "$TMP_TOKEN"
install -o root -g root -m 0600 "$TMP_TOKEN" "$CREDENTIAL_FILE"
unset API_TOKEN

systemctl daemon-reload
systemctl enable --now work-history-sync.timer work-history-reconcile.timer
systemctl start work-history-sync.service
systemctl --no-pager --full status work-history-sync.service || true
