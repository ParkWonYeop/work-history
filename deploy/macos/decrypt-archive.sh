#!/bin/zsh
set -euo pipefail

SCRIPT_DIR=${0:A:h}
SERVICE=com.workhistory.raw-archive
ACCOUNT=age-identity
if (( $# != 2 )); then
  echo "Usage: $0 ARCHIVE.jsonl.zst.age OUTPUT.jsonl" >&2
  exit 2
fi
INPUT=${1:A}
OUTPUT=${2:A}
if [[ -L "$INPUT" || ! -f "$INPUT" ]]; then
  echo "Archive input is missing or unsafe." >&2
  exit 1
fi
if [[ -e "$OUTPUT" || -L "$OUTPUT" ]]; then
  echo "Refusing to replace an existing output file." >&2
  exit 1
fi
if [[ ! -d "$OUTPUT:h" ]]; then
  echo "Output parent directory does not exist." >&2
  exit 1
fi
for executable in age zstd python3 security; do
  if ! command -v "$executable" >/dev/null 2>&1; then
    echo "Required command is missing: $executable" >&2
    exit 1
  fi
done

umask 077
TMP_IDENTITY=$(mktemp)
TMP_OUTPUT=$(mktemp "$OUTPUT:h/.work-history-archive.XXXXXX")
cleanup() {
  rm -f "$TMP_IDENTITY" "$TMP_OUTPUT"
}
trap cleanup EXIT HUP INT TERM
security find-generic-password -a "$ACCOUNT" -s "$SERVICE" -w > "$TMP_IDENTITY"
age -d -i "$TMP_IDENTITY" "$INPUT" | zstd -q -d -c > "$TMP_OUTPUT"
python3 "$SCRIPT_DIR/verify-archive.py" "$TMP_OUTPUT"
chmod 0600 "$TMP_OUTPUT"
mv "$TMP_OUTPUT" "$OUTPUT"
echo "Decrypted and verified archive: $OUTPUT"
