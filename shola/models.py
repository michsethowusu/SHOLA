"""Database models.

The shape of the data follows the job: a Word holds the English noun, a
Candidate holds one machine-proposed translation of it, an Assignment says a
volunteer owes a verdict on a word by a certain day, and an Evaluation is that
verdict. Consensus is derived from Evaluations, never stored as truth.
"""

from datetime import date, datetime

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func

db = SQLAlchemy()


class Volunteer(db.Model):
    __tablename__ = "volunteers"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(255), nullable=False, unique=True, index=True)
    language = db.Column(db.String(20), nullable=False, index=True)
    photo = db.Column(db.String(255))
    photo_consent = db.Column(db.Boolean, default=False, nullable=False)

    # Weekdays the volunteer chose, as "0,2,4" (Monday = 0). Empty means any.
    available_days = db.Column(db.String(20), default="", nullable=False)
    time_window = db.Column(db.String(20), default="anytime", nullable=False)

    joined_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    last_emailed_on = db.Column(db.Date)

    # Receiving emails at all. False means they stopped; reversible from their
    # own link, so it is not a deletion.
    active = db.Column(db.Boolean, default=True, nullable=False)

    # A pause with an end date. Sending resumes by itself when it passes, so a
    # break does not depend on remembering to come back.
    paused_until = db.Column(db.Date)

    # Consecutive sends that went unanswered. Not a penalty - words are never
    # held against anyone - just the signal that their schedule is wrong for
    # them, so we can offer a lighter one.
    missed_in_a_row = db.Column(db.Integer, default=0, nullable=False)
    nudged_on = db.Column(db.Date)

    @property
    def paused(self):
        """True while a pause is running."""
        return bool(self.paused_until and self.paused_until > date.today())

    @property
    def receiving(self):
        """Whether a send should reach them today."""
        return self.active and not self.paused

    assignments = db.relationship("Assignment", back_populates="volunteer",
                                  lazy="dynamic")
    evaluations = db.relationship("Evaluation", back_populates="volunteer",
                                  lazy="dynamic")

    @property
    def day_numbers(self):
        if not self.available_days:
            return list(range(7))
        return [int(d) for d in self.available_days.split(",") if d != ""]

    @property
    def initials(self):
        parts = [p for p in self.name.split() if p]
        return "".join(p[0].upper() for p in parts[:2]) or "?"

    def done_count(self):
        return self.evaluations.count()

    def pending_today(self, today=None):
        """Assignments due on or before today that still have no verdict.

        Earlier days are included so a list opened just after midnight, or a
        link followed hours late, still works. They are not a backlog: a send
        hands anything from an earlier day back to the queue first, so nothing
        accumulates against anyone.
        """
        today = today or date.today()
        return (self.assignments
                .filter(Assignment.status == "pending",
                        Assignment.due_date <= today)
                .order_by(Assignment.due_date, Assignment.id))

    def upcoming(self, today=None):
        """Work scheduled for a later day.

        Used when nothing is due: a volunteer who opens the site on an off day
        should be offered the next words rather than an empty screen. Their
        emails still only arrive on the days they chose.
        """
        today = today or date.today()
        return (self.assignments
                .filter(Assignment.status == "pending",
                        Assignment.due_date > today)
                .order_by(Assignment.due_date, Assignment.id))


class PendingSignup(db.Model):
    """A signup held back until the email address proves it exists.

    Nothing becomes a Volunteer until the code is entered, so an address
    somebody mistyped never gets 1000 words assigned to it. The code is stored
    hashed - it is a credential for as long as it lives.
    """

    __tablename__ = "pending_signups"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), nullable=False, unique=True, index=True)
    name = db.Column(db.String(120), nullable=False)
    language = db.Column(db.String(20), nullable=False)
    available_days = db.Column(db.String(20), default="", nullable=False)
    time_window = db.Column(db.String(20), default="anytime", nullable=False)
    photo = db.Column(db.String(255))
    photo_consent = db.Column(db.Boolean, default=False, nullable=False)

    code_hash = db.Column(db.String(255), nullable=False)
    attempts = db.Column(db.Integer, default=0, nullable=False)
    sends = db.Column(db.Integer, default=1, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)

    def expired(self):
        return datetime.utcnow() > self.expires_at


class Word(db.Model):
    __tablename__ = "words"

    id = db.Column(db.Integer, primary_key=True)
    phrase = db.Column(db.String(255), nullable=False, unique=True, index=True)
    assign_count = db.Column(db.Integer, default=0, nullable=False, index=True)

    # Corpus evidence. `occurrences` is the raw count across news, research and
    # speech; `frequency` is the rounded percentage, kept for display only -
    # 91% of words round to 0.0000, so it cannot order the long tail.
    frequency = db.Column(db.Float, default=0.0, nullable=False)
    occurrences = db.Column(db.Integer, default=0, nullable=False, index=True)

    # Which band of commonness this word sits in. Tier 1 is worked to
    # completion before tier 2 opens.
    tier = db.Column(db.Integer, default=5, nullable=False, index=True)

    # Superseded by WordState, which tracks these per language. Left in place
    # so existing databases still load; nothing reads them.
    top_votes = db.Column(db.Integer, default=0, nullable=False)
    total_votes = db.Column(db.Integer, default=0, nullable=False)
    done = db.Column(db.Boolean, default=False, nullable=False)
    contested = db.Column(db.Boolean, default=False, nullable=False)

    __table_args__ = (
        db.Index("ix_word_queue", "tier", "done", "top_votes", "occurrences"),
    )

    candidates = db.relationship("Candidate", back_populates="word",
                                 cascade="all, delete-orphan")

    def options(self, language):
        return [c for c in self.candidates if c.language == language]


class WordState(db.Model):
    """Vote state for one word in one language.

    A word is not settled globally - it is settled per language. Two Twi
    speakers agreeing tells you nothing about Ga, so every language keeps its
    own counts and its own done/contested flags. A missing row means "no votes
    yet", which the queue treats as open.
    """

    __tablename__ = "word_state"
    __table_args__ = (
        db.UniqueConstraint("word_id", "language", name="uq_word_language"),
        db.Index("ix_state_queue", "language", "done", "contested", "top_votes"),
    )

    id = db.Column(db.Integer, primary_key=True)
    word_id = db.Column(db.Integer, db.ForeignKey("words.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    language = db.Column(db.String(20), nullable=False, index=True)
    top_votes = db.Column(db.Integer, default=0, nullable=False)
    total_votes = db.Column(db.Integer, default=0, nullable=False)
    done = db.Column(db.Boolean, default=False, nullable=False)
    contested = db.Column(db.Boolean, default=False, nullable=False)

    word = db.relationship("Word")


class Candidate(db.Model):
    """One proposed translation of a word into one language."""

    __tablename__ = "candidates"
    __table_args__ = (
        db.UniqueConstraint("word_id", "language", "position",
                            name="uq_candidate_slot"),
        db.Index("ix_candidate_word_lang", "word_id", "language"),
    )

    id = db.Column(db.Integer, primary_key=True)
    word_id = db.Column(db.Integer, db.ForeignKey("words.id", ondelete="CASCADE"),
                        nullable=False)
    language = db.Column(db.String(20), nullable=False)
    position = db.Column(db.Integer, nullable=False)      # 1, 2 or 3
    text = db.Column(db.String(400), nullable=False)
    source = db.Column(db.String(40), default="gemini", nullable=False)

    word = db.relationship("Word", back_populates="candidates")


class Assignment(db.Model):
    __tablename__ = "assignments"
    __table_args__ = (
        db.UniqueConstraint("volunteer_id", "word_id", name="uq_one_per_pair"),
        db.Index("ix_assignment_queue", "volunteer_id", "status", "due_date"),
    )

    id = db.Column(db.Integer, primary_key=True)
    volunteer_id = db.Column(db.Integer, db.ForeignKey("volunteers.id"),
                             nullable=False)
    word_id = db.Column(db.Integer, db.ForeignKey("words.id"), nullable=False)
    due_date = db.Column(db.Date, nullable=False, index=True)
    status = db.Column(db.String(20), default="pending", nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    # A lease, not a permanent allocation: if a volunteer never answers, the
    # word must return to the queue instead of being stuck with them.
    expires_at = db.Column(db.DateTime, index=True)

    volunteer = db.relationship("Volunteer", back_populates="assignments")
    word = db.relationship("Word")


class Evaluation(db.Model):
    """A volunteer's verdict: either a chosen candidate or their own wording.

    Exactly one of candidate_id / custom_text is set, enforced in the view -
    picking an option and typing your own are mutually exclusive by design.
    """

    __tablename__ = "evaluations"
    __table_args__ = (
        db.UniqueConstraint("volunteer_id", "word_id", name="uq_one_verdict"),
        db.Index("ix_eval_word_lang", "word_id", "language"),
    )

    id = db.Column(db.Integer, primary_key=True)
    volunteer_id = db.Column(db.Integer, db.ForeignKey("volunteers.id"),
                             nullable=False)
    word_id = db.Column(db.Integer, db.ForeignKey("words.id"), nullable=False)
    language = db.Column(db.String(20), nullable=False)
    candidate_id = db.Column(db.Integer, db.ForeignKey("candidates.id"))
    custom_text = db.Column(db.String(400))
    skipped = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False,
                           index=True)

    volunteer = db.relationship("Volunteer", back_populates="evaluations")
    word = db.relationship("Word")
    candidate = db.relationship("Candidate")

    @property
    def chosen_text(self):
        if self.custom_text:
            return self.custom_text
        return self.candidate.text if self.candidate else None


def site_stats():
    """Numbers for the landing page and the stats board."""
    verdicts = db.session.query(func.count(Evaluation.id)).scalar() or 0
    volunteers = (db.session.query(func.count(Volunteer.id))
                  .filter(Volunteer.active.is_(True)).scalar() or 0)
    words = db.session.query(func.count(Word.id)).scalar() or 0
    covered = (db.session.query(func.count(func.distinct(Evaluation.word_id)))
               .scalar() or 0)
    return {"verdicts": verdicts, "volunteers": volunteers, "words": words,
            "covered": covered,
            "coverage_pct": (covered / words * 100) if words else 0.0}

def ensure_columns():
    """Add columns that models declare but an existing database lacks.

    `db.create_all()` creates missing tables and nothing else, so a new column
    on a table that already holds rows is silently absent until something reads
    it and the query fails. There is no Alembic here on purpose: the schema is
    small and the alternative is a migrations directory for one ALTER. Adding a
    column to the list below is enough.

    Only additive, nullable columns belong here. Anything that needs data moved
    or a column dropped is a real migration and should be written as one.
    """
    from sqlalchemy import inspect, text

    wanted = {
        "volunteers": {"paused_until": "DATE",
                       "missed_in_a_row": "INTEGER NOT NULL DEFAULT 0",
                       "nudged_on": "DATE"},
    }
    inspector = inspect(db.engine)
    added = []
    for table, columns in wanted.items():
        if table not in inspector.get_table_names():
            continue
        have = {c["name"] for c in inspector.get_columns(table)}
        for name, sql_type in columns.items():
            if name in have:
                continue
            db.session.execute(
                text(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"))
            added.append(f"{table}.{name}")
    if added:
        db.session.commit()
    return added
