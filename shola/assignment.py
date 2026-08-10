"""Word assignment and day scheduling.

Two rules from the brief drive this:

1. Coverage before duplication. Every word must reach one volunteer before any
   word reaches a second. Sorting candidate words by how many times they have
   been assigned gives exactly that, and it keeps working as volunteers join:
   once the whole list is covered, the least-covered words come round again and
   the duplicates become the agreement signal for consensus.

2. A year to finish, on the volunteer's own days. The 1000 words are spread
   across the weekdays they said they were free, so a volunteer who picked two
   days a week gets a bigger daily list than one who picked seven, and both
   arrive at 1000 in a year.
"""

from datetime import date, datetime, timedelta

from sqlalchemy import func

from .models import Assignment, Candidate, Evaluation, Volunteer, Word, db


def available_dates(day_numbers, start, horizon_days):
    """Dates within the horizon that fall on the volunteer's chosen weekdays."""
    days = set(day_numbers) or set(range(7))
    out = []
    for offset in range(horizon_days):
        d = start + timedelta(days=offset)
        if d.weekday() in days:
            out.append(d)
    return out


def spread(total, slots):
    """Split `total` items over `slots` days as evenly as possible.

    The remainder goes to the earliest days rather than the last, so a volunteer
    never finds their heaviest day is the final one.
    """
    if slots <= 0:
        return []
    base, extra = divmod(total, slots)
    return [base + (1 if i < extra else 0) for i in range(slots)]


def pick_words(volunteer, count):
    """Least-assigned first, then most frequent.

    The two orderings do different jobs and do not conflict: assign_count keeps
    coverage ahead of duplication, and within one coverage level the commonest
    words go out first, so the words people actually use are verified early
    rather than whenever they happen to come up.
    """
    already = db.session.query(Assignment.word_id).filter(
        Assignment.volunteer_id == volunteer.id)
    return (Word.query
            .filter(~Word.id.in_(already))
            .order_by(Word.assign_count.asc(), Word.frequency.desc(),
                      Word.id.asc())
            .limit(count)
            .all())


def assign_words(volunteer, count, start=None, horizon_days=365):
    """Deprecated: work is leased on demand now, see tiers.lease_words.

    Kept because the fixed allocation is still the right shape for a one-off
    backfill, but nothing in the live flow calls it.
    """
    # Starts today so a volunteer who just signed up has something to do
    # immediately; the first email still goes out on their next chosen day.
    start = start or date.today()
    words = pick_words(volunteer, count)
    if not words:
        return 0

    dates = available_dates(volunteer.day_numbers, start, horizon_days)
    if not dates:
        dates = [start]
    per_day = spread(len(words), len(dates))

    i = 0
    for day, n in zip(dates, per_day):
        for _ in range(n):
            if i >= len(words):
                break
            db.session.add(Assignment(volunteer_id=volunteer.id,
                                      word_id=words[i].id, due_date=day))
            words[i].assign_count += 1
            i += 1
    db.session.commit()
    return i


def adopt_wording(word_id, language, text):
    """Turn a volunteer's own wording into an option others can choose.

    A typed answer is worth no less than a machine-proposed one, so it joins
    the list of options for that word: the next speaker sees it and can agree
    with it rather than typing the same thing again. This is also what lets a
    language with nothing loaded get started - the first speaker types, and
    everyone after has something to vote on.

    Returns the Candidate. An answer matching an existing option becomes a vote
    for that option instead of a duplicate entry, so agreement is not split
    between two spellings of the same thing.
    """
    from .tiers import normalise

    wanted = normalise(text)
    if not wanted:
        return None

    existing = Candidate.query.filter_by(word_id=word_id, language=language).all()
    for cand in existing:
        if normalise(cand.text) == wanted:
            return cand

    position = max((c.position for c in existing), default=0) + 1
    cand = Candidate(word_id=word_id, language=language, position=position,
                     text=text.strip(), source="volunteer")
    db.session.add(cand)
    db.session.flush()
    return cand


def record_verdict(volunteer, word_id, candidate_id=None, custom_text=None,
                   skipped=False):
    """Store a verdict, or revise the one already there.

    A volunteer who realises they tapped the wrong option can go back and fix
    it, so a second verdict on the same word replaces the first rather than
    being ignored. It stays one verdict per volunteer per word, so nobody votes
    twice and consensus is unaffected.
    """
    if custom_text and not skipped:
        adopted = adopt_wording(word_id, volunteer.language, custom_text)
        if adopted is not None:
            # Pointed at the option so the next speaker can tap it, with the
            # typed text kept alongside. Keeping both is what lets this count as
            # a contribution rather than a vote: verification counts taps, and
            # custom_text being set is how a typed answer is known.
            candidate_id = adopted.id

    existing = Evaluation.query.filter_by(volunteer_id=volunteer.id,
                                          word_id=word_id).first()
    if existing:
        existing.candidate_id = candidate_id
        existing.custom_text = custom_text or None
        existing.skipped = skipped
        existing.created_at = datetime.utcnow()
        assignment = Assignment.query.filter_by(volunteer_id=volunteer.id,
                                                word_id=word_id).first()
        if assignment:
            assignment.status = "skipped" if skipped else "done"
        db.session.commit()
        from .tiers import refresh_word
        refresh_word(word_id, volunteer.language)
        return existing

    ev = Evaluation(volunteer_id=volunteer.id, word_id=word_id,
                    language=volunteer.language, candidate_id=candidate_id,
                    custom_text=(custom_text or None), skipped=skipped)
    db.session.add(ev)

    assignment = Assignment.query.filter_by(volunteer_id=volunteer.id,
                                            word_id=word_id).first()
    if assignment:
        assignment.status = "skipped" if skipped else "done"
    db.session.commit()

    from .tiers import refresh_word
    refresh_word(word_id, volunteer.language)
    return ev


def leaderboard(limit=50):
    """SHOLA Champions, most verdicts first."""
    rows = (db.session.query(Volunteer,
                             func.count(Evaluation.id).label("n"))
            .outerjoin(Evaluation, Evaluation.volunteer_id == Volunteer.id)
            .filter(Volunteer.active.is_(True))
            .group_by(Volunteer.id)
            .order_by(func.count(Evaluation.id).desc(), Volunteer.joined_at)
            .limit(limit)
            .all())
    return [(v, n) for v, n in rows]
