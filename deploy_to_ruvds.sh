#!/usr/bin/env bash
set -euo pipefail

SERVER="${1:?Usage: DEPLOY_PASSWORD=... ./deploy_to_ruvds.sh SERVER [USER]}"
USER="${2:-root}"
PASSWORD="${DEPLOY_PASSWORD:-}"
REMOTE_DIR="/root/avito_bot"
LOCAL_DIR="$(cd "$(dirname "$0")" && pwd)"

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

"${SSH[@]}" "mkdir -p '$REMOTE_DIR'"
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
  -C "$LOCAL_DIR" -czf - . \
  | "${SSH[@]}" "tar -xzf - -C '$REMOTE_DIR'"

"${SSH[@]}" "cd '$REMOTE_DIR' && chmod +x setup_server.sh && ./setup_server.sh"
echo "Deployment complete: $USER@$SERVER:$REMOTE_DIR"
