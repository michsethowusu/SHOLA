#!/usr/bin/env bash
# Seed the word list on first boot, then hand over to gunicorn.
#
# The words are fetched from the published dataset rather than baked into the
# image or copied from another machine: 478,822 rows with translations is a
# 531 MB database, and rebuilding it from a 38 MB download is both smaller and
# reproducible on any host.
set -euo pipefail

SEED_DIR=/app/instance/seed
TRANSLATED_URL="${SHOLA_TRANSLATED_URL:-https://raw.githubusercontent.com/GhanaNLP/GhanaNouns/main/data/ghana-nouns-translated.csv.gz}"
SOURCE_URL="${SHOLA_SOURCE_URL:-https://raw.githubusercontent.com/GhanaNLP/GhanaNouns/main/data/ghana-nouns.csv}"

words=$(python -c "
from wsgi import app
from shola.models import Word, db
with app.app_context():
    print(Word.query.count())
" 2>/dev/null || echo 0)

if [ "${words:-0}" -eq 0 ] && [ "${SHOLA_SKIP_SEED:-0}" != "1" ]; then
  echo "[seed] no words yet, fetching the dataset"
  mkdir -p "$SEED_DIR"
  [ -f "$SEED_DIR/translated.csv.gz" ] || curl -fsSL "$TRANSLATED_URL" -o "$SEED_DIR/translated.csv.gz"
  [ -f "$SEED_DIR/source.csv" ]        || curl -fsSL "$SOURCE_URL"     -o "$SEED_DIR/source.csv"
  echo "[seed] importing; this takes several minutes"
  flask --app wsgi shola import-words \
        --csv "$SEED_DIR/translated.csv.gz" \
        --freq-csv "$SEED_DIR/source.csv"
  flask --app wsgi shola assign-tiers
  echo "[seed] done"
else
  echo "[seed] $words words already present, skipping"
fi

exec "$@"
