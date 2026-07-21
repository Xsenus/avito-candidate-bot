#!/usr/bin/env bash
set -euo pipefail

SERVER="${1:?Usage: DEPLOY_PASSWORD=... ./deploy_to_ruvds.sh SERVER [USER]}"
USER="${2:-root}"
PASSWORD="${DEPLOY_PASSWORD:-}"
REMOTE_DIR="/root/avito_bot"
RELEASE_ROOT="/root/avito_bot_releases"
ROLLBACK_ROOT="/root/avito_bot_rollbacks"
LOCAL_DIR="$(cd "$(dirname "$0")" && pwd)"
COMMIT="$(git -C "$LOCAL_DIR" rev-parse --short HEAD 2>/dev/null || echo source)"
TAG="$(date -u +%Y%m%dT%H%M%SZ)-$COMMIT"
REMOTE_ARCHIVE="$RELEASE_ROOT/$TAG.tar.gz"

if [ -z "$PASSWORD" ]; then
  echo "Set DEPLOY_PASSWORD in the current shell."
  exit 1
fi
if ! command -v sshpass >/dev/null 2>&1; then
  echo "sshpass is required."
  exit 1
fi

export SSHPASS="$PASSWORD"
SSH=(sshpass -e ssh -o StrictHostKeyChecking=accept-new "$USER@$SERVER")

echo "Preparing release $TAG"
"${SSH[@]}" "mkdir -p '$RELEASE_ROOT' '$ROLLBACK_ROOT'"
tar \
  --exclude=.env \
  --exclude=.evn \
  --exclude=.git \
  --exclude=venv \
  --exclude=.venv \
  --exclude=data \
  --exclude='*.log' \
  --exclude='__pycache__' \
  --exclude='.pytest_cache' \
  --exclude='.pytest_tmp' \
  -C "$LOCAL_DIR" -czf - . \
  | "${SSH[@]}" "cat > '$REMOTE_ARCHIVE'"

"${SSH[@]}" "bash -s -- '$TAG' '$REMOTE_ARCHIVE'" <<'REMOTE_SCRIPT'
set -euo pipefail

tag="$1"
archive="$2"
current="/root/avito_bot"
release_root="/root/avito_bot_releases"
rollback_root="/root/avito_bot_rollbacks"
release="$release_root/$tag"
rollback="$rollback_root/$tag"

case "$release" in
  /root/avito_bot_releases/*) ;;
  *) echo "Unsafe release path: $release"; exit 1 ;;
esac
case "$rollback" in
  /root/avito_bot_rollbacks/*) ;;
  *) echo "Unsafe rollback path: $rollback"; exit 1 ;;
esac

test -d "$current"
test -f "$current/.env"
test -d "$current/venv"
test -f "$archive"
test ! -e "$release"
test ! -e "$rollback"

mkdir -p "$release"
tar -xzf "$archive" -C "$release"
cp -a "$current/.env" "$release/.env"
cp -a "$current/venv" "$release/venv"
if [ -d "$current/data" ]; then
  cp -a "$current/data" "$release/data"
else
  mkdir -p "$release/data"
fi

"$release/venv/bin/python" -m pip install -q -r "$release/requirements.txt"
"$release/venv/bin/python" -m playwright install chromium
cd "$release"
"$release/venv/bin/python" -m pytest -q
"$release/venv/bin/python" preflight.py

systemctl stop avito-bot

# Copy SQLite once more while the old process is stopped, so no state is lost.
if [ -d "$current/data" ]; then
  case "$release/data" in
    /root/avito_bot_releases/*/data) ;;
    *) echo "Unsafe data refresh path: $release/data"; exit 1 ;;
  esac
  rm -rf -- "$release/data"
  cp -a "$current/data" "$release/data"
fi

mv -- "$current" "$rollback"
mv -- "$release" "$current"
systemctl daemon-reload
systemctl reset-failed avito-bot || true
started_at="$(date --iso-8601=seconds)"
start_ok=true
if ! systemctl start avito-bot; then
  start_ok=false
fi
sleep 10
restart_count="$(systemctl show avito-bot -p NRestarts --value)"
auth_ok=false
if journalctl -u avito-bot --since "$started_at" --no-pager | grep -q "Avito auth OK"; then
  auth_ok=true
fi

if [ "$start_ok" != "true" ] \
  || ! systemctl is-active --quiet avito-bot \
  || [ "$restart_count" != "0" ] \
  || [ "$auth_ok" != "true" ]; then
  echo "New release failed to start; restoring $rollback" >&2
  systemctl stop avito-bot || true
  failed="$release_root/${tag}-failed"
  mv -- "$current" "$failed"
  mv -- "$rollback" "$current"
  systemctl daemon-reload
  systemctl start avito-bot
  exit 1
fi

echo "Release active: $current"
echo "Rollback copy: $rollback"
systemctl show avito-bot -p MainPID -p NRestarts --no-pager
REMOTE_SCRIPT

echo "Deployment complete: $USER@$SERVER:$REMOTE_DIR"
echo "Release tag: $TAG"
