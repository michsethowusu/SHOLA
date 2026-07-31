#!/usr/bin/env python3
"""One-off migration from fixed allocation to the tiered work queue.

Adds the new columns, loads raw occurrence counts, assigns tiers, and clears
the old fixed allocations. That last step matters: a pre-existing pending
assignment counts as a live lease, so leaving 1000 stale rows in place would
make the queue believe every word is already spoken for and hand out nothing.

    ./.venv/bin/python migrate_to_tiers.py seed/ghana-nouns.csv
"""

import csv
import sys

from dotenv import load_dotenv
from sqlalchemy import inspect, text

load_dotenv()

from shola import create_app                      # noqa: E402
from shola.models import Assignment, Evaluation, Word, db   # noqa: E402
from shola.tiers import assign_tiers, refresh_word, tier_progress  # noqa: E402

NEW_COLUMNS = {
    "occurrences": "INTEGER NOT NULL DEFAULT 0",
    "tier": "INTEGER NOT NULL DEFAULT 5",
    "top_votes": "INTEGER NOT NULL DEFAULT 0",
    "total_votes": "INTEGER NOT NULL DEFAULT 0",
    "done": "BOOLEAN NOT NULL DEFAULT 0",
    "contested": "BOOLEAN NOT NULL DEFAULT 0",
}


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: migrate_to_tiers.py <ghana-nouns.csv>")
    source = sys.argv[1]

    app = create_app()
    with app.app_context():
        insp = inspect(db.engine)

        have = {c["name"] for c in insp.get_columns("words")}
        for name, spec in NEW_COLUMNS.items():
            if name not in have:
                db.session.execute(text(f"ALTER TABLE words ADD COLUMN {name} {spec}"))
                print(f"  added words.{name}")
        have_a = {c["name"] for c in insp.get_columns("assignments")}
        if "expires_at" not in have_a:
            db.session.execute(text("ALTER TABLE assignments ADD COLUMN expires_at DATETIME"))
            print("  added assignments.expires_at")
        db.session.commit()

        print("loading occurrence counts ...")
        counts = {}
        with open(source, newline="", encoding="utf-8") as fh:
            for rec in csv.DictReader(fh):
                total = 0
                for col in ("news_count", "research_count", "speech_count"):
                    try:
                        total += int(float(rec.get(col) or 0))
                    except (TypeError, ValueError):
                        pass
                counts[rec["phrase"]] = total
        print(f"  {len(counts):,} phrases")

        print("writing counts ...")
        n = 0
        for word in Word.query.yield_per(2000):
            c = counts.get(word.phrase)
            if c is not None and word.occurrences != c:
                word.occurrences = c
                n += 1
            if n and n % 20000 == 0:
                db.session.commit()
                print(f"  {n:,}")
        db.session.commit()
        print(f"  set occurrences on {n:,} words")

        print("assigning tiers ...")
        assign_tiers()

        stale = Assignment.query.filter_by(status="pending").count()
        if stale:
            Assignment.query.delete()
            db.session.commit()
            print(f"  cleared {stale:,} pre-existing allocations "
                  "(they would have blocked the queue)")

        ids = [r[0] for r in db.session.query(db.distinct(Evaluation.word_id))]
        for word in Word.query.filter(Word.id.in_(ids)).all():
            refresh_word(word, commit=False)
        db.session.commit()
        print(f"  refreshed vote state on {len(ids):,} words")

        print("\ntiers now:")
        for row in tier_progress():
            print(f"  tier {row['tier']}  {row['total']:>8,} words  "
                  f"{row['done']:>6,} settled")


if __name__ == "__main__":
    main()
