"""The one body of work SHOLA collects, and who it reaches.

SHOLA asks a single question: how do you say this English word in your
language? Everything here used to be about routing between several projects -
which one a volunteer had joined, whose turn it was today, whether one had
bought a run of exclusive attention. None of that exists now. There is one
project, it collects words, and every speaker of every language it covers is
sent from it.

The `Project` row survives as the thing words hang off. It is an internal
anchor, not a feature: `Word.project_id` is on 478,822 rows with 1.4 million
candidates beside them, and rebuilding that table in SQLite to delete a column
nobody sees is a worse idea than leaving it.
"""

from .models import Project, ProjectLanguage, Word, db


def core_project():
    """The words project. Created on first boot, and the only one there is."""
    from .models import CORE_PROJECT

    return Project.query.filter_by(slug=CORE_PROJECT["slug"]).first()


def approved_projects(language=None):
    """Kept so callers reading "the projects in play" still read sensibly.

    There is one, and a language filter it does not collect returns nothing.
    """
    project = core_project()
    if project is None or project.status != "approved":
        return []
    if language:
        got = (ProjectLanguage.query
               .filter_by(project_id=project.id, language=language).first())
        if got is None:
            return []
    return [project]


def has_open_items(project, language):
    """Whether this project still has anything for a speaker of this language."""
    from .tiers import open_query

    return open_query(language, project_id=project.id).limit(1).count() > 0


def active_for(volunteer):
    """Where today's list is drawn from."""
    return approved_projects(volunteer.language)


def item_counts(project):
    """Items per language, for the progress page."""
    rows = (db.session.query(Word.language, db.func.count(Word.id))
            .filter(Word.project_id == project.id)
            .group_by(Word.language).all())
    return {code: n for code, n in rows}
