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

from datetime import date, timedelta

from sqlalchemy import func

from .models import Assignment, Evaluation, Volunteer, Word, db


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
    """Least-assigned words first, skipping any this volunteer already has."""
    already = db.session.query(Assignment.word_id).filter(
        Assignment.volunteer_id == volunteer.id)
    return (Word.query
            .filter(~Word.id.in_(already))
            .order_by(Word.assign_count.asc(), Word.id.asc())
            .limit(count)
            .all())


def assign_words(volunteer, count, start=None, horizon_days=365):
    """Give a volunteer `count` words, dated across their available days."""
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


def redistribute(volunteer, today=None, horizon_days=365):
    """Re-date overdue work across the volunteer's remaining available days.

    Missing a week should not produce one crushing list; it should raise each
    remaining day slightly. Work is never removed, only moved, so the 1000
    still land inside the commitment window.
    """
    today = today or date.today()
    overdue = (volunteer.assignments
               .filter(Assignment.status == "pending",
                       Assignment.due_date < today)
               .order_by(Assignment.due_date, Assignment.id)
               .all())
    if not overdue:
        return 0

    future = (volunteer.assignments
              .filter(Assignment.status == "pending",
                      Assignment.due_date >= today)
              .count())
    dates = available_dates(volunteer.day_numbers, today, horizon_days)
    if not dates:
        return 0

    # Blend overdue back in with what is already scheduled ahead.
    per_day = spread(len(overdue) + future, len(dates))
    room = [max(0, n) for n in per_day]

    moved, slot = 0, 0
    for item in overdue:
        while slot < len(dates) and room[slot] == 0:
            slot += 1
        if slot >= len(dates):
            item.due_date = dates[-1]
        else:
            item.due_date = dates[slot]
            room[slot] -= 1
        moved += 1
    db.session.commit()
    return moved


def record_verdict(volunteer, word_id, candidate_id=None, custom_text=None,
                   skipped=False):
    """Store one verdict and close its assignment. Idempotent per word."""
    existing = Evaluation.query.filter_by(volunteer_id=volunteer.id,
                                          word_id=word_id).first()
    if existing:
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
