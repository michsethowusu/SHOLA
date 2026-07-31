#!/usr/bin/env bash
# Deploy to the H200. Use this rather than a hand-typed rsync.
#
# The exclude list is the point: --delete once removed the server's .venv
# because it does not exist locally, which took the site down. Anything that
# lives only on the server belongs here.
set -euo pipefail

HOST="${SHOLA_HOST:-h200}"
DEST="${SHOLA_DEST:-/mnt/volume_d2wey28/projects/shola}"

rsync -az --delete \
  --exclude='.git' \
  --exclude='.venv' \
  --exclude='.env' \
  --exclude='instance' \
  --exclude='seed' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='*.log' \
  ./ "$HOST:$DEST/"

ssh "$HOST" "sudo systemctl restart shola.service"
sleep 6
ssh "$HOST" "systemctl is-active shola.service && curl -s -m 5 http://127.0.0.1:8110/healthz"
echo
echo "https://shola.inkika.org"
