#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this script as root inside the dedicated LXC." >&2
  exit 1
fi
if [ "$#" -ne 1 ]; then
  echo "Usage: $0 NPM_IPV4" >&2
  exit 2
fi

NPM_IP=$1
OLD_IFS=$IFS
IFS=.
set -- $NPM_IP
IFS=$OLD_IFS
if [ "$#" -ne 4 ]; then
  echo "NPM address must be an IPv4 address" >&2
  exit 1
fi
for octet in "$@"; do
  case "$octet" in
    ''|*[!0-9]*) echo "NPM address must be an IPv4 address" >&2; exit 1 ;;
  esac
  if [ "$octet" -gt 255 ]; then
    echo "NPM address must be an IPv4 address" >&2
    exit 1
  fi
done

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
TEMPLATE=$SCRIPT_DIR/work-history.nft.template
RULES=/etc/work-history/work-history.nft
if [ ! -f "$TEMPLATE" ] || [ -L "$TEMPLATE" ]; then
  echo "Firewall template is missing or unsafe" >&2
  exit 1
fi
if [ -L "$RULES" ]; then
  echo "Refusing to replace a symbolic-link firewall file" >&2
  exit 1
fi

TMP=$(mktemp /etc/work-history/work-history.nft.XXXXXX)
trap 'rm -f -- "$TMP"' EXIT HUP INT TERM
sed "s/__NPM_IP__/$NPM_IP/g" "$TEMPLATE" > "$TMP"
chmod 0600 "$TMP"
nft --check --file "$TMP"
install -o root -g root -m 0600 "$TMP" "$RULES"

systemctl enable work-history-firewall.service
systemctl restart work-history-firewall.service
systemctl --no-pager --full status work-history-firewall.service
