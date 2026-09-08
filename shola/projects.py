"""Which projects a volunteer works on, and where a day's list comes from.

A volunteer signs up to share their language. Nothing else is asked of them:
every approved project collecting that language draws on them, and one short
list arrives on the days they chose.

Each list comes from a **single** project. Five items split between two asks
somebody to change task mid-list for no reason - translating a word and
translating a sentence are different jobs, and five of one is easier than two
of one and three of the other.

The next list comes from a different project. Not the next day: the next list.
Somebody who answers their five on the site and asks for more straight away
gets the other project, the same as if they had waited for the next email. The
cursor counts lists rather than days for exactly that reason, and is offset per
volunteer so two people starting out are not both handed the same project.

Volunteers used to choose projects and could opt in and out of them. That was a
question nobody needed to answer. Someone who has agreed to check Twi has
agreed to check Twi; asking them which body of work it belongs to makes them
responsible for a decision that is ours, and a project nobody happened to tick
would sit unanswered for reasons unrelated to whether it mattered.

One rule overrides the rotation: a project inside its exclusive window is the
only one sent, in every language it covers, until the window closes - which is
what an author with a deadline is given instead of a slice of everyone's
attention.
"""

from datetime import date

from .models import Project, ProjectLanguage, Word, db


def approved_projects(language=None):
    """Projects open for joining, in the order the sign-up page shows them.

    Filtered by language when given: there is no point offering someone a
    project that collects nothing in the language they speak.
    """
    q = Project.query.filter(Project.status == "approved")
    if language:
        q = (q.join(ProjectLanguage,
                    ProjectLanguage.project_id == Project.id)
             .filter(ProjectLanguage.language == language))
    return q.order_by(Project.sort_order, Project.id).all()


def has_open_items(project, language):
    """Whether this project still has anything for a speaker of this language."""
    from .tiers import open_query
    return open_query(language, project_id=project.id).limit(1).count() > 0


def exclusive_project(language=None, today=None):
    """The project currently holding an exclusive window, if any.

    Filtered by language, because a window only silences the other projects for
    the speakers this one can actually use. An Ewe project running exclusively
    should not leave Kasem speakers with nothing to do.

    If two windows somehow overlap, the one ending soonest wins: it is the one
    with least time left to make use of it.
    """
    today = today or date.today()
    q = (Project.query
         .filter(Project.status == "approved",
                 Project.exclusive_until.isnot(None),
                 Project.exclusive_until >= today))
    if language:
        q = (q.join(ProjectLanguage, ProjectLanguage.project_id == Project.id)
             .filter(ProjectLanguage.language == language))
    return q.order_by(Project.exclusive_until, Project.id).first()


def active_for(volunteer):
    """Projects to draw today's list from, for this volunteer's language.

    A live exclusive window is the whole list, as long as it still has items
    this speaker can answer - an exclusive project that has run dry in their
    language would otherwise send them nothing at all, which serves nobody.
    """
    language = volunteer.language
    pinned = exclusive_project(language)
    if pinned is not None and has_open_items(pinned, language):
        return [pinned]
    return approved_projects(language)


def rotate(projects, offset):
    """Rotate the project order so the same one is not always first.

    A day's list comes from a single project, so whichever project sorts first
    would get every list for ever without this.
    """
    if not projects:
        return projects
    k = offset % len(projects)
    return projects[k:] + projects[:k]


def item_counts(project):
    """Items per language, for the admin dashboard and the project page."""
    rows = (db.session.query(Word.language, db.func.count(Word.id))
            .filter(Word.project_id == project.id)
            .group_by(Word.language).all())
    return {(code or "all"): n for code, n in rows}
