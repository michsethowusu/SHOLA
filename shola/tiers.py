"""Tiers, vote state, and the work queue.

The project is finished tier by tier rather than word by word. Tier 1 is the
commonest ~11k words; none of tier 2 is handed out until every word in tier 1
has two speakers agreeing on the same wording. That way the words people
actually use are settled first, and a half-finished project is still a usable
resource rather than a thin scatter across half a million entries.

Work is leased, not allocated. Nothing is reserved at signup, because a
volunteer who signs up today should be given whatever the project needs today,
not a list decided months ago. A lease expires if it goes unanswered, so a word
parked with someone who stopped coming returns to the queue.
"""

import unicodedata
from collections import Counter
from datetime import date, datetime, timedelta

from sqlalchemy import func

from .models import Assignment, Evaluation, Volunteer, Word, db

# Occurrence thresholds, highest tier first. Chosen from the real distribution:
# tier 1 is ~11k words, the band worth settling before anything else.
TIER_THRESHOLDS = [
    (1, 50),      # >= 50 occurrences   ~11,206 words
    (2, 20),      # 20-49               ~11,184
    (3, 10),      # 10-19               ~16,466
    (4, 5),       # 5-9                 ~32,158
    (5, 0),       # everything else     ~407,808
]

# A word is settled when one wording has this many votes. Two speakers who
# disagree do not settle it: the top wording still has only one vote, so a
# third verdict is needed to break the tie.
VOTES_TO_SETTLE = 2

# How long a leased word waits for an answer before returning to the queue.
LEASE_DAYS = 10

# Give up on agreement after this many verdicts.
#
# Voting between the three options always resolves on its own: the worst case
# is one vote each, and the fourth verdict has to create a pair. This limit
# exists for typed answers, which are free text and so unbounded - five
# speakers can each write a genuinely different wording and no pair ever forms.
# (Case and Unicode differences are folded first, so those merge rather than
# splitting the vote.) Such a word is a finding, not a failure: it is closed as
# contested, every variant is kept, and the tier can finish.
MAX_VERDICTS_BEFORE_CONTESTED = 5


def tier_for(occurrences):
    for tier, floor in TIER_THRESHOLDS:
        if occurrences >= floor:
            return tier
    return TIER_THRESHOLDS[-1][0]


def normalise(text):
    """Same folding consensus uses, so the two always agree on what a vote is."""
    return unicodedata.normalize("NFC", (text or "").strip()).casefold()


def refresh_word(word, commit=True):
    """Recompute a word's vote state from its verdicts."""
    evals = Evaluation.query.filter_by(word_id=word.id, skipped=False).all()
    counts = Counter()
    for ev in evals:
        text = ev.chosen_text
        if text:
            counts[normalise(text)] += 1
    word.total_votes = sum(counts.values())
    word.top_votes = max(counts.values()) if counts else 0
    word.done = word.top_votes >= VOTES_TO_SETTLE
    word.contested = (not word.done
                      and word.total_votes >= MAX_VERDICTS_BEFORE_CONTESTED)
    if commit:
        db.session.commit()
    return word.done


def open_words():
    """Filter for words still worth handing out."""
    return db.and_(Word.done.is_(False), Word.contested.is_(False))


def active_tier():
    """The lowest tier still holding words that can be settled."""
    return (db.session.query(func.min(Word.tier))
            .filter(open_words()).scalar())


def tier_progress():
    """Per-tier totals for the progress board."""
    rows = (db.session.query(
        Word.tier, func.count(Word.id),
        func.sum(db.case((Word.done.is_(True), 1), else_=0)),
        func.sum(db.case((Word.contested.is_(True), 1), else_=0)))
        .group_by(Word.tier).order_by(Word.tier).all())
    out = []
    for tier, total, done, contested in rows:
        done, contested = int(done or 0), int(contested or 0)
        closed = done + contested
        out.append({"tier": tier, "total": total, "done": done,
                    "contested": contested, "left": total - closed,
                    "pct": (closed / total * 100) if total else 0.0})
    return out


def outstanding_leases(word_ids):
    """word_id -> how many live leases already promise a verdict on it."""
    if not word_ids:
        return {}
    now = datetime.utcnow()
    rows = (db.session.query(Assignment.word_id, func.count(Assignment.id))
            .filter(Assignment.word_id.in_(word_ids),
                    Assignment.status == "pending",
                    db.or_(Assignment.expires_at.is_(None),
                           Assignment.expires_at > now))
            .group_by(Assignment.word_id).all())
    return dict(rows)


def lease_words(volunteer, count, today=None):
    """Hand a volunteer up to `count` words from the tier being worked on.

    Words closest to being settled come first, so a tier converges instead of
    accumulating half-voted entries, and within that the commonest go first.
    """
    if count <= 0:
        return 0
    today = today or date.today()
    tier = active_tier()
    if tier is None:
        return 0

    # Words this volunteer has already judged, or already holds.
    mine = db.session.query(Evaluation.word_id).filter(
        Evaluation.volunteer_id == volunteer.id)
    held = db.session.query(Assignment.word_id).filter(
        Assignment.volunteer_id == volunteer.id)

    # Over-fetch: some candidates will already have enough live leases.
    candidates = (Word.query
                  .filter(Word.tier == tier, open_words(),
                          ~Word.id.in_(mine), ~Word.id.in_(held))
                  .order_by(Word.top_votes.desc(), Word.occurrences.desc(),
                            Word.id.asc())
                  .limit(count * 6)
                  .all())
    if not candidates:
        return 0

    live = outstanding_leases([w.id for w in candidates])
    expires = datetime.utcnow() + timedelta(days=LEASE_DAYS)

    given = 0
    for word in candidates:
        if given >= count:
            break
        still_needed = VOTES_TO_SETTLE - word.top_votes - live.get(word.id, 0)
        if still_needed <= 0:
            continue
        db.session.add(Assignment(volunteer_id=volunteer.id, word_id=word.id,
                                  due_date=today, expires_at=expires))
        word.assign_count += 1
        given += 1

    if given:
        db.session.commit()
    return given


def daily_quota(volunteer, target=None, horizon_days=365):
    """How many words a volunteer should see on one of their days.

    Still the brief's arithmetic - an annual goal spread over the days they
    chose - but now it sets the size of each day's lease rather than carving up
    a fixed list at signup.
    """
    target = target or 1000
    days_a_week = len(volunteer.day_numbers) or 7
    days_a_year = max(1, round(horizon_days * days_a_week / 7))
    return max(1, -(-target // days_a_year))      # ceiling division


def top_up(volunteer, target=None, today=None):
    """Bring a volunteer's outstanding words up to their daily quota."""
    today = today or date.today()
    quota = daily_quota(volunteer, target=target)
    pending = volunteer.pending_today(today).count()
    return lease_words(volunteer, quota - pending, today=today)


def release_expired(now=None):
    """Return unanswered leases to the queue."""
    now = now or datetime.utcnow()
    n = (Assignment.query
         .filter(Assignment.status == "pending",
                 Assignment.expires_at.isnot(None),
                 Assignment.expires_at <= now)
         .update({"status": "expired"}, synchronize_session=False))
    db.session.commit()
    return n


def assign_tiers():
    """(Re)compute every word's tier from its occurrence count."""
    changed = 0
    for tier, floor in TIER_THRESHOLDS:
        upper = None
        for t, f in TIER_THRESHOLDS:
            if t == tier - 1:
                upper = f
        q = Word.query.filter(Word.occurrences >= floor)
        if upper is not None:
            q = q.filter(Word.occurrences < upper)
        changed += q.update({"tier": tier}, synchronize_session=False)
    db.session.commit()
    return changed
