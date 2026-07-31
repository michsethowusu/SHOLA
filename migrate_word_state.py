#!/usr/bin/env python3
"""Move vote state from Word to per-language WordState.

Word.done was shared across all four languages, so two Twi speakers agreeing
closed the word for Ga, Ewe and Dagbani speakers who had never seen it. This
creates word_state and rebuilds it from the verdicts on record, counting votes
within each language.

    ./.venv/bin/python migrate_word_state.py
"""

from dotenv import load_dotenv
from sqlalchemy import inspect, text

load_dotenv()

from shola import create_app                                  # noqa: E402
from shola.models import Evaluation, Word, WordState, db       # noqa: E402
from shola.tiers import refresh_word, tier_progress           # noqa: E402


def main():
    app = create_app()
    with app.app_context():
        insp = inspect(db.engine)
        if "word_state" not in insp.get_table_names():
            WordState.__table__.create(db.engine)
            print("created word_state")
        else:
            print("word_state already exists")

        pairs = db.session.query(Evaluation.word_id,
                                 Evaluation.language).distinct().all()
        print(f"rebuilding state for {len(pairs):,} word/language pairs ...")
        for i, (word_id, language) in enumerate(pairs, 1):
            refresh_word(word_id, language, commit=False)
            if i % 2000 == 0:
                db.session.commit()
                print(f"  {i:,}")
        db.session.commit()

        # The old flags are no longer read; clear them so nothing is tempted to.
        db.session.execute(text(
            "UPDATE words SET done = 0, contested = 0, top_votes = 0, "
            "total_votes = 0"))
        db.session.commit()
        print("cleared the old shared flags on words")

        for code, lang in app.config["LANGUAGES"].items():
            rows = tier_progress(code)
            done = sum(r["done"] for r in rows)
            print(f"  {lang['name']:9s} {done:>7,} confirmed")


if __name__ == "__main__":
    main()
