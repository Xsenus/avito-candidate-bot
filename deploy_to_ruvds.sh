#!/usr/bin/env bash
set -euo pipefail

SERVER="${1:-194.32.248.49}"
USER="${2:-root}"
PASSWORD="${3:-${DEPLOY_PASSWORD:-}}"
REMOTE_DIR="/root/avito_bot"
LOCAL_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ -z "$PASSWORD" ]; then
  echo "Pass the server password as argument 3 or set DEPLOY_PASSWORD."
  exit 1
fi

if ! command -v sshpass >/dev/null 2>&1; then
  echo "sshpass не найден. Установите его: apt-get install -y sshpass"
  exit 1
fi

sshpass -p "$PASSWORD" ssh -o StrictHostKeyChecking=no "$USER@$SERVER" "mkdir -p $REMOTE_DIR"
sshpass -p "$PASSWORD" scp -o StrictHostKeyChecking=no -r "$LOCAL_DIR"/. "$USER@$SERVER:$REMOTE_DIR/"

sshpass -p "$PASSWORD" ssh -o StrictHostKeyChecking=no "$USER@$SERVER" "bash -lc '
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3 python3-pip python3-venv git
cd $REMOTE_DIR
if [ ! -d venv ]; then python3 -m venv venv; fi
source venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if [ ! -f .env ]; then cp .env.example .env; fi
if pgrep -f "poller.py" >/dev/null 2>&1; then pkill -f "poller.py" || true; fi
nohup python poller.py > poller.log 2>&1 &
echo \"Бот запущен в фоне. Лог: $REMOTE_DIR/poller.log\"
'"
