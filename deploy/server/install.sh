#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this installer as root inside the dedicated LXC." >&2
  exit 1
fi

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SOURCE_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
APP_ROOT=/opt/work-history
SOURCE_COPY=$APP_ROOT/source

if [ ! -f "$SOURCE_DIR/pyproject.toml" ] || [ ! -f "$SOURCE_DIR/alembic.ini" ]; then
  echo "Project files are missing." >&2
  exit 1
fi
SENSITIVE_FILE=$(find "$SOURCE_DIR" \
  \( -path "$SOURCE_DIR/.git" -o -path "$SOURCE_DIR/.venv" \) -prune -o \
  -type f \( \
    -name '*.age' -o \
    -name '*.jsonl' -o \
    -name 'work-history-age-recovery-key*' -o \
    -name 'r2-access-key-id' -o \
    -name 'r2-secret-access-key' -o \
    -name 'r2-credentials*' \
  \) -print -quit)
if [ -n "$SENSITIVE_FILE" ]; then
  echo "Refusing to install while a raw archive, recovery key, or R2 credential is in the source tree." >&2
  exit 1
fi

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  age \
  ca-certificates \
  curl \
  nftables \
  postgresql \
  postgresql-client \
  python3 \
  python3-venv \
  zstd

if ! getent group workhistory >/dev/null; then
  groupadd --system workhistory
fi
if ! getent passwd workhistory >/dev/null; then
  useradd --system --gid workhistory --home-dir /var/lib/work-history \
    --create-home --shell /usr/sbin/nologin workhistory
fi

install -d -o root -g root -m 0755 "$APP_ROOT" "$APP_ROOT/bin"
install -d -o workhistory -g workhistory -m 0750 /var/lib/work-history
install -d -o workhistory -g workhistory -m 0700 /var/lib/work-history/raw-archive
install -d -o workhistory -g workhistory -m 0700 /var/backups/work-history
install -d -o root -g root -m 0750 /etc/work-history
install -d -o root -g root -m 0700 /etc/work-history/credentials

case "$SOURCE_COPY" in
  /opt/work-history/source) ;;
  *) echo "Unexpected application source path." >&2; exit 1 ;;
esac
if [ -L "$SOURCE_COPY" ]; then
  echo "Refusing to replace a symbolic-link source directory." >&2
  exit 1
fi
rm -rf -- "$SOURCE_COPY"
install -d -o root -g root -m 0755 "$SOURCE_COPY"
tar -C "$SOURCE_DIR" \
  --exclude=.git \
  --exclude=.venv \
  --exclude=.pytest_cache \
  --exclude=.ruff_cache \
  --exclude=.report-tmp \
  --exclude=exports \
  --exclude='.env*' \
  --exclude='*.age' \
  --exclude='*.jsonl' \
  --exclude='*.db' \
  --exclude='*.db-shm' \
  --exclude='*.db-wal' \
  --exclude='*.egg-info' \
  --exclude='__pycache__' \
  --exclude='work-history-age-recovery-key*' \
  --exclude='r2-access-key-id' \
  --exclude='r2-secret-access-key' \
  --exclude='r2-credentials*' \
  -cf - . | tar --no-overwrite-dir -C "$SOURCE_COPY" -xf -

python3 -m venv "$APP_ROOT/venv"
"$APP_ROOT/venv/bin/pip" install --upgrade pip
"$APP_ROOT/venv/bin/pip" install "$SOURCE_COPY"

systemctl enable --now postgresql.service
if ! runuser -u postgres -- psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='workhistory'" | grep -q 1; then
  runuser -u postgres -- createuser --no-createdb --no-createrole --no-superuser workhistory
fi
if ! runuser -u postgres -- psql -tAc "SELECT 1 FROM pg_database WHERE datname='workhistory'" | grep -q 1; then
  runuser -u postgres -- createdb \
    --owner=workhistory \
    --encoding=UTF8 \
    --locale=C.utf8 \
    --template=template0 \
    workhistory
fi
if ! runuser -u postgres -- psql -tAc "SELECT 1 FROM pg_database WHERE datname='workhistory_restore_test'" | grep -q 1; then
  runuser -u postgres -- createdb \
    --owner=workhistory \
    --encoding=UTF8 \
    --locale=C.utf8 \
    --template=template0 \
    workhistory_restore_test
fi

if [ ! -f /etc/work-history/server.env ]; then
  install -o root -g workhistory -m 0640 \
    "$SOURCE_COPY/deploy/server/server.env.example" /etc/work-history/server.env
fi
if [ ! -f /etc/work-history/archive.env ]; then
  install -o root -g workhistory -m 0640 \
    "$SOURCE_COPY/deploy/server/archive.env.example" /etc/work-history/archive.env
fi
if [ ! -f /etc/work-history/credentials/read-api-token ]; then
  umask 077
  openssl rand -base64 32 | tr -d '\n' > /etc/work-history/credentials/read-api-token
fi
if [ ! -f /etc/work-history/credentials/atlassian-api-token ]; then
  umask 077
  : > /etc/work-history/credentials/atlassian-api-token
fi
if [ ! -f /etc/work-history/credentials/slack-user-token ]; then
  umask 077
  : > /etc/work-history/credentials/slack-user-token
fi
if [ ! -f /etc/work-history/credentials/slack-app-token ]; then
  umask 077
  : > /etc/work-history/credentials/slack-app-token
fi
if [ ! -f /etc/work-history/credentials/r2-access-key-id ]; then
  umask 077
  : > /etc/work-history/credentials/r2-access-key-id
fi
if [ ! -f /etc/work-history/credentials/r2-secret-access-key ]; then
  umask 077
  : > /etc/work-history/credentials/r2-secret-access-key
fi
chmod 0600 /etc/work-history/credentials/read-api-token \
  /etc/work-history/credentials/atlassian-api-token \
  /etc/work-history/credentials/slack-user-token \
  /etc/work-history/credentials/slack-app-token \
  /etc/work-history/credentials/r2-access-key-id \
  /etc/work-history/credentials/r2-secret-access-key

install -o root -g root -m 0755 "$SOURCE_COPY/deploy/server/backup.sh" "$APP_ROOT/bin/backup.sh"
install -o root -g root -m 0755 \
  "$SOURCE_COPY/deploy/server/verify-backup.sh" "$APP_ROOT/bin/verify-backup.sh"
for unit in "$SOURCE_COPY"/deploy/server/systemd/*; do
  install -o root -g root -m 0644 "$unit" "/etc/systemd/system/$(basename "$unit")"
done

(
  cd "$SOURCE_COPY"
  runuser -u workhistory -- env \
    DATABASE_URL='postgresql+psycopg:///workhistory?host=/var/run/postgresql' \
    "$APP_ROOT/venv/bin/alembic" -c "$SOURCE_COPY/alembic.ini" upgrade head
)

systemctl daemon-reload
systemctl enable work-history-api.service
systemctl restart work-history-api.service
systemctl enable --now work-history-backup.timer work-history-cleanup.timer

echo "Server installed. Edit /etc/work-history/server.env and run configure-atlassian.sh."
echo "The read API token is stored at /etc/work-history/credentials/read-api-token."
