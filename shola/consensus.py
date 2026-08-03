"""Deriving the most likely translation from volunteer picks.

Every verdict is a vote. A vote for one of the machine's options and a vote for
a volunteer's own wording count the same, which is the point: if three people
independently type the same thing the machine never proposed, that wording wins.

Nothing here is stored as truth. Consensus is always computed from the current
votes, so it improves as volunteers arrive and can never go stale.
"""

import unicodedata
from collections import Counter, defaultdict

from .models import Candidate, Evaluation, Word, db


def normalise(text):
    """Fold case and Unicode form so "Ɔdɔ" and "ɔdɔ" are one vote, not two.

    NFC matters here: ɛ typed as e + combining mark must match a precomposed ɛ,
    or the same answer from two phones would split the vote.
    """
    return unicodedata.normalize("NFC", (text or "").strip()).casefold()


def tally(word_id, language):
    """Vote counts for one word in one language, best first."""
    evals = (Evaluation.query
             .filter_by(word_id=word_id, language=language, skipped=False)
             .all())
    counts = Counter()
    display = {}
    for ev in evals:
        text = ev.chosen_text
        if not text:
            continue
        key = normalise(text)
        counts[key] += 1
        display.setdefault(key, text.strip())

    total = sum(counts.values())
    ranked = []
    for key, n in counts.most_common():
        ranked.append({"text": display[key], "votes": n,
                       "share": n / total if total else 0.0})
    return {"votes": total, "voters": len(evals), "ranked": ranked,
            "skips": sum(1 for e in evals if e.skipped)}


def best(word_id, language, min_votes=None):
    """The winning translation, or None while the evidence is too thin.

    A single vote is not consensus, so `min_votes` guards against publishing one
    person's opinion as the answer.
    """
    from .tiers import VOTES_TO_SETTLE
    min_votes = VOTES_TO_SETTLE if min_votes is None else min_votes
    t = tally(word_id, language)
    if not t["ranked"] or t["votes"] < min_votes:
        return None
    top = t["ranked"][0]
    runner_up = t["ranked"][1]["votes"] if len(t["ranked"]) > 1 else 0
    return {"text": top["text"], "votes": top["votes"], "share": top["share"],
             "total_votes": t["votes"], "unanimous": len(t["ranked"]) == 1,
             "margin": top["votes"] - runner_up}


def export_rows(language, min_votes=None):
    """Yield (phrase, agreed translation, votes, share, total) for export."""
    word_ids = [r[0] for r in db.session.query(Evaluation.word_id)
                .filter_by(language=language, skipped=False).distinct()]
    for wid in word_ids:
        agreed = best(wid, language, min_votes=min_votes)
        if not agreed:
            continue
        word = db.session.get(Word, wid)
        yield (word.phrase, agreed["text"], agreed["votes"],
               round(agreed["share"], 3), agreed["total_votes"])


def language_progress():
    """Per-language counts for the stats board."""
    out = defaultdict(lambda: {"verdicts": 0, "words": 0, "agreed": 0})
    rows = (db.session.query(Evaluation.language, Evaluation.word_id,
                             db.func.count(Evaluation.id))
            .group_by(Evaluation.language, Evaluation.word_id)
            .all())
    for language, _word_id, n in rows:
        out[language]["verdicts"] += n
        out[language]["words"] += 1
        from .tiers import VOTES_TO_SETTLE
        if n >= VOTES_TO_SETTLE:
            out[language]["agreed"] += 1
    return dict(out)


def candidate_by_id(candidate_id):
    return db.session.get(Candidate, candidate_id)


def verified_count(language, min_votes=None):
    """How many words in this language have reached agreement."""
    from .models import WordState
    return WordState.query.filter_by(language=language, done=True).count()


def sample_entries(language, limit=3):
    """A few real verified entries, for the documentation page."""
    out = []
    for row in export_rows(language):
        phrase, text, votes, share, total = row
        out.append({"phrase": phrase, "translation": text, "votes": votes,
                    "agreement": round(share, 2), "total_votes": total})
        if len(out) >= limit:
            break
    return out
