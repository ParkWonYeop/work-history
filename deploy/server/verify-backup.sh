#!/bin/sh
set -eu

BACKUP_DIR=/var/backups/work-history
RESTORE_DB=workhistory_restore_test

if [ "$#" -ne 1 ]; then
  echo "Usage: $0 /var/backups/work-history/workhistory-TIMESTAMP.dump" >&2
  exit 2
fi
DUMP_FILE=$1

case "$DUMP_FILE" in
  /var/backups/work-history/workhistory-*.dump) ;;
  *) echo "Unexpected backup path" >&2; exit 1 ;;
esac
if [ -L "$BACKUP_DIR" ] || [ ! -d "$BACKUP_DIR" ]; then
  echo "Backup directory is missing or is a symbolic link" >&2
  exit 1
fi
if [ -L "$DUMP_FILE" ] || [ ! -f "$DUMP_FILE" ]; then
  echo "Backup is missing, not regular, or is a symbolic link" >&2
  exit 1
fi

pg_restore --list "$DUMP_FILE" >/dev/null
psql --dbname="$RESTORE_DB" --set=ON_ERROR_STOP=1 <<'SQL'
DROP SCHEMA IF EXISTS public CASCADE;
CREATE SCHEMA public AUTHORIZATION workhistory;
SQL
pg_restore \
  --exit-on-error \
  --no-owner \
  --no-privileges \
  --dbname="$RESTORE_DB" \
  "$DUMP_FILE"

psql --dbname="$RESTORE_DB" --set=ON_ERROR_STOP=1 <<'SQL'
DO $$
DECLARE
  required_table text;
BEGIN
  FOREACH required_table IN ARRAY ARRAY[
    'alembic_version',
    'source_identities',
    'artifacts',
    'artifact_versions',
    'activity_events',
    'raw_records',
    'sync_runs',
    'sync_cursors',
    'ingest_devices',
    'ingest_nonces',
    'ingest_batches',
    'generated_reports',
    'generated_report_versions'
  ] LOOP
    IF to_regclass('public.' || required_table) IS NULL THEN
      RAISE EXCEPTION 'required table is missing: %', required_table;
    END IF;
  END LOOP;
END
$$;
SELECT version_num FROM alembic_version;
SELECT count(*) FROM activity_events;
SELECT count(*) FROM generated_reports;
SQL

echo "Backup restore verification succeeded: $DUMP_FILE"
