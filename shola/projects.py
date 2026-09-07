"""Which projects a volunteer works on, and how a day's list is shared out.

A volunteer signs up to share their language. Nothing else is asked of them:
every approved project collecting that language sends them work, and one short
list a day arrives split across those projects as evenly as the numbers allow -
five items across two projects is three and two, and nothing pretends
otherwise.

Volunteers used to choose projects and could opt in and out of them. That was a
question nobody needed to answer. Someone who has agreed to check Twi has
agreed to check Twi; asking them which body of work it belongs to makes them
responsible for a decision that is ours, and a project nobody happened to tick
would sit unanswered for reasons unrelated to whether it mattered.

Two rules bend the split. A project whose queue is dry gives its share to the
others rather than shortening the list. And a project inside its exclusive
window is the only one sent, in every language it covers, until the window
closes - which is what an author with a deadline is given instead of a slice of
everyone's attention.
"""

from datetime import datetime

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


def shares(total, n):
    """Split `total` items across `n` projects, remainder to the first.

    Five across two is [3, 2]. Perfectly even is impossible for most numbers
    and pretending otherwise would mean sending a different amount than the
    volunteer was told.
    """
    if n <= 0 or total <= 0:
        return []
    base, extra = divmod(total, n)
    return [base + (1 if i < extra else 0) for i in range(n)]


def rotate(projects, offset):
    """Rotate the project order so the same project is not always short-changed.

    With five items across two projects one gets three and one gets two. Fixed
    order means the same project is always the one that gets two.
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
