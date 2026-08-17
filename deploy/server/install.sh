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

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  ca-certificates \
  curl \
  postgresql \
  postgresql-client \
  python3 \
  python3-venv

if ! getent group workhistory >/dev/null; then
  groupadd --system workhistory
fi
if ! getent passwd workhistory >/dev/null; then
  useradd --system --gid workhistory --home-dir /var/lib/work-history \
    --create-home --shell /usr/sbin/nologin workhistory
fi

install -d -o root -g root -m 0755 "$APP_ROOT" "$APP_ROOT/bin"
install -d -o workhistory -g workhistory -m 0750 /var/lib/work-history
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
  --exclude=.venv \
  --exclude=.pytest_cache \
  --exclude='*.egg-info' \
  --exclude='__pycache__' \
  -cf - . | tar -C "$SOURCE_COPY" -xf -

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

if [ ! -f /etc/work-history/server.env ]; then
  install -o root -g workhistory -m 0640 \
    "$SOURCE_COPY/deploy/server/server.env.example" /etc/work-history/server.env
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
chmod 0600 /etc/work-history/credentials/read-api-token \
  /etc/work-history/credentials/atlassian-api-token \
  /etc/work-history/credentials/slack-user-token \
  /etc/work-history/credentials/slack-app-token

install -o root -g root -m 0755 "$SOURCE_COPY/deploy/server/backup.sh" "$APP_ROOT/bin/backup.sh"
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
systemctl enable --now work-history-api.service
systemctl enable --now work-history-backup.timer work-history-cleanup.timer

echo "Server installed. Edit /etc/work-history/server.env and run configure-atlassian.sh."
echo "The read API token is stored at /etc/work-history/credentials/read-api-token."
