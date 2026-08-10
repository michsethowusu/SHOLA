#!/usr/bin/env bash
# Report the disk, seed the word list on first boot only, then hand over to
# gunicorn.
#
# The words are fetched from the published dataset rather than baked into the
# image or copied from another machine: 478,822 rows with translations is a
# 531 MB database, and rebuilding it from a 38 MB download is both smaller and
# reproducible on any host.
set -uo pipefail

SEED_DIR=/app/instance/seed
DB=/app/instance/shola.db
TRANSLATED_URL="${SHOLA_TRANSLATED_URL:-https://raw.githubusercontent.com/GhanaNLP/GhanaNouns/main/data/ghana-nouns-translated.csv.gz}"
SOURCE_URL="${SHOLA_SOURCE_URL:-https://raw.githubusercontent.com/GhanaNLP/GhanaNouns/main/data/ghana-nouns.csv}"

# There is no shell on this host, so the log is the only way to see the disk.
# A migration once failed with "database or disk is full" and it took a deploy
# to find that out.
echo "[disk] $(df -h /app/instance 2>/dev/null | tail -1)"
echo "[disk] largest under /app/instance:"
du -ah /app/instance 2>/dev/null | sort -rh | head -12 | sed 's/^/[disk]   /'

# Anything left behind by an interrupted backup. These are copies; the originals
# are in the volume and in R2, so removing them frees space without losing data.
if [ "${SHOLA_CLEAN_TEMP:-0}" = "1" ]; then
  echo "[clean] removing backup working files"
  rm -rf /app/instance/backup-tmp /tmp/shola-backup* 2>/dev/null || true
  find /app/instance -maxdepth 1 -name '*.db.tmp' -delete 2>/dev/null || true
  echo "[clean] $(df -h /app/instance 2>/dev/null | tail -1)"
fi

# Seeding is decided from a positive answer, never from a failure. The check used
# to swallow errors and treat them as "no words", so a full disk or a locked
# database made it re-import 478,822 rows on top of the ones already there.
words=$(python -c "
from wsgi import app
from shola.models import Word
with app.app_context():
    print(Word.query.count())
" 2>&1) && ok=1 || ok=0

if [ "$ok" != "1" ]; then
  echo "[seed] could not read the database, so NOT seeding. Reason:"
  echo "$words" | tail -3 | sed 's/^/[seed]   /'
elif [ "$words" -eq 0 ] 2>/dev/null; then
  if [ "${SHOLA_SKIP_SEED:-0}" = "1" ]; then
    echo "[seed] empty, but SHOLA_SKIP_SEED=1"
  elif [ -s "$DB" ] && [ "${SHOLA_FORCE_SEED:-0}" != "1" ]; then
    # An existing database reporting zero words is a broken read, not a fresh
    # install. Importing into it would double the corpus.
    echo "[seed] database exists but reports 0 words: refusing to seed."
    echo "[seed] set SHOLA_FORCE_SEED=1 if it really is empty."
  else
    echo "[seed] no database yet, fetching the dataset"
    mkdir -p "$SEED_DIR"
    [ -f "$SEED_DIR/translated.csv.gz" ] || curl -fsSL "$TRANSLATED_URL" -o "$SEED_DIR/translated.csv.gz"
    [ -f "$SEED_DIR/source.csv" ]        || curl -fsSL "$SOURCE_URL"     -o "$SEED_DIR/source.csv"
    echo "[seed] importing; this takes several minutes"
    flask --app wsgi shola import-words \
          --csv "$SEED_DIR/translated.csv.gz" \
          --freq-csv "$SEED_DIR/source.csv"
    flask --app wsgi shola assign-tiers
    echo "[seed] done"
    # The downloads are only needed once and are 140 MB together.
    rm -f "$SEED_DIR/translated.csv.gz" "$SEED_DIR/source.csv"
  fi
else
  echo "[seed] $words words already present, skipping"
fi

exec "$@"
