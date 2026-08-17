#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root inside the work-history LXC." >&2
  exit 1
fi

ENV_FILE=/etc/work-history/server.env
USER_TOKEN_FILE=/etc/work-history/credentials/slack-user-token
APP_TOKEN_FILE=/etc/work-history/credentials/slack-app-token
if [ ! -f "$ENV_FILE" ]; then
  echo "Install the server first." >&2
  exit 1
fi

printf 'Slack workspace URL (https://example-workspace.slack.com): '
IFS= read -r WORKSPACE_URL
printf 'Slack App ID (A...): '
IFS= read -r APP_ID
printf 'Initial history date [2026-04-01]: '
IFS= read -r HISTORY_START
HISTORY_START=${HISTORY_START:-2026-04-01}

restore_terminal() {
  stty echo 2>/dev/null || true
}
trap restore_terminal EXIT HUP INT TERM
printf 'Slack user token (xoxp-...): '
stty -echo
IFS= read -r USER_TOKEN
stty echo
printf '\nSlack app token (xapp-...): '
stty -echo
IFS= read -r APP_TOKEN
stty echo
trap - EXIT HUP INT TERM
printf '\n'

case "$WORKSPACE_URL" in
  https://*.slack.com|https://*.slack-gov.com) ;;
  *) echo "Expected an HTTPS Slack workspace URL." >&2; exit 1 ;;
esac
case "$APP_ID" in
  A*) ;;
  *) echo "Slack App ID must start with A." >&2; exit 1 ;;
esac
case "$USER_TOKEN" in
  xoxp-*) ;;
  *) echo "Expected a user OAuth token beginning with xoxp-." >&2; exit 1 ;;
esac
case "$APP_TOKEN" in
  xapp-*) ;;
  *) echo "Expected an app-level token beginning with xapp-." >&2; exit 1 ;;
esac
if ! date -d "$HISTORY_START" '+%F' >/dev/null 2>&1; then
  echo "Initial history date must use YYYY-MM-DD." >&2
  exit 1
fi

TMP_ENV=$(mktemp)
TMP_USER=$(mktemp)
TMP_APP=$(mktemp)
trap 'rm -f "$TMP_ENV" "$TMP_USER" "$TMP_APP"' EXIT
awk '
  !/^SLACK_WORKSPACE_URL=/ &&
  !/^SLACK_APP_ID=/ &&
  !/^SLACK_HISTORY_START=/
' "$ENV_FILE" > "$TMP_ENV"
printf 'SLACK_WORKSPACE_URL=%s\nSLACK_APP_ID=%s\nSLACK_HISTORY_START=%s\n' \
  "$WORKSPACE_URL" "$APP_ID" "$HISTORY_START" >> "$TMP_ENV"
printf '%s' "$USER_TOKEN" > "$TMP_USER"
printf '%s' "$APP_TOKEN" > "$TMP_APP"
install -o root -g workhistory -m 0640 "$TMP_ENV" "$ENV_FILE"
install -o root -g root -m 0600 "$TMP_USER" "$USER_TOKEN_FILE"
install -o root -g root -m 0600 "$TMP_APP" "$APP_TOKEN_FILE"
unset USER_TOKEN APP_TOKEN

systemctl daemon-reload
systemctl enable --now work-history-slack-socket.service work-history-slack-daily.timer
systemctl start work-history-slack-daily.service

BACKFILL_TO=$(TZ=Asia/Seoul date '+%Y-%m-%dT%H:%M:%S%:z')
"/opt/work-history/source/deploy/server/run-slack-backfill.sh" \
  "${HISTORY_START}T00:00:00+09:00" "$BACKFILL_TO" --no-block

echo "Slack configured. The historical backfill is running in the background."
echo "Inspect it with: journalctl -u work-history-slack-backfill.service -f"
