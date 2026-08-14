"""Reporting what volunteers answered, and how many chose each answer.

Every answer is published with its vote count. Nothing here rules on whether an
answer is correct or "verified": that judgement belongs to whoever builds on the
data, and it needs the counts to make it. Declaring a single winner and dropping
the rest would throw away exactly the evidence they need.

The leading answer is offered as a convenience. Where two wordings tie, both are
returned as leaders - a tie is a real result about the language, not a defect to
be broken by picking whichever the database happened to return first.

Nothing here is stored as truth. It is computed from the current votes, so it
improves as volunteers arrive and can never go stale.
"""

import unicodedata
from collections import Counter, defaultdict

from .models import Candidate, Evaluation, Project, Word, WordState, db


def normalise(text):
    """Fold case and Unicode form so "Ɔdɔ" and "ɔdɔ" are one vote, not two.

    NFC matters here: ɛ typed as e + combining mark must match a precomposed ɛ,
    or the same answer from two phones would split the vote.
    """
    return unicodedata.normalize("NFC", (text or "").strip()).casefold()


def tally(word_id, language):
    """Answer counts for one item in one language, most chosen first."""
    evals = (Evaluation.query
             .filter_by(word_id=word_id, language=language, skipped=False)
             .all())
    counts = Counter()
    display = {}
    typed_first = set()
    for ev in evals:
        text = ev.chosen_text
        if not text:
            continue
        key = normalise(text)
        counts[key] += 1
        display.setdefault(key, text.strip())
        if ev.custom_text:
            typed_first.add(key)

    # Where an answer came from, for anyone who wants to weigh them differently:
    # an option the project supplied, or a wording a volunteer wrote.
    supplied = {normalise(c.text) for c in
                Candidate.query.filter_by(word_id=word_id, language=language)
                .filter(Candidate.source != "volunteer").all()}

    total = sum(counts.values())
    ranked = []
    for key, n in counts.most_common():
        ranked.append({"text": display[key], "votes": n,
                       "share": n / total if total else 0.0,
                       "source": "option" if key in supplied else "volunteer"})
    return {"votes": total, "voters": len(evals), "ranked": ranked,
            "typed": len(typed_first),
            "skips": sum(1 for e in evals if e.skipped)}


def answers(word_id, language):
    """Every answer for one item in one language, most chosen first.

    Each entry says how the answer arrived - `option` for one that came with the
    project, `volunteer` for a wording somebody typed, which becomes an option
    for the next speaker. `chose` counts everyone who landed on that wording,
    however they got there.
    """
    t = tally(word_id, language)
    return t["ranked"]


def leaders(word_id, language, min_votes=None):
    """The answer or answers with the most votes.

    A list, because ties happen and are informative. `min_votes` filters out
    thin evidence for callers who want it; by default everything is reported and
    the caller decides.
    """
    ranked = answers(word_id, language)
    if not ranked:
        return []
    top = ranked[0]["votes"]
    if min_votes and top < min_votes:
        return []
    return [r for r in ranked if r["votes"] == top]


def best(word_id, language, min_votes=None):
    """The leading answer, or None.

    Kept for callers that want one row. A tie is reported through `tied`, so
    nothing silently picks a side.
    """
    top = leaders(word_id, language, min_votes=min_votes)
    if not top:
        return None
    t = tally(word_id, language)
    return {"text": top[0]["text"], "votes": top[0]["votes"],
            "share": top[0]["share"], "total_votes": t["votes"],
            "tied": [r["text"] for r in top] if len(top) > 1 else [],
            "unanimous": len(t["ranked"]) == 1,
            "margin": (top[0]["votes"] - t["ranked"][len(top)]["votes"]
                       if len(t["ranked"]) > len(top) else top[0]["votes"])}


def item_ids_with_answers(language, project_id=None):
    q = (db.session.query(Evaluation.word_id)
         .filter(Evaluation.language == language,
                 Evaluation.skipped.is_(False)))
    if project_id is not None:
        q = q.join(Word, Word.id == Evaluation.word_id).filter(
            Word.project_id == project_id)
    return [r[0] for r in q.distinct()]


def export_rows(language, min_votes=None, project_id=None):
    """Yield (item, leading answer, votes, share, total) per item.

    Where answers tie, the leaders are joined with " | " rather than one being
    picked: a tie is a fact about the language and hiding it would be a
    fabrication. Callers wanting every answer separately use answer_rows().
    """
    for wid in item_ids_with_answers(language, project_id=project_id):
        top = leaders(wid, language, min_votes=min_votes)
        if not top:
            continue
        t = tally(wid, language)
        word = db.session.get(Word, wid)
        yield (word.phrase, " | ".join(r["text"] for r in top),
               top[0]["votes"], round(top[0]["share"], 3), t["votes"])


def answer_rows(language, min_votes=None, project_id=None):
    """Yield every answer for every item: the full record, nothing dropped.

    (item, answer, votes, share, total answers, where it came from, leading?)

    This is the honest export. export_rows() is a convenience over it for people
    who want one row per item.
    """
    for wid in item_ids_with_answers(language, project_id=project_id):
        t = tally(wid, language)
        if not t["ranked"]:
            continue
        if min_votes and t["ranked"][0]["votes"] < min_votes:
            continue
        word = db.session.get(Word, wid)
        best_votes = t["ranked"][0]["votes"]
        for row in t["ranked"]:
            yield (word.phrase, row["text"], row["votes"],
                   round(row["share"], 3), t["votes"], row["source"],
                   row["votes"] == best_votes)


def typed_rows(language, project_id=None):
    """Every answer volunteers typed, one row each, with no verification.

    Kept separate from export_rows on purpose. These are contributions, not
    consensus: nobody agreed with them, and a project with no options produces
    nothing but these. Presenting them alongside verified answers would let a
    single unreviewed opinion pass as a settled one.
    """
    q = (db.session.query(Evaluation, Word)
         .join(Word, Word.id == Evaluation.word_id)
         .filter(Evaluation.language == language,
                 Evaluation.skipped.is_(False),
                 Evaluation.custom_text.isnot(None),
                 Evaluation.custom_text != ""))
    if project_id is not None:
        q = q.filter(Word.project_id == project_id)
    for ev, word in q.order_by(Word.position, Word.id, Evaluation.id).all():
        yield (word.phrase, ev.custom_text, ev.created_at.date().isoformat())


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


def verified_rows(language, project_id=None):
    """Items with a clear answer: the target reached and one wording ahead.

    A tie is not a verified answer, so it is left for the problem list. This is
    the endpoint for somebody who wants a dictionary rather than evidence.
    """
    from .models import WordState

    q = (db.session.query(WordState, Word)
         .join(Word, Word.id == WordState.word_id)
         .filter(WordState.language == language,
                 WordState.done.is_(True),
                 WordState.problem.is_(False)))
    if project_id is not None:
        q = q.filter(Word.project_id == project_id)
    for state, word in q.order_by(Word.position, Word.id).all():
        top = leaders(word.id, language)
        if len(top) != 1:
            continue
        yield {"item": word.phrase, "answer": top[0]["text"],
               "chose": top[0]["votes"], "of": state.total_votes,
               "from": top[0]["source"]}


def problem_rows(language, project_id=None):
    """Items that need a human to look: skipped past the target, reported, or
    finished with no single answer ahead.

    Each carries `why`, so the list can be triaged rather than merely read.
    """
    from .models import Flag, WordState

    flagged = {}
    fq = (db.session.query(Flag, Word)
          .join(Word, Word.id == Flag.word_id)
          .filter(Flag.resolved.is_(False), Flag.language == language))
    if project_id is not None:
        fq = fq.filter(Word.project_id == project_id)
    for flag, word in fq.all():
        flagged[word.id] = (word, flag.reason, flag.note)

    q = (db.session.query(WordState, Word)
         .join(Word, Word.id == WordState.word_id)
         .filter(WordState.language == language)
         .filter(db.or_(WordState.problem.is_(True),
                        WordState.done.is_(True),
                        WordState.contested.is_(True)))
         )
    if project_id is not None:
        q = q.filter(Word.project_id == project_id)

    seen = set()
    for state, word in q.order_by(Word.position, Word.id).all():
        why = note = None
        if state.problem:
            why = "skipped"
            note = f"{state.skips} speakers passed over it"
        elif state.contested:
            why = "withdrawn"
            note = "taken out after a report"
        elif state.done and len(leaders(word.id, language)) > 1:
            why = "no agreement"
            top = leaders(word.id, language)
            note = (f"{len(top)} answers tied on "
                    f"{top[0]['votes']} of {state.total_votes}")
        if not why:
            continue
        seen.add(word.id)
        row = {"item": word.phrase, "why": why, "note": note,
               "answers": [{"answer": r["text"], "chose": r["votes"]}
                           for r in answers(word.id, language)]}
        yield row

    for word_id, (word, reason, note) in flagged.items():
        if word_id in seen:
            continue
        yield {"item": word.phrase, "why": "reported", "note": note or reason,
               "answers": [{"answer": r["text"], "chose": r["votes"]}
                           for r in answers(word_id, language)]}


def settled_count(language, min_votes=None, project_id=None):
    """Items in this language that have collected the answers they wanted."""
    from .models import WordState
    q = (db.session.query(db.func.count(WordState.id))
         .join(Word, Word.id == WordState.word_id)
         .filter(WordState.language == language, WordState.done.is_(True)))
    if project_id is not None:
        q = q.filter(Word.project_id == project_id)
    return q.scalar() or 0


def settled_counts(project_id=None):
    """language -> items done, in one query rather than one per language."""
    from .models import WordState
    q = (db.session.query(WordState.language, db.func.count(WordState.id))
         .join(Word, Word.id == WordState.word_id)
         .filter(WordState.done.is_(True)))
    if project_id is not None:
        q = q.filter(Word.project_id == project_id)
    return dict(q.group_by(WordState.language).all())


# Old names. Kept because "verified" is a word the API and templates used, and
# the counts they wanted are these.
verified_count = settled_count
verified_counts = settled_counts


def typed_count(language, project_id=None):
    """How many typed answers have been collected."""
    q = (db.session.query(db.func.count(Evaluation.id))
         .join(Word, Word.id == Evaluation.word_id)
         .filter(Evaluation.language == language,
                 Evaluation.custom_text.isnot(None),
                 Evaluation.custom_text != ""))
    if project_id is not None:
        q = q.filter(Word.project_id == project_id)
    return q.scalar() or 0


def typed_counts(project_id=None):
    """language -> typed answers collected, in one query rather than one each."""
    q = (db.session.query(Evaluation.language, db.func.count(Evaluation.id))
         .join(Word, Word.id == Evaluation.word_id)
         .filter(Evaluation.custom_text.isnot(None),
                 Evaluation.custom_text != ""))
    if project_id is not None:
        q = q.filter(Word.project_id == project_id)
    return dict(q.group_by(Evaluation.language).all())


def sample_entries(language, limit=3, project_id=None):
    """A few real verified entries, for the documentation page."""
    out = []
    for row in export_rows(language, project_id=project_id):
        phrase, text, votes, share, total = row
        out.append({"phrase": phrase, "translation": text, "votes": votes,
                    "agreement": round(share, 2), "total_votes": total})
        if len(out) >= limit:
            break
    return out
