"""Tiers, vote state, and the work queue.

Work is done tier by tier rather than item by item. In the translation project
tier 1 is the commonest ~11k words; none of tier 2 is handed out until every
word in tier 1 has enough speakers agreeing. That way the words people actually
use are settled first, and a half-finished project is still a usable resource
rather than a thin scatter across half a million entries. Projects uploaded as a
file have no frequency data, so all their items land in one tier and the gate
does nothing - they are worked in the order the file gave.

Work is leased, not allocated. Nothing is reserved at signup, because a
volunteer who signs up today should be given whatever the project needs today,
not a list decided months ago. A lease expires if it goes unanswered, so a word
parked with someone who stopped coming returns to the queue.
"""

import unicodedata
from collections import Counter
from datetime import date, datetime, timedelta

from sqlalchemy import func

from .models import (Assignment, Evaluation, Project, Volunteer, Word,
                     WordState, db)

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
#
# The default; each project carries its own, because the cost of being wrong is
# not the same everywhere. Read it through votes_needed() rather than directly.
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


def votes_needed(project=None, project_id=None):
    """Matching answers that settle an item in this project.

    A project with no options to choose from never settles anything: there is
    nothing for a typed answer to agree with, so those projects collect and
    export rather than verify. Callers check `has_options` before treating a
    count as verification.
    """
    if project is None and project_id is not None:
        project = db.session.get(Project, project_id)
    if project is None:
        return VOTES_TO_SETTLE
    return max(1, project.votes_to_settle or VOTES_TO_SETTLE)


def project_of(word_id):
    row = db.session.get(Word, word_id)
    return row.project if row is not None else None


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
    """Recompute one language's vote state for one item.

    Votes are counted within the language only. Pooling them would let Twi
    answers settle an item for Ga speakers, who never saw it.

    Two counts, deliberately different. `top_votes` is agreement and only a tap
    on an offered option adds to it: two people writing the same thing have not
    agreed with each other in any way we can check, since spelling, spacing and
    dialect all differ, and verification has to mean something. `total_votes` is
    how many times the item has been answered at all, typing included - that is
    what finishes an item in a project where nothing can be verified.

    A typed wording becomes an option, so the next speaker can tap it, and those
    taps count like any other.
    """
    evals = Evaluation.query.filter_by(word_id=word_id, language=language,
                                       skipped=False).all()
    counts = Counter()
    answers = 0
    for ev in evals:
        answers += 1
        # A typed answer is an answer - it counts towards how many times this
        # item has been done, which is what finishes a project where nothing can
        # be verified. It never counts towards agreement, because agreeing with
        # somebody's spelling is not something we can establish.
        if ev.custom_text:
            continue
        text = ev.chosen_text
        if text:
            counts[normalise(text)] += 1

    project = project_of(word_id)
    needed = votes_needed(project)
    row = state_for(word_id, language)
    row.total_votes = answers
    row.top_votes = max(counts.values()) if counts else 0
    # A project without options cannot settle anything - every answer is typed
    # and there is nothing to agree with. Those items close once they have the
    # number of answers the project asked for, so they stop being handed out,
    # but nothing here is called verified.
    if project is not None and not project.has_options:
        row.done = row.total_votes >= needed
        row.contested = False
    else:
        row.done = row.top_votes >= needed
        row.contested = (not row.done
                         and row.total_votes >= MAX_VERDICTS_BEFORE_CONTESTED)
    if commit:
        db.session.commit()
    return row.done


def open_query(language, project_id=None):
    """Items still worth handing out in this language.

    Left join, because an item with no votes yet has no state row at all and
    must still count as open.

    Items belonging to a project that collects a separate file per language are
    filtered to that language; items with no language of their own (an English
    word awaiting translation) are open to every speaker. Flagged items are
    excluded until someone has looked at the report.
    """
    from .models import Flag

    flagged = db.session.query(Flag.word_id).filter(Flag.resolved.is_(False))
    q = (db.session.query(Word)
         .outerjoin(WordState,
                    db.and_(WordState.word_id == Word.id,
                            WordState.language == language))
         .filter(func.coalesce(WordState.done, False).is_(False),
                 func.coalesce(WordState.contested, False).is_(False),
                 db.or_(Word.language.is_(None), Word.language == language),
                 ~Word.id.in_(flagged)))
    if project_id is not None:
        q = q.filter(Word.project_id == project_id)
    return q


def active_tier(language, project_id=None):
    """The lowest tier still holding items this language can settle."""
    return (open_query(language, project_id=project_id)
            .with_entities(func.min(Word.tier))
            .scalar())


def tier_progress(language, project_id=None):
    """Per-tier totals for one language."""
    rows = (db.session.query(
        Word.tier, func.count(Word.id),
        func.sum(db.case((WordState.done.is_(True), 1), else_=0)),
        func.sum(db.case((WordState.contested.is_(True), 1), else_=0)))
        .outerjoin(WordState, db.and_(WordState.word_id == Word.id,
                                     WordState.language == language))
        .filter(db.or_(Word.language.is_(None), Word.language == language))
        .filter(Word.project_id == project_id if project_id is not None
                else db.true())
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


def lease_from_project(volunteer, project, count, today=None):
    """Hand a volunteer up to `count` items from one project.

    Items closest to being settled come first, so a tier converges instead of
    accumulating half-voted entries. Within that, the commonest first for the
    translation project, and file order for anything uploaded - an uploaded
    project has no frequency data, so `occurrences` is zero throughout and
    `position` decides.
    """
    if count <= 0:
        return 0
    today = today or date.today()
    language = volunteer.language
    tier = active_tier(language, project_id=project.id)
    if tier is None:
        return 0

    needed_votes = votes_needed(project)

    # Items this volunteer has already judged, or already holds.
    mine = db.session.query(Evaluation.word_id).filter(
        Evaluation.volunteer_id == volunteer.id)
    held = db.session.query(Assignment.word_id).filter(
        Assignment.volunteer_id == volunteer.id)

    # Over-fetch: some candidates will already have enough live leases.
    candidates = (open_query(language, project_id=project.id)
                  .filter(Word.tier == tier,
                          ~Word.id.in_(mine), ~Word.id.in_(held))
                  .order_by(func.coalesce(WordState.top_votes, 0).desc(),
                            Word.occurrences.desc(), Word.position.asc(),
                            Word.id.asc())
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
        state = states.get(word.id)
        if project.has_options:
            have = state.top_votes if state else 0
        else:
            # Nothing agrees with anything here, so what matters is how many
            # answers the item still wants, not how many matched.
            have = state.total_votes if state else 0
        still_needed = needed_votes - have - live.get(word.id, 0)
        if still_needed <= 0:
            continue
        db.session.add(Assignment(volunteer_id=volunteer.id, word_id=word.id,
                                  due_date=today, expires_at=expires))
        word.assign_count += 1
        given += 1

    if given:
        db.session.commit()
    return given


def lease_words(volunteer, count, today=None):
    """Hand a volunteer up to `count` items, mixed across their projects.

    The mix cannot be exact - five items across two projects is three and two -
    so the order is rotated by how much the volunteer has already done, and a
    project whose queue is dry passes its share to the others rather than
    shortening the day's list.
    """
    if count <= 0:
        return 0
    from .projects import active_for, rotate, shares

    projects = active_for(volunteer)
    if not projects:
        return 0

    # Rotate so the project that gets the smaller share is not always the same
    # one. Keyed on work done rather than a random number, so it is stable
    # within a day and testable.
    projects = rotate(projects, volunteer.done_count())

    given = 0
    for want, project in zip(shares(count, len(projects)), projects):
        given += lease_from_project(volunteer, project, want, today=today)

    # Whatever a dry project could not supply, offer to the others rather than
    # sending a short list.
    short = count - given
    if short > 0:
        for project in projects:
            if short <= 0:
                break
            got = lease_from_project(volunteer, project, short, today=today)
            given += got
            short -= got
    return given


def daily_quota(volunteer=None):
    """How many words go out in one send.

    The same number for everyone, whatever days they chose. What a volunteer
    controls is when the words arrive, not how many - a short list is the
    point of the thing.
    """
    from flask import current_app
    return max(1, current_app.config["WORDS_PER_DAY"])


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


def answers_needed(language, tier=None, project_id=None):
    """How many more verdicts would close a tier in this language.

    Counts what each open word still lacks rather than assuming two per word,
    so a word already holding one matching vote counts as one, not two.
    """
    if tier is None:
        tier = active_tier(language, project_id=project_id)
    if tier is None:
        return 0
    needed = votes_needed(project_id=project_id)
    rows = (open_query(language, project_id=project_id)
            .filter(Word.tier == tier)
            .with_entities(func.sum(
                needed - func.coalesce(WordState.top_votes, 0)))
            .scalar())
    return int(rows or 0)


def recruitment(language, words_per_volunteer, completion_rate, signed_up,
                project_id=None):
    """Volunteers needed to close this language's current tier in a year.

    Assumes only `completion_rate` of volunteers finish their commitment and
    that the rest contribute nothing. That is harsher than reality - people who
    drop out still answer something - and the point is to over-recruit rather
    than run out of speakers in month eleven.
    """
    tier = active_tier(language, project_id=project_id)
    needed_answers = answers_needed(language, tier, project_id=project_id)
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
