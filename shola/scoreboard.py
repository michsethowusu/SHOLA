"""How often each system's wording is the one a speaker picks.

Every option carries the name of whatever wrote it - a model, or `human`. When
a volunteer answers an item, the options they were choosing between are on the
record, so each answer is a small comparison: one system's wording was picked
and the others were not.

Two numbers matter and they are not the same:

  **offered**  answers where this system had an option in front of the speaker.
  **picked**   of those, how often the speaker's answer was this system's
               wording.

`picked` credits the wording, not the click. If a speaker types out the same
words a model proposed, the model was right, and a scoreboard that only counted
clicks would say it was wrong. Matching is done on the same normalised form the
vote counts use, so case and Unicode form cannot split a hit from a miss.

An answer is counted for every system that offered that wording. Two models
that agree both get the credit, which is correct: neither was wrong.

Nothing here is a judgement about the language. It is a record of what speakers
chose when shown these options, which is the only sense in which one machine
translation is measurably better than another.
"""

from collections import defaultdict

from .consensus import normalise
from .models import Candidate, Evaluation, Project, Word, db

# Not a model. It is the default for options nobody attributed, and it belongs
# on the board as the baseline every model is being compared against.
HUMAN = "human"


def split_sources(source):
    """The systems behind one option.

    Usually one. Where two systems produced the same wording there is no sense
    in showing a speaker the same option twice, so the option is stored once
    with both names against it, separated by a semicolon - and both are
    credited when it is picked, which is right: neither was wrong.
    """
    names = {part.strip() for part in (source or "").split(";")}
    return {n for n in names if n} or {HUMAN}


def _answers(project_id=None, language=None):
    """Answered, un-skipped evaluations, with the item they answered."""
    q = (db.session.query(Evaluation.word_id, Evaluation.language,
                          Evaluation.candidate_id, Evaluation.custom_text,
                          Candidate.text.label("option_text"))
         .outerjoin(Candidate, Evaluation.candidate_id == Candidate.id)
         .filter(Evaluation.skipped.is_(False)))
    if language:
        q = q.filter(Evaluation.language == language)
    if project_id is not None:
        q = q.join(Word, Evaluation.word_id == Word.id) \
             .filter(Word.project_id == project_id)
    return q.all()


def _options(pairs):
    """{(word_id, language): {normalised text: {sources}}} for the pairs given."""
    out = defaultdict(lambda: defaultdict(set))
    if not pairs:
        return out
    word_ids = {word_id for word_id, _ in pairs}
    rows = (db.session.query(Candidate.word_id, Candidate.language,
                             Candidate.text, Candidate.source)
            .filter(Candidate.word_id.in_(word_ids)).all())
    wanted = set(pairs)
    for word_id, language, text, source in rows:
        if (word_id, language) in wanted:
            out[(word_id, language)][normalise(text)] |= split_sources(source)
    return out


def scores(project_id=None, language=None, min_offered=1):
    """A row per system, most picked first.

    Each row: name, offered, picked, rate, and `sole` - the answers where this
    system was the only one offering that wording, so its wins are its own
    rather than shared with a system that said the same thing.
    """
    answers = _answers(project_id, language)
    if not answers:
        return []
    options = _options({(a.word_id, a.language) for a in answers})

    offered = defaultdict(int)
    picked = defaultdict(int)
    sole = defaultdict(int)
    for a in answers:
        by_text = options.get((a.word_id, a.language))
        if not by_text:
            continue                       # nothing was offered; nothing to score
        present = set()
        for sources in by_text.values():
            present |= sources
        # A wording a volunteer typed becomes an option for the next speaker.
        # It was not on screen for this answer, so it is not scored here.
        present.discard("volunteer")
        if not present:
            continue
        for name in present:
            offered[name] += 1
        chosen = normalise(a.custom_text or a.option_text or "")
        if not chosen:
            continue
        winners = by_text.get(chosen, set()) - {"volunteer"}
        for name in winners:
            picked[name] += 1
        if len(winners) == 1:
            sole[next(iter(winners))] += 1

    rows = []
    for name, n in offered.items():
        if n < min_offered:
            continue
        hits = picked.get(name, 0)
        rows.append({"name": name, "offered": n, "picked": hits,
                     "rate": hits / n if n else 0.0,
                     "sole": sole.get(name, 0),
                     "human": name == HUMAN})
    rows.sort(key=lambda r: (-r["rate"], -r["offered"], r["name"]))
    return rows


def head_to_head(project_id=None, language=None):
    """Which system won where two of them offered different wordings.

    Only answers where both were on screen and disagreed are counted, so this
    is not skewed by one of them being offered more often than the other.
    """
    answers = _answers(project_id, language)
    options = _options({(a.word_id, a.language) for a in answers})
    wins = defaultdict(int)
    seen = defaultdict(int)
    for a in answers:
        by_text = options.get((a.word_id, a.language)) or {}
        owner = {}
        for text, sources in by_text.items():
            for name in sources - {"volunteer"}:
                owner.setdefault(name, set()).add(text)
        names = sorted(owner)
        chosen = normalise(a.custom_text or a.option_text or "")
        for i, left in enumerate(names):
            for right in names[i + 1:]:
                if owner[left] == owner[right]:
                    continue               # they said the same thing
                seen[(left, right)] += 1
                if chosen in owner[left] and chosen not in owner[right]:
                    wins[(left, right)] += 1
                elif chosen in owner[right] and chosen not in owner[left]:
                    wins[(right, left)] += 1
    out = []
    for (left, right), n in sorted(seen.items(), key=lambda kv: -kv[1]):
        out.append({"left": left, "right": right, "compared": n,
                    "left_wins": wins.get((left, right), 0),
                    "right_wins": wins.get((right, left), 0)})
    return out


def by_language(project_id=None, min_offered=1):
    """{language code: scores} for a project, for spotting a model that only
    works in one language."""
    codes = [row[0] for row in
             db.session.query(Evaluation.language).distinct().all()]
    out = {}
    for code in sorted(c for c in codes if c):
        rows = scores(project_id, code, min_offered)
        if rows:
            out[code] = rows
    return out


def named_models(project_id=None):
    """Every system name attached to an option, whether it has been seen yet."""
    q = db.session.query(Candidate.source).distinct()
    if project_id is not None:
        q = q.join(Word, Candidate.word_id == Word.id) \
             .filter(Word.project_id == project_id)
    found = set()
    for row in q.all():
        found |= split_sources(row[0])
    return sorted(found - {"volunteer"})


def project_scores(slug, **kwargs):
    project = Project.query.filter_by(slug=slug).first()
    if project is None:
        return None
    return scores(project.id, **kwargs)
