"""Which projects a volunteer works on, and how a day's list is shared out.

A volunteer opts in to one or more projects and receives one short list a day.
That list is split across the projects they joined, as evenly as the numbers
allow - five items across two projects is three and two, and nothing pretends
otherwise.

Two rules bend the split. A project whose queue is dry gives its share to the
others rather than shortening the list. And someone who arrived through a
project's own share link works only on that project until it is finished: the
person who brought them here earned that, and it is the one promise the
platform makes to whoever shares a link.
"""

from datetime import datetime

from .models import Project, ProjectLanguage, VolunteerProject, Word, db


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


def joined(volunteer):
    """The projects this volunteer opted in to, exclusive ones first."""
    rows = (db.session.query(VolunteerProject, Project)
            .join(Project, Project.id == VolunteerProject.project_id)
            .filter(VolunteerProject.volunteer_id == volunteer.id,
                    Project.status == "approved")
            .order_by(VolunteerProject.exclusive.desc(),
                      Project.sort_order, Project.id)
            .all())
    return [(vp, project) for vp, project in rows]


def opt_in(volunteer, project_ids, exclusive_id=None):
    """Join projects. Repeating an existing opt-in is not an error.

    Only projects collecting the volunteer's language are accepted - anything
    else would put items in front of someone who cannot answer them.
    """
    open_ids = {p.id for p in approved_projects(volunteer.language)}
    added = []
    have = {vp.project_id for vp, _ in joined(volunteer)}
    for pid in project_ids:
        pid = int(pid)
        if pid not in open_ids or pid in have:
            continue
        db.session.add(VolunteerProject(volunteer_id=volunteer.id,
                                       project_id=pid,
                                       exclusive=(pid == exclusive_id)))
        added.append(pid)
    if added:
        db.session.commit()
    return added


def opt_out(volunteer, project_id):
    """Leave a project. Answers already given stay where they are."""
    n = (VolunteerProject.query
         .filter_by(volunteer_id=volunteer.id, project_id=int(project_id))
         .delete())
    if n:
        db.session.commit()
    return n


def has_open_items(project, language):
    """Whether this project still has anything for a speaker of this language."""
    from .tiers import open_query
    return open_query(language, project_id=project.id).limit(1).count() > 0


def active_for(volunteer):
    """Projects to draw today's list from.

    An unfinished exclusive project is the whole list. Once it runs out, the
    rest open up - the promise was priority, not permanence.
    """
    pairs = joined(volunteer)
    if not pairs:
        return []
    exclusive = [p for vp, p in pairs
                 if vp.exclusive and has_open_items(p, volunteer.language)]
    if exclusive:
        return exclusive
    return [p for _vp, p in pairs]


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


def mark_announced(project):
    project.announced_at = datetime.utcnow()
    db.session.commit()


def item_counts(project):
    """Items per language, for the admin dashboard and the project page."""
    rows = (db.session.query(Word.language, db.func.count(Word.id))
            .filter(Word.project_id == project.id)
            .group_by(Word.language).all())
    return {(code or "all"): n for code, n in rows}
