#!/usr/bin/env bash
# Deploy SHOLA.
#
# The app runs on the Coolify VPS and builds from GitHub, so deploying means
# push, then ask Coolify to build. It used to rsync to the H200; after the move
# that quietly deployed to a machine no longer serving traffic, so the old path
# is gone rather than left as a footgun.
#
#   COOLIFY_TOKEN=... ./deploy.sh
set -euo pipefail

APP_UUID="${SHOLA_APP_UUID:-fj2jijjl9gavv683vcfuhuep}"
COOLIFY_URL="${COOLIFY_URL:-http://82.29.179.121:8000}"
SITE="${SHOLA_SITE:-https://shola.inkika.org}"
TOKEN="${COOLIFY_TOKEN:-}"

if [ -z "$TOKEN" ]; then
  echo "COOLIFY_TOKEN is not set" >&2
  exit 1
fi

if [ -n "$(git status --porcelain)" ]; then
  echo "working tree is dirty; commit before deploying" >&2
  git status --short >&2
  exit 1
fi

echo "pushing to GitHub"
git push origin main

echo "asking Coolify to build"
curl -sS -m 60 -H "Authorization: Bearer $TOKEN" \
  "$COOLIFY_URL/api/v1/deploy?uuid=$APP_UUID" | head -c 200
echo

echo -n "waiting for the site"
for _ in $(seq 1 60); do
  if [ "$(curl -s -o /dev/null -w '%{http_code}' -m 10 "$SITE/healthz")" = "200" ]; then
    echo " — up"
    curl -s -m 10 "$SITE/healthz"; echo
    exit 0
  fi
  echo -n "."
  sleep 10
done
echo " — did not come back up in time; check Coolify" >&2
exit 1
