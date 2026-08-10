"""Database models.

The shape of the data follows the job: a Project is a body of work someone
wants done, a Word is one item in it, a Candidate is one proposed answer for
that item in one language, an Assignment says a volunteer owes a verdict by a
certain day, and an Evaluation is that verdict. Consensus is derived from
Evaluations, never stored as truth.

`Word` and `word_id` are historical names. An item may be a word, a sentence or
a paragraph - the project says which - and the interface calls them items
throughout. The table keeps its old name because renaming it would mean
rewriting every foreign key on a live database for no behavioural gain.
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

    # Projects picked at sign-up, as "1,4". Held here rather than created as
    # opt-ins because nothing exists to opt in until the code comes back.
    project_ids = db.Column(db.String(200), default="", nullable=False)
    exclusive_project_id = db.Column(db.Integer)

    code_hash = db.Column(db.String(255), nullable=False)
    attempts = db.Column(db.Integer, default=0, nullable=False)
    sends = db.Column(db.Integer, default=1, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)

    def expired(self):
        return datetime.utcnow() > self.expires_at


class Project(db.Model):
    """A body of work: some items, in some languages, needing answers.

    The translation of everyday words is one of these, not a special case. It
    is created by a migration on first boot so that everything already
    collected belongs to a project like anything submitted later.
    """

    __tablename__ = "projects"

    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(80), nullable=False, unique=True, index=True)

    # Phrased as the job being asked of a volunteer, because this is what they
    # choose from: "Translate everyday Ghanaian words", not "Word corpus v2".
    title = db.Column(db.String(160), nullable=False)
    summary = db.Column(db.String(600), default="", nullable=False)

    # word | sentence | paragraph. Decides the wording of the interface and
    # how much room an answer needs.
    item_format = db.Column(db.String(20), default="word", nullable=False)

    # How many answers an item wants before it closes and stops being handed
    # out. Where there are options, that means matching answers, and reaching it
    # is what "verified" means. Where every answer is typed, nothing matches
    # anything, so it is simply how many answers to collect per item - it still
    # decides when an item is done and therefore when the project is finished.
    # Per project because the cost of being wrong differs: a place name may
    # need more agreement than a common noun.
    votes_to_settle = db.Column(db.Integer, default=5, nullable=False)

    # False when items arrive with no options, so every answer is typed. Those
    # answers are collected and exported, never treated as verified - there is
    # nothing for them to agree with.
    has_options = db.Column(db.Boolean, default=True, nullable=False)


    status = db.Column(db.String(20), default="pending", nullable=False,
                       index=True)
    review_note = db.Column(db.String(600), default="", nullable=False)

    submitter_name = db.Column(db.String(120), default="", nullable=False)
    submitter_email = db.Column(db.String(255), default="", nullable=False)
    submitter_org = db.Column(db.String(160), default="", nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    approved_at = db.Column(db.DateTime)
    announced_at = db.Column(db.DateTime)

    # Ordering on the sign-up page. The core project sits first.
    sort_order = db.Column(db.Integer, default=100, nullable=False)

    languages = db.relationship("ProjectLanguage", back_populates="project",
                                cascade="all, delete-orphan", lazy="dynamic")

    @property
    def language_codes(self):
        return [pl.language for pl in self.languages]

    @property
    def item_noun(self):
        return {"word": "word", "sentence": "sentence",
                "paragraph": "paragraph"}.get(self.item_format, "item")

    @property
    def item_plural(self):
        return self.item_noun + "s"

    @property
    def approved(self):
        return self.status == "approved"

    def item_count(self, language=None):
        q = Word.query.filter(Word.project_id == self.id)
        if language:
            q = q.filter(db.or_(Word.language.is_(None),
                                Word.language == language))
        return q.count()

    def preview(self, language=None, limit=5):
        """A few real items with their options, for anyone deciding to join."""
        q = Word.query.filter(Word.project_id == self.id)
        if language:
            q = q.filter(db.or_(Word.language.is_(None),
                                Word.language == language))
        items = q.order_by(Word.position, Word.id).limit(limit).all()
        out = []
        for item in items:
            langs = [language] if language else self.language_codes
            options = []
            for code in langs:
                options = [c.text for c in sorted(item.options(code),
                                                  key=lambda c: c.position)]
                if options:
                    break
            out.append({"text": item.phrase, "options": options})
        return out

    def progress(self, language=None):
        """How much of this project is done.

        An item exists once, however many languages answer it. For one language
        that is simply items done out of items; across a project it is the share
        of (items x languages) finished, reported as a percentage rather than a
        count - "42,136,336 items" would be a nonsense number to put on a page
        when the project has 478,822 items in it.

        Counted from WordState, which only holds rows for items somebody has
        answered, so the cost follows work done rather than corpus size.
        """
        codes = [language] if language else self.language_codes
        items = self.item_count(language)
        # Not called "items": a template reading `progress.items` gets dict.items
        # in Jinja, which formats as a bound method and has bitten this codebase
        # twice.
        if not codes or not items:
            return {"item_total": items, "languages": len(codes), "done": 0,
                    "left": items, "pct": 0.0, "finished": False,
                    "answers_each": self.votes_to_settle}

        done = (db.session.query(db.func.count(WordState.id))
                .join(Word, Word.id == WordState.word_id)
                .filter(Word.project_id == self.id,
                        WordState.language.in_(codes),
                        WordState.done.is_(True))
                .scalar() or 0)
        slots = items * len(codes)
        done = min(done, slots)
        return {"item_total": items, "languages": len(codes), "done": done,
                "left": slots - done,
                "pct": (done / slots * 100) if slots else 0.0,
                "finished": bool(slots) and done >= slots,
                "answers_each": self.votes_to_settle}


class ProjectLanguage(db.Model):
    """One language a project collects answers in.

    A separate row rather than a comma-joined column so the sign-up page can
    ask the database which projects a speaker of one language can join.
    """

    __tablename__ = "project_languages"
    __table_args__ = (
        db.UniqueConstraint("project_id", "language", name="uq_project_language"),
    )

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id",
                                                     ondelete="CASCADE"),
                           nullable=False, index=True)
    language = db.Column(db.String(20), nullable=False, index=True)

    project = db.relationship("Project", back_populates="languages")


class VolunteerProject(db.Model):
    """A volunteer opting in to a project.

    `exclusive` marks someone who arrived through a project's own share link.
    They may join others, but nothing else is sent to them until the project
    that brought them here is finished - the person who shared the link earned
    that.
    """

    __tablename__ = "volunteer_projects"
    __table_args__ = (
        db.UniqueConstraint("volunteer_id", "project_id", name="uq_opt_in"),
    )

    id = db.Column(db.Integer, primary_key=True)
    volunteer_id = db.Column(db.Integer, db.ForeignKey("volunteers.id",
                                                       ondelete="CASCADE"),
                             nullable=False, index=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id",
                                                     ondelete="CASCADE"),
                           nullable=False, index=True)
    exclusive = db.Column(db.Boolean, default=False, nullable=False)
    joined_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    project = db.relationship("Project")


class Flag(db.Model):
    """A volunteer reporting a problem with an item.

    The people looking at the data are the only ones who will notice a broken
    item, so they need somewhere to say so mid-task. A flagged item stops being
    handed out until someone has looked.
    """

    __tablename__ = "flags"
    __table_args__ = (
        db.Index("ix_flag_open", "resolved", "created_at"),
    )

    id = db.Column(db.Integer, primary_key=True)
    word_id = db.Column(db.Integer, db.ForeignKey("words.id"), nullable=False,
                        index=True)
    volunteer_id = db.Column(db.Integer, db.ForeignKey("volunteers.id"),
                             nullable=False)
    language = db.Column(db.String(20), nullable=False)
    reason = db.Column(db.String(40), nullable=False)
    note = db.Column(db.String(600), default="", nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    resolved = db.Column(db.Boolean, default=False, nullable=False)
    resolution = db.Column(db.String(40), default="", nullable=False)

    word = db.relationship("Word")
    volunteer = db.relationship("Volunteer")


class Word(db.Model):
    __tablename__ = "words"

    id = db.Column(db.Integer, primary_key=True)

    # Not unique on its own any more: the same text can legitimately be an item
    # in two projects. Unique per project instead, see ensure_indexes().
    phrase = db.Column(db.String(600), nullable=False, index=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"),
                           index=True)

    # NULL when the item reads the same for every language, as an English word
    # awaiting translation does. Set when a project supplied a separate file per
    # language and the items themselves differ.
    language = db.Column(db.String(20), index=True)

    # Order within the uploaded file, so a project is worked in the order its
    # author intended. The translation project orders by frequency instead.
    position = db.Column(db.Integer, default=0, nullable=False)

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
        db.Index("ix_word_project", "project_id", "language", "position"),
    )

    candidates = db.relationship("Candidate", back_populates="word",
                                 cascade="all, delete-orphan")
    project = db.relationship("Project")

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
    or a column dropped is a real migration and should be written as one -
    see adopt_orphan_items() below.
    """
    from sqlalchemy import inspect, text

    wanted = {
        "volunteers": {"paused_until": "DATE",
                       "missed_in_a_row": "INTEGER NOT NULL DEFAULT 0",
                       "nudged_on": "DATE"},
        "pending_signups": {"project_ids": "VARCHAR(200) NOT NULL DEFAULT ''",
                            "exclusive_project_id": "INTEGER"},
        "words": {"project_id": "INTEGER",
                  "language": "VARCHAR(20)",
                  "position": "INTEGER NOT NULL DEFAULT 0"},
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


def ensure_indexes():
    """Replace the global unique index on item text with a per-project one.

    An item's text was unique across the whole table, which was right when
    there was one body of work and wrong the moment there were two: the same
    sentence can legitimately be an item in a translation project and in a
    transcription project. Uniqueness still matters within a project, to catch
    a file uploaded twice.

    Cheap because it was a standalone index rather than a table constraint -
    dropping it needs no rebuild of the 478k rows.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(db.engine)
    if "words" not in inspector.get_table_names():
        return []
    changed = []
    indexes = {i["name"]: i for i in inspector.get_indexes("words")}
    old = indexes.get("ix_words_phrase")
    if old is not None and old.get("unique"):
        db.session.execute(text("DROP INDEX ix_words_phrase"))
        db.session.execute(text("CREATE INDEX ix_words_phrase"
                                " ON words (phrase)"))
        changed.append("ix_words_phrase: unique -> plain")
    if "ix_words_project_phrase" not in indexes:
        # Not UNIQUE: existing rows predate project_id and a duplicate would
        # abort the boot. The importer checks for duplicates itself, where it
        # can report them to the person uploading the file.
        db.session.execute(text("CREATE INDEX ix_words_project_phrase"
                                " ON words (project_id, phrase)"))
        changed.append("ix_words_project_phrase")
    if changed:
        db.session.commit()
    return changed


CORE_PROJECT = {
    "slug": "everyday-words",
    "title": "Translate everyday Ghanaian words",
    "summary": ("Machine translation proposed three ways to say each common "
                "English word. Choose the one you would actually use, or type "
                "your own."),
    "item_format": "word",
    "sort_order": 0,
}


def adopt_orphan_items():
    """Give the work collected before projects existed a project to belong to.

    Everything already in the database was one body of work with no name. It
    becomes a project like any other, so nothing downstream needs a special
    case for "the original words".
    """
    # Every language, not only the four with machine translations loaded. The
    # items are English words, answerable in any of them: where no options
    # exist the first speaker types the wording and the next can agree with it.
    from .config import ALL_LANGUAGES

    core = Project.query.filter_by(slug=CORE_PROJECT["slug"]).first()
    if core is None:
        core = Project(status="approved",
                       approved_at=datetime.utcnow(),
                       votes_to_settle=5, has_options=True,
                       **CORE_PROJECT)
        db.session.add(core)
        db.session.flush()

    have = {pl.language for pl in core.languages}
    for code in ALL_LANGUAGES:
        if code not in have:
            db.session.add(ProjectLanguage(project_id=core.id, language=code))

    adopted = (Word.query.filter(Word.project_id.is_(None))
               .update({"project_id": core.id}, synchronize_session=False))

    # And the volunteers. Nothing is sent to someone with no project, so
    # without this every existing volunteer would receive an empty list the
    # morning this deploys - they signed up for this work and are still doing
    # it, so they are opted in to it.
    opted = db.session.query(VolunteerProject.volunteer_id).subquery()
    orphans = (Volunteer.query
               .filter(~Volunteer.id.in_(db.session.query(opted.c.volunteer_id)))
               .all())
    for volunteer in orphans:
        db.session.add(VolunteerProject(volunteer_id=volunteer.id,
                                       project_id=core.id))
    db.session.commit()
    return core, adopted, len(orphans)
