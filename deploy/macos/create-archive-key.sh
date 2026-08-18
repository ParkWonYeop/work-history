#!/bin/sh
set -eu

SERVICE=com.workhistory.raw-archive
ACCOUNT=age-identity
if [ "$#" -ne 1 ]; then
  echo "Usage: $0 /absolute/path/to/work-history-age-recovery-key.txt" >&2
  exit 2
fi
RECOVERY_PATH=$1
case "$RECOVERY_PATH" in
  /*) ;;
  *) echo "Recovery path must be absolute." >&2; exit 1 ;;
esac
if [ -e "$RECOVERY_PATH" ] || [ -L "$RECOVERY_PATH" ]; then
  echo "Refusing to replace an existing recovery file." >&2
  exit 1
fi
if [ ! -d "$(dirname "$RECOVERY_PATH")" ]; then
  echo "Recovery file parent directory does not exist." >&2
  exit 1
fi
for executable in age-keygen security; do
  if ! command -v "$executable" >/dev/null 2>&1; then
    echo "Required command is missing: $executable" >&2
    exit 1
  fi
done

umask 077
TMP_DIR=$(mktemp -d)
TMP_KEY=$TMP_DIR/identity.txt
cleanup() {
  rm -f "$TMP_KEY"
  rmdir "$TMP_DIR" 2>/dev/null || true
}
trap cleanup EXIT HUP INT TERM
age-keygen -o "$TMP_KEY" >/dev/null
SECRET_KEY=$(sed -n '/^AGE-SECRET-KEY-/p' "$TMP_KEY")
PUBLIC_KEY=$(age-keygen -y "$TMP_KEY")
if [ -z "$SECRET_KEY" ] || [ -z "$PUBLIC_KEY" ]; then
  echo "Failed to generate an age key pair." >&2
  exit 1
fi

security add-generic-password -U -a "$ACCOUNT" -s "$SERVICE" -w "$SECRET_KEY" >/dev/null
install -m 0600 "$TMP_KEY" "$RECOVERY_PATH"
unset SECRET_KEY

echo "Archive age recipient: $PUBLIC_KEY"
echo "Recovery key saved with mode 0600: $RECOVERY_PATH"
echo "Move the recovery key into a separate password manager before enabling R2 lock."
