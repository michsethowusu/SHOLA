"""End-to-end checks for projects: submission, approval, mixing and reporting.

Kept apart from test_flow.py because the fixture is different: this one builds
several projects and asks how a day's list is divided between them, where the
other file works one project hard.
"""

import os
import sys
import tempfile
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shola import create_app                                    # noqa: E402
from shola.config import Config                                 # noqa: E402
from shola import consensus, importer                           # noqa: E402
from shola.assignment import record_verdict                     # noqa: E402
from shola.models import (CORE_PROJECT, Candidate, Evaluation, Flag,  # noqa: E402
                          Project, ProjectLanguage, Volunteer, Word,
                          WordState, db)
from shola.projects import (active_for, joined, opt_in, opt_out,  # noqa: E402
                            shares)
from shola.tiers import (daily_quota, refresh_word, state_for,   # noqa: E402
                         top_up, votes_needed)

PASSED = []


def check(label, condition, detail=""):
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail and not condition
                                   else ""))
    PASSED.append(bool(condition))
    return bool(condition)


def make_app():
    tmp = tempfile.mkdtemp()

    class T(Config):
        TESTING = True
        SECRET_KEY = "test"
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{tmp}/t.db"
        WORDS_PER_DAY = 6
        SMTP_USER = "x@example.com"
        SMTP_PASSWORD = "y"
        ADMIN_EMAILS = "boss@example.com"

    return create_app(T)


def core():
    return Project.query.filter_by(slug=CORE_PROJECT["slug"]).first()


def make_project(slug, title, langs, options=True, n=40, fmt="sentence",
                 threshold=3, status="approved"):
    project = Project(slug=slug, title=title, item_format=fmt,
                      has_options=options, votes_to_settle=threshold,
                      status=status, sort_order=50)
    db.session.add(project)
    db.session.flush()
    for code in langs:
        db.session.add(ProjectLanguage(project_id=project.id, language=code))
    db.session.flush()
    for code in langs:
        rows = [(f"{slug} item {i} for {code}",
                 [f"{code} option {i}a", f"{code} option {i}b"] if options
                 else [])
                for i in range(n)]
        importer.import_rows(project, code, rows)
    db.session.commit()
    return project


def volunteer(email, language="twi", project_ids=(), exclusive_id=None):
    v = Volunteer(name="Test Person", email=email, language=language)
    db.session.add(v)
    db.session.commit()
    opt_in(v, list(project_ids), exclusive_id=exclusive_id)
    return v


def main():
    app = make_app()
    ok = True

    print("\nthe work that existed before projects belongs to one now")
    with app.app_context():
        c = core()
        ok &= check("a core project exists", c is not None and c.approved)
        ok &= check("titled as the job, not the dataset",
                    c.title == "Translate everyday Ghanaian words", c.title)
        ok &= check("and it collects every language",
                    c.languages.count() == len(app.config["ALL_LANGUAGES"]),
                    str(c.languages.count()))

    print("\na project is a CSV per language")
    with app.app_context():
        rows, problems = importer.parse(
            "text,option1,option2\nmarket,dwaso,dwabo\nwater,nsuo,nsu\n".encode())
        ok &= check("a well-formed file parses", not problems and len(rows) == 2,
                    str(problems))
        sentences = make_project("read-sentences", "Read these sentences aloud",
                                 ["twi", "ewe"], options=False, n=30)
        ok &= check("items are created per language",
                    Word.query.filter_by(project_id=sentences.id).count() == 60,
                    str(Word.query.filter_by(project_id=sentences.id).count()))
        ok &= check("each item is tied to its language",
                    Word.query.filter_by(project_id=sentences.id,
                                         language="ewe").count() == 30)
        ok &= check("and carries its file order",
                    Word.query.filter_by(project_id=sentences.id)
                    .order_by(Word.position).first().position == 1)

    print("\nitems only reach speakers of the language they were filed under")
    with app.app_context():
        ga_speaker = volunteer("ga@example.com", "ga",
                               [core().id, Project.query.filter_by(
                                   slug="read-sentences").first().id])
        # read-sentences collects Twi and Ewe only, so opting in is refused.
        ok &= check("a project is not joinable in a language it ignores",
                    len(joined(ga_speaker)) == 1,
                    str([p.slug for _v, p in joined(ga_speaker)]))

    print("\none list, shared between the projects someone joined")
    with app.app_context():
        from shola.models import CORE_PROJECT as CP
        seed_core(30)
        sentences = Project.query.filter_by(slug="read-sentences").first()
        both = volunteer("both@example.com", "twi",
                         [core().id, sentences.id])
        n = top_up(both)
        ok &= check("the list is the configured length",
                    n == app.config["WORDS_PER_DAY"], f"leased {n}")
        by_project = {}
        for a in both.assignments:
            by_project[a.word.project_id] = by_project.get(a.word.project_id,
                                                           0) + 1
        ok &= check("drawn from both projects", len(by_project) == 2,
                    str(by_project))
        ok &= check("split as evenly as six across two allows",
                    sorted(by_project.values()) == [3, 3], str(by_project))

    print("\nan odd number cannot split evenly, and does not pretend to")
    ok &= check("five across two is three and two",
                shares(5, 2) == [3, 2], str(shares(5, 2)))
    ok &= check("seven across three is 3/2/2",
                shares(7, 3) == [3, 2, 2], str(shares(7, 3)))
    ok &= check("nothing is lost in the split",
                sum(shares(7, 3)) == 7 and sum(shares(5, 2)) == 5)

    print("\na dry project gives its share to the others")
    with app.app_context():
        tiny = make_project("tiny-job", "Check a handful of names", ["twi"],
                            n=2, threshold=1)
        mixed = volunteer("mixed@example.com", "twi", [core().id, tiny.id])
        n = top_up(mixed)
        ok &= check("the list is still full length",
                    n == app.config["WORDS_PER_DAY"], f"leased {n}")
        from_tiny = sum(1 for a in mixed.assignments
                        if a.word.project_id == tiny.id)
        ok &= check("taking everything the small project had",
                    from_tiny == 2, str(from_tiny))

    print("\na share link puts one project first until it is finished")
    with app.app_context():
        small = make_project("one-off", "Name these market goods", ["twi"],
                             n=3, threshold=1)
        guest = volunteer("guest@example.com", "twi",
                          [core().id, small.id], exclusive_id=small.id)
        ok &= check("only the exclusive project is active",
                    [p.slug for p in active_for(guest)] == ["one-off"],
                    str([p.slug for p in active_for(guest)]))
        n = top_up(guest)
        ok &= check("so the whole list comes from it",
                    all(a.word.project_id == small.id
                        for a in guest.assignments) and n == 3, f"{n} leased")
        # Answer all three: the promise was priority, not permanence.
        for a in list(guest.assignments):
            record_verdict(guest, a.word_id, custom_text="an answer")
        ok &= check("once it runs out the rest open up",
                    len(active_for(guest)) == 2,
                    str([p.slug for p in active_for(guest)]))
        n = top_up(guest)
        ok &= check("and the next list is drawn from them", n > 0, f"{n}")

    print("\nverification belongs to projects that offer options")
    with app.app_context():
        typed = Project.query.filter_by(slug="read-sentences").first()
        ok &= check("a typed-answer project verifies nothing",
                    not typed.has_options)
        item = Word.query.filter_by(project_id=typed.id,
                                    language="twi").first()
        for i in range(6):
            v = volunteer(f"typer{i}@example.com", "twi", [typed.id])
            db.session.add(Evaluation(volunteer_id=v.id, word_id=item.id,
                                      language="twi",
                                      custom_text="the same wording"))
        db.session.commit()
        refresh_word(item.id, "twi")
        ok &= check("even six matching answers are not consensus",
                    consensus.best(item.id, "twi") is None)
        ok &= check("and none of it counts as verified",
                    consensus.verified_count("twi", project_id=typed.id) == 0)
        ok &= check("but every answer is exported",
                    len(list(consensus.typed_rows("twi",
                                                  project_id=typed.id))) == 6,
                    str(len(list(consensus.typed_rows("twi",
                                                      project_id=typed.id)))))
        ok &= check("the item does close, so it stops being handed out",
                    state_for(item.id, "twi").done)

    print("\neach project sets its own bar for agreement")
    with app.app_context():
        strict = make_project("strict-job", "Check these place names", ["twi"],
                              n=5, threshold=2)
        ok &= check("a project's own threshold is used",
                    votes_needed(strict) == 2, str(votes_needed(strict)))
        ok &= check("and the core project keeps five",
                    votes_needed(core()) == 5, str(votes_needed(core())))
        item = Word.query.filter_by(project_id=strict.id).first()
        opt = item.candidates[0]
        v1 = volunteer("s1@example.com", "twi", [strict.id])
        record_verdict(v1, item.id, candidate_id=opt.id)
        ok &= check("one vote does not settle it",
                    not state_for(item.id, "twi").done)
        v2 = volunteer("s2@example.com", "twi", [strict.id])
        record_verdict(v2, item.id, candidate_id=opt.id)
        ok &= check("two does, in a project that asked for two",
                    state_for(item.id, "twi").done)
        ok &= check("and it counts as verified",
                    consensus.verified_count("twi",
                                             project_id=strict.id) == 1)

    print("\nreporting an item takes it out of everyone's queue")
    with app.app_context():
        reporter = volunteer("reporter@example.com", "twi", [core().id])
        top_up(reporter)
        target = reporter.assignments.first().word_id
        tok = token_for(app, reporter)
    client = app.test_client()
    r = client.post(f"/w/{tok}/{target}/flag",
                    data={"reason": "nonsense", "note": "not a real word"})
    ok &= check("the report is accepted", r.status_code in (200, 302),
                f"HTTP {r.status_code}")
    with app.app_context():
        ok &= check("it is recorded",
                    Flag.query.filter_by(word_id=target).count() == 1)
        ok &= check("it leaves the reporter's list",
                    Word.query.filter(Word.id == target).first() is not None
                    and not any(a.word_id == target for a in
                                Volunteer.query.filter_by(
                                    email="reporter@example.com").first()
                                .pending_today()))
        other = volunteer("other-twi@example.com", "twi", [core().id])
        top_up(other)
        ok &= check("and nobody else is asked about it",
                    target not in {a.word_id for a in other.assignments})
        ok &= check("no verdict was recorded for it",
                    Evaluation.query.filter_by(word_id=target).count() == 0)

    print("\nsubmitting a project, then approving it")
    with app.app_context():
        before = Project.query.count()
    fresh = app.test_client()
    csv_bytes = b"text,option1,option2\nakwaaba,welcome,you are welcome\nmedaase,thanks,thank you\n"
    r = fresh.post("/submit", data={
        "title": "Check these greetings in Twi",
        "summary": "Two ways each. Pick the natural one.",
        "item_format": "word", "answer_mode": "choose",
        "votes_to_settle": "3", "languages": ["twi"],
        "name": "Kofi", "email": "kofi@example.com", "org": "Kofi Labs",
        "file_twi": (io_bytes(csv_bytes), "greetings.csv"),
    }, content_type="multipart/form-data", follow_redirects=True)
    ok &= check("the submission is accepted", r.status_code == 200
                and b"Submitted" in r.data, f"HTTP {r.status_code}")
    with app.app_context():
        ok &= check("a project is created", Project.query.count() == before + 1)
        proposed = Project.query.filter_by(status="pending").first()
        ok &= check("waiting for a decision, not live",
                    proposed is not None and proposed.status == "pending")
        ok &= check("with its items loaded",
                    proposed.item_count() == 2, str(proposed.item_count()))
        ok &= check("and its own threshold", proposed.votes_to_settle == 3)
        pid = proposed.id
        # Nobody is offered a pending project.
        waiting = volunteer("waiting@example.com", "twi", [pid])
        ok &= check("nobody can opt in before approval",
                    len(joined(waiting)) == 0,
                    str([p.slug for _v, p in joined(waiting)]))

    print("\na bad file is refused with the line numbers, not half imported")
    with app.app_context():
        before = Word.query.count()
    r = fresh.post("/submit", data={
        "title": "A file with problems in it",
        "item_format": "word", "answer_mode": "choose",
        "languages": ["twi"], "email": "kofi@example.com",
        "file_twi": (io_bytes(b"market,dwaso\nwater\n"), "bad.csv"),
    }, content_type="multipart/form-data")
    ok &= check("refused", r.status_code == 400, f"HTTP {r.status_code}")
    ok &= check("naming the line", b"Line 1" in r.data)
    with app.app_context():
        ok &= check("and nothing was written",
                    Word.query.count() == before, str(Word.query.count()))

    print("\nthe admin side needs an allowlisted address")
    anon = app.test_client()
    r = anon.get("/admin/dashboard")
    ok &= check("the dashboard is closed to strangers",
                r.status_code == 302 and "/admin" in r.headers["Location"])
    with app.app_context():
        from shola.admin import make_link
        with app.test_request_context():
            good = make_link("boss@example.com")
            bad = make_link("nobody@example.com")
    r = anon.get("/" + good.split("/", 3)[3], follow_redirects=True)
    ok &= check("an allowlisted link signs in", b"Waiting for you" in r.data)
    stranger = app.test_client()
    r = stranger.get("/" + bad.split("/", 3)[3], follow_redirects=True)
    ok &= check("a link for anyone else does not",
                b"Waiting for you" not in r.data)

    print("\napproving a project lets volunteers opt in")
    r = anon.post(f"/admin/project/{pid}/decide",
                  data={"action": "approve", "note": "looks fine"},
                  follow_redirects=True)
    with app.app_context():
        proj = db.session.get(Project, pid)
        ok &= check("it is approved", proj.status == "approved", proj.status)
        joiner = Volunteer.query.filter_by(email="waiting@example.com").first()
        ok &= check("and now it can be joined",
                    len(opt_in(joiner, [pid])) == 1)
        tok = token_for(app, joiner)
    r = app.test_client().get(f"/w/{tok}/projects")
    ok &= check("it shows on the volunteer's own page",
                b"Check these greetings in Twi" in r.data)

    print("\nempty projects cannot be approved")
    with app.app_context():
        hollow = Project(slug="hollow", title="A project with no items",
                         status="pending")
        db.session.add(hollow)
        db.session.flush()
        db.session.add(ProjectLanguage(project_id=hollow.id, language="twi"))
        db.session.commit()
        hid = hollow.id
    r = anon.post(f"/admin/project/{hid}/decide", data={"action": "approve"},
                  follow_redirects=True)
    with app.app_context():
        ok &= check("refused, with a reason",
                    db.session.get(Project, hid).status == "pending"
                    and b"no items loaded" in r.data)

    print("\nopting out hands back work from the project you left")
    with app.app_context():
        leaver = Volunteer.query.filter_by(email="both@example.com").first()
        sentences = Project.query.filter_by(slug="read-sentences").first()
        tok = token_for(app, leaver)
        held_before = {a.word.project_id for a in leaver.pending_today()}
        ok &= check("they hold work from both", len(held_before) == 2,
                    str(held_before))
    r = app.test_client().post(f"/w/{tok}/projects",
                               data={"projects": [str(core_id(app))]},
                               follow_redirects=True)
    with app.app_context():
        leaver = Volunteer.query.filter_by(email="both@example.com").first()
        held_after = {a.word.project_id for a in leaver.pending_today()}
        ok &= check("afterwards only the one they kept",
                    held_after == {core_id(app)}, str(held_after))
    r = app.test_client().post(f"/w/{tok}/projects", data={},
                               follow_redirects=True)
    with app.app_context():
        leaver = Volunteer.query.filter_by(email="both@example.com").first()
        ok &= check("and leaving everything is refused",
                    len(joined(leaver)) >= 1)

    print("\nthe API answers in projects")
    api = app.test_client()
    r = api.get("/api/projects")
    ok &= check("projects are listed", r.status_code == 200
                and b"everyday-words" in r.data)
    data = r.get_json()
    entry = next(p for p in data["projects"] if p["slug"] == "read-sentences")
    ok &= check("a typed project says it cannot verify",
                entry["verifiable"] is False
                and entry["votes_to_verify"] is None, str(entry))
    r = api.get("/api/items/read-sentences/twi")
    ok &= check("its answers come back as typed, not verified",
                r.get_json()["answers"] == "typed"
                and r.get_json()["verified"] is False)
    ok &= check("and say so in a note", "not consensus"
                in r.get_json()["note"])
    r = api.get("/api/items/strict-job/twi")
    ok &= check("a project with options returns verified answers",
                r.get_json()["answers"] == "verified"
                and r.get_json()["min_votes"] == 2, str(r.get_json())[:120])
    r = api.get("/api/items/read-sentences/ga")
    ok &= check("a language the project ignores 404s", r.status_code == 404)
    r = api.get("/api/items/nope/twi")
    ok &= check("an unknown project 404s", r.status_code == 404)
    r = api.get("/api/items/strict-job/twi?format=csv")
    ok &= check("csv works too", r.status_code == 200
                and b"item,answer,votes" in r.data)
    r = api.get("/api/words/twi")
    ok &= check("the old words endpoint still answers", r.status_code == 200)

    print("\nthe public pages hold together")
    for path in ("/", "/projects", "/projects/everyday-words", "/stats",
                 "/api", "/submit", "/join"):
        r = api.get(path)
        ok &= check(f"{path} renders", r.status_code == 200,
                    f"HTTP {r.status_code}")

    print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
    print(f"{sum(PASSED)}/{len(PASSED)} checks passed")
    return 0 if ok else 1


def seed_core(n):
    """Words for the core project, as the real import would create them."""
    from shola.config import LANGUAGES
    c = core()
    for i in range(n):
        w = Word(phrase=f"core word {i}", frequency=float(n - i),
                 occurrences=n - i, tier=1, project_id=c.id)
        db.session.add(w)
        db.session.flush()
        for lang in LANGUAGES:
            for pos in (1, 2, 3):
                db.session.add(Candidate(word_id=w.id, language=lang,
                                         position=pos,
                                         text=f"{lang}-core-{i}-{pos}"))
    db.session.commit()


def token_for(app, volunteer):
    from shola.mailer import make_token
    with app.test_request_context():
        return make_token(volunteer)


def core_id(app):
    with app.app_context():
        return core().id


def io_bytes(data):
    import io
    return io.BytesIO(data)


if __name__ == "__main__":
    sys.exit(main())
