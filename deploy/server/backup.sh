#!/bin/sh
set -eu

BACKUP_DIR=/var/backups/work-history
case "$BACKUP_DIR" in
  /var/backups/work-history) ;;
  *) echo "Unexpected backup directory" >&2; exit 1 ;;
esac

if [ -L "$BACKUP_DIR" ] || [ ! -d "$BACKUP_DIR" ]; then
  echo "Backup directory is missing or is a symbolic link" >&2
  exit 1
fi

umask 077
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
PART="$BACKUP_DIR/workhistory-$STAMP.dump.part"
FINAL="$BACKUP_DIR/workhistory-$STAMP.dump"
pg_dump --format=custom --file="$PART" workhistory
mv "$PART" "$FINAL"
find "$BACKUP_DIR" -xdev -type f -name 'workhistory-*.dump' -mtime +13 -delete
