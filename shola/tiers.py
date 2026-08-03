"""Tiers, vote state, and the work queue.

The project is finished tier by tier rather than word by word. Tier 1 is the
commonest ~11k words; none of tier 2 is handed out until every word in tier 1
has five speakers agreeing on the same wording. That way the words people
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

from .models import Assignment, Evaluation, Volunteer, Word, WordState, db

# Occurrence thresholds, highest tier first. Chosen from the real distribution:
# tier 1 is ~11k words, the band worth settling before anything else.
TIER_THRESHOLDS = [
    (1, 50),      # >= 50 occurrences   ~11,206 words
    (2, 20),      # 20-49               ~11,184
    (3, 10),      # 10-19               ~16,466
    (4, 5),       # 5-9                 ~32,158
    (5, 0),       # everything else     ~407,808
]

# A word is settled when one wording has this many votes. Speakers who disagree
# do not settle it - only matching answers count towards the total.
VOTES_TO_SETTLE = 5

# How long a leased word waits for an answer before returning to the queue.
LEASE_DAYS = 10

# Give up on agreement after this many verdicts.
#
# This has to sit well clear of VOTES_TO_SETTLE. With five needed and only
# three options, four votes could sit on each without any reaching five: twelve
# verdicts, all legitimate, none settling. Volunteers can also add options of
# their own, so the ceiling is not fixed. Closing at 20 leaves room for genuine
# disagreement while stopping one word absorbing effort for ever. A contested
# word keeps every variant offered; it simply stops being handed out.
MAX_VERDICTS_BEFORE_CONTESTED = 20


def tier_for(occurrences):
    for tier, floor in TIER_THRESHOLDS:
        if occurrences >= floor:
            return tier
    return TIER_THRESHOLDS[-1][0]


def normalise(text):
    """Same folding consensus uses, so the two always agree on what a vote is."""
    return unicodedata.normalize("NFC", (text or "").strip()).casefold()


def state_for(word_id, language, create=True):
    """The WordState row for one word in one language."""
    row = WordState.query.filter_by(word_id=word_id, language=language).first()
    if row is None and create:
        row = WordState(word_id=word_id, language=language)
        db.session.add(row)
    return row


def refresh_word(word_id, language, commit=True):
    """Recompute one language's vote state for one word.

    Votes are counted within the language only. Pooling them would let Twi
    answers settle a word for Ga speakers, who never saw it.
    """
    evals = Evaluation.query.filter_by(word_id=word_id, language=language,
                                       skipped=False).all()
    counts = Counter()
    for ev in evals:
        text = ev.chosen_text
        if text:
            counts[normalise(text)] += 1

    row = state_for(word_id, language)
    row.total_votes = sum(counts.values())
    row.top_votes = max(counts.values()) if counts else 0
    row.done = row.top_votes >= VOTES_TO_SETTLE
    row.contested = (not row.done
                     and row.total_votes >= MAX_VERDICTS_BEFORE_CONTESTED)
    if commit:
        db.session.commit()
    return row.done


def open_query(language):
    """Words still worth handing out in this language.

    Left join, because a word with no votes yet has no state row at all and
    must still count as open.
    """
    return (db.session.query(Word)
            .outerjoin(WordState,
                       db.and_(WordState.word_id == Word.id,
                               WordState.language == language))
            .filter(func.coalesce(WordState.done, False).is_(False),
                    func.coalesce(WordState.contested, False).is_(False)))


def active_tier(language):
    """The lowest tier still holding words this language can settle."""
    row = (open_query(language)
           .with_entities(func.min(Word.tier))
           .scalar())
    return row


def tier_progress(language):
    """Per-tier totals for one language."""
    rows = (db.session.query(
        Word.tier, func.count(Word.id),
        func.sum(db.case((WordState.done.is_(True), 1), else_=0)),
        func.sum(db.case((WordState.contested.is_(True), 1), else_=0)))
        .outerjoin(WordState, db.and_(WordState.word_id == Word.id,
                                     WordState.language == language))
        .group_by(Word.tier).order_by(Word.tier).all())
    out = []
    for tier, total, done, contested in rows:
        done, contested = int(done or 0), int(contested or 0)
        closed = done + contested
        out.append({"tier": tier, "total": total, "done": done,
                    "contested": contested, "left": total - closed,
                    "pct": (closed / total * 100) if total else 0.0})
    return out


def outstanding_leases(word_ids, language):
    """word_id -> live leases promising a verdict in this language.

    Scoped by the holder's language: a Twi speaker holding a word does nothing
    towards settling it in Ga.
    """
    if not word_ids:
        return {}
    now = datetime.utcnow()
    rows = (db.session.query(Assignment.word_id, func.count(Assignment.id))
            .join(Volunteer, Volunteer.id == Assignment.volunteer_id)
            .filter(Assignment.word_id.in_(word_ids),
                    Volunteer.language == language,
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
    language = volunteer.language
    tier = active_tier(language)
    if tier is None:
        return 0

    # Words this volunteer has already judged, or already holds.
    mine = db.session.query(Evaluation.word_id).filter(
        Evaluation.volunteer_id == volunteer.id)
    held = db.session.query(Assignment.word_id).filter(
        Assignment.volunteer_id == volunteer.id)

    # Over-fetch: some candidates will already have enough live leases.
    candidates = (open_query(language)
                  .filter(Word.tier == tier,
                          ~Word.id.in_(mine), ~Word.id.in_(held))
                  .order_by(func.coalesce(WordState.top_votes, 0).desc(),
                            Word.occurrences.desc(), Word.id.asc())
                  .limit(count * 6)
                  .all())
    if not candidates:
        return 0

    live = outstanding_leases([w.id for w in candidates], language)
    states = {r.word_id: r for r in WordState.query.filter(
        WordState.word_id.in_([w.id for w in candidates]),
        WordState.language == language).all()}
    expires = datetime.utcnow() + timedelta(days=LEASE_DAYS)

    given = 0
    for word in candidates:
        if given >= count:
            break
        have = states[word.id].top_votes if word.id in states else 0
        still_needed = VOTES_TO_SETTLE - have - live.get(word.id, 0)
        if still_needed <= 0:
            continue
        db.session.add(Assignment(volunteer_id=volunteer.id, word_id=word.id,
                                  due_date=today, expires_at=expires))
        word.assign_count += 1
        given += 1

    if given:
        db.session.commit()
    return given


def daily_quota(volunteer=None):
    """How many words go out in one send.

    A flat number per send, not an annual goal divided by the days someone
    chose. The one exception is a schedule of once a week or less: a single
    email a week carrying ten words is barely worth opening, so those sends
    are longer.
    """
    from flask import current_app
    cfg = current_app.config
    sends_a_week = len(volunteer.day_numbers) if volunteer else 7
    if volunteer and sends_a_week and sends_a_week < 2:
        return max(1, cfg["WORDS_PER_WEEKLY_SEND"])
    return max(1, cfg["WORDS_PER_DAY"])


def release_stale(volunteer, today=None):
    """Hand back words this volunteer was offered on an earlier day.

    Nothing carries over. A missed day does not become a debt collected later:
    the words go straight back to the queue where another speaker can reach
    them, and the next send is a fresh list of whatever the project needs then.
    """
    today = today or date.today()
    n = (volunteer.assignments
         .filter(Assignment.status == "pending",
                 Assignment.due_date < today)
         .update({"status": "expired"}, synchronize_session=False))
    if n:
        db.session.commit()
    return n


def top_up(volunteer, today=None):
    """Give this volunteer a fresh list for today, up to their quota."""
    today = today or date.today()
    release_stale(volunteer, today)
    quota = daily_quota(volunteer)
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


def answers_needed(language, tier=None):
    """How many more verdicts would close a tier in this language.

    Counts what each open word still lacks rather than assuming two per word,
    so a word already holding one matching vote counts as one, not two.
    """
    if tier is None:
        tier = active_tier(language)
    if tier is None:
        return 0
    rows = (open_query(language)
            .filter(Word.tier == tier)
            .with_entities(func.sum(
                VOTES_TO_SETTLE - func.coalesce(WordState.top_votes, 0)))
            .scalar())
    return int(rows or 0)


def recruitment(language, words_per_volunteer, completion_rate, signed_up):
    """Volunteers needed to close this language's current tier in a year.

    Assumes only `completion_rate` of volunteers finish their commitment and
    that the rest contribute nothing. That is harsher than reality - people who
    drop out still answer something - and the point is to over-recruit rather
    than run out of speakers in month eleven.
    """
    tier = active_tier(language)
    needed_answers = answers_needed(language, tier)
    per_volunteer = max(1.0, words_per_volunteer * max(completion_rate, 0.01))
    needed = -(-needed_answers // int(per_volunteer)) if needed_answers else 0
    return {
        "tier": tier,
        "answers_needed": needed_answers,
        "per_volunteer": int(per_volunteer),
        "volunteers_needed": needed,
        "signed_up": signed_up,
        "still_to_recruit": max(0, needed - signed_up),
    }
