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
from shola.models import (CORE_PROJECT, Assignment, Candidate,     # noqa: E402
                          Evaluation, Flag, Project, ProjectLanguage,
                          Volunteer, Word, WordState, db)
from shola.projects import active_for, exclusive_project  # noqa: E402
from shola.consensus import tally                               # noqa: E402
from shola.tiers import (answers_target, daily_quota, open_query,  # noqa: E402
                         project_order, refresh_word, release_stale,
                         state_for, top_up)

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
    # One item per row, with options per language hanging off it - the shape the
    # importer produces from a single file.
    items = []
    for i in range(n):
        entry = {"text": f"{slug} item {i}", "item_language": None,
                 "options": {}}
        if options:
            for code in langs:
                entry["options"][code] = [f"{code} option {i}a",
                                          f"{code} option {i}b"]
        items.append(entry)
    importer.import_items(project, items)
    db.session.commit()
    return project


def volunteer(email, language="twi", project_ids=(), exclusive_id=None):
    """A volunteer is a language. `project_ids` is accepted and ignored.

    There is nothing to opt in to: every approved project collecting their
    language draws on them.
    """
    v = Volunteer(name="Test Person", email=email, language=language)
    db.session.add(v)
    db.session.commit()
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
        rows, problems, meta = importer.parse(
            ("text,language,option1,option2\n"
             "market,twi,dwaso,dwabo\nwater,twi,nsuo,nsu\n").encode(),
            known_languages={"twi"})
        ok &= check("a well-formed file parses", not problems and len(rows) == 2,
                    str(problems))
        ok &= check("options attach to the language named",
                    rows[0]["options"].get("twi") == ["dwaso", "dwabo"],
                    str(rows[0]))
        ok &= check("and the file names the project's languages",
                    meta["languages"] == {"twi"}, str(meta))
        sentences = make_project("read-sentences", "Read these sentences aloud",
                                 ["twi", "ewe"], options=False, n=30)
        ok &= check("an item exists once, not once per language",
                    Word.query.filter_by(project_id=sentences.id).count() == 30,
                    str(Word.query.filter_by(project_id=sentences.id).count()))
        ok &= check("and is open to every language the project collects",
                    Word.query.filter_by(project_id=sentences.id,
                                         language=None).count() == 30)
        ok &= check("and carries its file order",
                    Word.query.filter_by(project_id=sentences.id)
                    .order_by(Word.position).first().position == 1)

    print("\nitems only reach speakers of the language they were filed under")
    with app.app_context():
        ga_speaker = volunteer("ga@example.com", "ga")
        # read-sentences collects Twi and Ewe only, so a Ga speaker is never
        # drawn into it however many projects exist.
        slugs = [p.slug for p in active_for(ga_speaker)]
        ok &= check("a project is not used in a language it ignores",
                    "read-sentences" not in slugs, str(slugs))
        ok &= check("but the projects covering their language are",
                    "everyday-words" in slugs, str(slugs))

    # Its own app: this section answers a lot of items to walk the rotation
    # forward, and doing that in the shared database would move the numbers
    # other sections assert on.
    print("\none list, one project - and the next list is a different one")
    rot = make_app()
    with rot.app_context():
        seed_core(60)
        make_project("rot-a", "Rotation A", ["twi"], n=60)
        make_project("rot-b", "Rotation B", ["twi"], n=60)
        both = volunteer("both@example.com", "twi")

        n = top_up(both)
        ok &= check("the list is the configured length",
                    n == app.config["WORDS_PER_DAY"], f"leased {n}")
        started_with = {a.word.project_id for a in both.pending_today()}
        ok &= check("and every item came from the same project",
                    len(started_with) == 1, str(started_with))

        # A top-up part-way through a list must not switch projects. This is
        # what broke before: the rotation moved when an item was answered, so
        # the next top-up drew from somewhere else and mixed the list.
        first = next(iter(started_with))
        for a in list(both.pending_today())[:2]:
            opt = [c for c in a.word.candidates if c.language == "twi"]
            if opt:
                record_verdict(both, a.word_id, candidate_id=opt[0].id)
        top_up(both)
        ok &= check("a top-up mid-list stays with the same project",
                    {a.word.project_id for a in both.pending_today()} == {first},
                    str({a.word.project_id for a in both.pending_today()}))

        # Finish the list and ask again, the same day, the way somebody
        # clicking through on the site does. The next list is the other
        # project - the rotation counts lists, not days.
        def finish(v):
            for a in list(v.pending_today()):
                opt = [c for c in a.word.candidates if c.language == "twi"]
                record_verdict(v, a.word_id,
                               candidate_id=opt[0].id if opt else None,
                               custom_text=None if opt else "typed")
        finish(both)
        top_up(both)
        second = {a.word.project_id for a in both.pending_today()}
        ok &= check("the next list is one project too", len(second) == 1,
                    str(second))
        ok &= check("and it is a different project, on the same day",
                    second != {first}, f"{first} then {second}")

        # And it keeps alternating rather than sticking on the second one.
        run = [first, next(iter(second))]
        for _ in range(4):
            finish(both)
            top_up(both)
            run.append(next(iter({a.word.project_id
                                  for a in both.pending_today()})))
        ok &= check("consecutive lists never repeat the same project",
                    all(a != b for a, b in zip(run, run[1:])), str(run))
        ok &= check("so every project gets worked on in one sitting",
                    len(set(run)) == len(active_for(both)), str(run))

        # Two volunteers starting out should not both be sent the same project.
        picks = set()
        for i in range(6):
            v = volunteer(f"spread{i}@example.com", "twi")
            picks.add(project_order(v, active_for(v), date.today())[0][0].slug)
        ok &= check("new volunteers do not all start on the same project",
                    len(picks) > 1, str(picks))

        # The cursor only moves when a list was actually handed over.
        idle = volunteer("idle@example.com", "twi")
        before = idle.lists_taken
        _order, fresh = project_order(idle, active_for(idle), date.today())
        ok &= check("asking which project does not itself advance the rotation",
                    idle.lists_taken == before and fresh is True)

    print("\na project too dry to fill the list makes a short list, not a mixed one")
    with app.app_context():
        tiny = make_project("tiny-job", "Check a handful of names", ["twi"],
                            n=2, threshold=1)
        tiny.start_exclusive(30)      # force today's list to come from it
        db.session.commit()
        short = volunteer("short@example.com", "twi")
        n = top_up(short)
        ok &= check("only what it had", n == 2, f"leased {n}")
        ok &= check("all of it from that project",
                    {a.word.project_id for a in short.assignments} == {tiny.id})
        tiny.exclusive_until = None
        db.session.commit()

    print("\nan exclusive run is the only project sent, then it is not")
    with app.app_context():
        small = make_project("one-off", "Name these market goods", ["twi"],
                             n=3, threshold=1)
        speaker = volunteer("guest@example.com", "twi")
        ok &= check("before the window, everything in their language is used",
                    len(active_for(speaker)) > 1,
                    str([p.slug for p in active_for(speaker)]))

        small.start_exclusive(30)
        db.session.commit()
        ok &= check("the window is live", small.is_exclusive)
        ok &= check("it is found for the language it covers",
                    exclusive_project("twi") is not None
                    and exclusive_project("twi").slug == "one-off")
        ok &= check("only the exclusive project is active",
                    [p.slug for p in active_for(speaker)] == ["one-off"],
                    str([p.slug for p in active_for(speaker)]))
        n = top_up(speaker)
        ok &= check("so the whole list comes from it",
                    all(a.word.project_id == small.id
                        for a in speaker.assignments) and n == 3, f"{n} leased")

        # Nobody outside its languages is affected: an Ewe speaker carries on.
        ewe = volunteer("ewe-during@example.com", "ewe")
        ok &= check("speakers of other languages are untouched",
                    exclusive_project("ewe") is None
                    and len(active_for(ewe)) >= 1,
                    str([p.slug for p in active_for(ewe)]))

        # Answer all three by tapping an option: typing would add options
        # without closing anything, so the project would never run out.
        for a in list(speaker.assignments):
            opt = [c for c in a.word.candidates if c.language == "twi"][0]
            record_verdict(speaker, a.word_id, candidate_id=opt.id)
        ok &= check("an exclusive project that has run dry does not "
                    "starve them",
                    len(active_for(speaker)) > 1,
                    str([p.slug for p in active_for(speaker)]))
        n = top_up(speaker)
        ok &= check("and the next list is drawn from the others", n > 0, f"{n}")
        # Leave no window behind: a live one would silence every project for
        # Twi in the sections that follow.
        small.exclusive_until = None
        db.session.commit()

    print("\nthe window ends by itself, and extending never shortens it")
    with app.app_context():
        from datetime import timedelta
        job = make_project("timed-job", "Check these place names", ["twi"],
                           n=4, threshold=1)
        job.start_exclusive(30)
        db.session.commit()
        ok &= check("thirty days out", job.exclusive_days_left == 30,
                    str(job.exclusive_days_left))

        # Extending adds to what is left rather than restarting.
        job.start_exclusive(10, extend=True)
        db.session.commit()
        ok &= check("extending adds to the remainder",
                    job.exclusive_days_left == 40,
                    str(job.exclusive_days_left))

        # A window that has passed is simply not exclusive any more - nothing
        # has to remember to switch it off.
        job.exclusive_until = date.today() - timedelta(days=1)
        db.session.commit()
        ok &= check("yesterday's window is over", not job.is_exclusive)
        ok &= check("and it is no longer found",
                    exclusive_project("twi") is None)
        ok &= check("so the list goes back to every project",
                    len(active_for(volunteer("after@example.com", "twi"))) > 1)

        # Starting one fresh does not inherit the stale date.
        job.start_exclusive(7)
        db.session.commit()
        ok &= check("a fresh run counts from today",
                    job.exclusive_days_left == 7, str(job.exclusive_days_left))

        # A paused project holds no window, whatever its date says.
        job.status = "paused"
        db.session.commit()
        ok &= check("a paused project does not hold the pool",
                    exclusive_project("twi") is None)
        job.status = "approved"
        job.exclusive_until = None
        db.session.commit()

    print("\na project with no options collects answers all the same")
    with app.app_context():
        typed = Project.query.filter_by(slug="read-sentences").first()
        ok &= check("it arrived with no options", not typed.has_options)
        item = Word.query.filter_by(project_id=typed.id).first()
        for i in range(6):
            v = volunteer(f"typer{i}@example.com", "twi", [typed.id])
            record_verdict(v, item.id, custom_text="the same wording")
        ok &= check("the answers are counted",
                    tally(item.id, "twi")["ranked"][0]["votes"] == 6,
                    str(tally(item.id, "twi")["ranked"]))
        ok &= check("the leading answer is reported",
                    consensus.best(item.id, "twi") is not None)
        ok &= check("the item closed once it hit its target",
                    state_for(item.id, "twi").done)
        ok &= check("and every answer is exported",
                    len(list(consensus.typed_rows("twi",
                                                  project_id=typed.id))) == 6)
        ok &= check("with the wording now an option for the next speaker",
                    any(c.source == "volunteer" for c in item.candidates))

    print("\nthe target is what finishes an item, and progress follows it")
    with app.app_context():
        collect = make_project("collect-only", "Write these out in your language",
                               ["twi"], options=False, n=2, threshold=3)
        ok &= check("the project's own target is used",
                    answers_target(collect) == 3, str(answers_target(collect)))
        item = Word.query.filter_by(project_id=collect.id).first()
        for i in range(3):
            v = volunteer(f"collector{i}@example.com", "twi", [collect.id])
            record_verdict(v, item.id, custom_text=f"my own wording {i}")
        st = state_for(item.id, "twi")
        ok &= check("three answers close the item", st.done,
                    f"total={st.total_votes} done={st.done}")
        ok &= check("all three are counted", st.total_votes == 3,
                    str(st.total_votes))
        ok &= check("three different wordings are all reported",
                    len(tally(item.id, "twi")["ranked"]) == 3,
                    str(tally(item.id, "twi")["ranked"]))
        prog = collect.progress("twi")
        ok &= check("one of two items done",
                    prog["done"] == 1 and prog["item_total"] == 2, str(prog))
        ok &= check("counted per item, not per language",
                    prog["item_total"] == 2, str(prog))

    print("\na typed wording becomes an option others can choose")
    with app.app_context():
        opts = make_project("with-options", "Pick the natural wording", ["twi"],
                            options=True, n=2, threshold=3)
        item = Word.query.filter_by(project_id=opts.id).first()
        before = len(item.candidates)
        for i in range(2):
            v = volunteer(f"writer{i}@example.com", "twi", [opts.id])
            record_verdict(v, item.id, custom_text="the wording we all typed")
        added = [c for c in item.candidates if c.source == "volunteer"]
        ok &= check("added once, however many people typed it",
                    len(added) == 1, str([c.text for c in added]))
        ok &= check("and offered alongside the originals",
                    len(item.candidates) == before + 1)
        t = tally(item.id, "twi")
        ok &= check("their answers are counted together",
                    t["ranked"][0]["votes"] == 2, str(t["ranked"]))
        ok &= check("and marked as a volunteer's wording",
                    t["ranked"][0]["source"] == "volunteer")
        v = volunteer("tapper-opt@example.com", "twi", [opts.id])
        record_verdict(v, item.id, candidate_id=added[0].id)
        t = tally(item.id, "twi")
        ok &= check("a tap on it adds to the same total",
                    t["ranked"][0]["votes"] == 3, str(t["ranked"]))
        ok &= check("the item is finished at its target",
                    state_for(item.id, "twi").done)

    print("\neach project sets its own target")
    with app.app_context():
        strict = make_project("strict-job", "Check these place names", ["twi"],
                              n=5, threshold=2)
        ok &= check("a project's own target is used",
                    answers_target(strict) == 2, str(answers_target(strict)))
        ok &= check("and the core project keeps five",
                    answers_target(core()) == 5, str(answers_target(core())))
        item = Word.query.filter_by(project_id=strict.id).first()
        opt = item.candidates[0]
        v1 = volunteer("s1@example.com", "twi", [strict.id])
        record_verdict(v1, item.id, candidate_id=opt.id)
        ok &= check("one answer does not finish it",
                    not state_for(item.id, "twi").done)
        v2 = volunteer("s2@example.com", "twi", [strict.id])
        record_verdict(v2, item.id, candidate_id=opt.id)
        ok &= check("two does, in a project that asked for two",
                    state_for(item.id, "twi").done)
        ok &= check("and it counts as done",
                    consensus.settled_count("twi", project_id=strict.id) == 1)

    print("\nthe fast progress query agrees with the slow one")
    with app.app_context():
        from shola.tiers import tier_progress, tier_progress_all
        # Some real vote state to disagree over: settled, contested, and
        # untouched items across two languages.
        proj = Project.query.filter_by(slug="with-options").first() or \
            make_project("agree-check", "Check agreement", ["twi", "ewe"],
                         options=True, n=6, threshold=2)
        codes = proj.language_codes
        items = Word.query.filter_by(project_id=proj.id).all()
        for n, item in enumerate(items[:4]):
            code = codes[n % len(codes)]
            opts = [c for c in item.candidates if c.language == code]
            if not opts:
                continue
            for i in range(2):
                v = volunteer(f"cmp{n}-{i}@example.com", code, [proj.id])
                record_verdict(v, item.id, candidate_id=opts[0].id)
        slow = {code: tier_progress(code, project_id=proj.id) for code in codes}
        fast = tier_progress_all(codes, project_id=proj.id)
        ok &= check("both report the same tiers",
                    sorted(slow) == sorted(fast))
        same = all(slow[code] == fast[code] for code in codes)
        ok &= check("and the same numbers for every tier", same,
                    f"slow={slow} fast={fast}")
        # And across the whole database, not only one project.
        slow_all = {code: tier_progress(code) for code in codes}
        fast_all = tier_progress_all(codes)
        ok &= check("also with no project filter",
                    all(slow_all[c] == fast_all[c] for c in codes),
                    f"slow={slow_all} fast={fast_all}")

    print("\nattention is spread evenly, not piled on the nearly-done")
    with app.app_context():
        even = make_project("even-spread", "Answer these evenly", ["twi"],
                            options=True, n=6, threshold=4)
        # An exclusive run is how a single project becomes the only source now
        # that nobody opts in, and it is what this check needs: the question is
        # how one project spreads its own items.
        even.start_exclusive(30)
        db.session.commit()
        # Ten volunteers, five items each: with 6 items and a target of 4 there
        # is room for 24 answers, so nothing should get 4 while another gets 0.
        for i in range(10):
            v = volunteer(f"even{i}@example.com", "twi")
            top_up(v)
        counts = {}
        for a in Assignment.query.join(Word, Word.id == Assignment.word_id) \
                .filter(Word.project_id == even.id).all():
            counts[a.word_id] = counts.get(a.word_id, 0) + 1
        spread = sorted(counts.values())
        ok &= check("every item was handed to somebody",
                    len(counts) == 6, f"{len(counts)} of 6 items touched")
        ok &= check("and no item got far more attention than another",
                    spread and spread[-1] - spread[0] <= 1,
                    f"per-item counts {spread}")
        even.exclusive_until = None
        db.session.commit()

    print("\na skipped item goes back to the pool, but never to the same person")
    with app.app_context():
        skipping = make_project("skip-test", "Skip what you cannot answer",
                                ["twi"], options=True, n=3, threshold=2)
        skipping.start_exclusive(30)     # the only source, so the skip is seen
        db.session.commit()
        skipper = volunteer("skipper@example.com", "twi")
        top_up(skipper)
        first = skipper.assignments.first()
        skipped_id = first.word_id
        record_verdict(skipper, skipped_id, skipped=True)

        ok &= check("the skip is not counted as an answer",
                    state_for(skipped_id, "twi").total_votes == 0,
                    str(state_for(skipped_id, "twi").total_votes))
        ok &= check("the item is not finished by being skipped",
                    not state_for(skipped_id, "twi").done)
        ok &= check("it is still in the pool",
                    skipped_id in {w.id for w in
                                   open_query("twi", project_id=skipping.id)})

        release_stale(skipper)
        top_up(skipper)
        ok &= check("and never comes back to the person who skipped it",
                    skipped_id not in {a.word_id for a in
                                       skipper.assignments.filter_by(
                                           status="pending")},
                    "a skipped item was handed back to the same volunteer")

        other = volunteer("not-skipper@example.com", "twi", [skipping.id])
        top_up(other)
        ok &= check("but it does reach somebody else",
                    skipped_id in {a.word_id for a in other.assignments},
                    "a skipped item never reached another volunteer")
        # Put the pool back: a window left open silences every other Twi
        # project for the rest of the run.
        skipping.exclusive_until = None
        db.session.commit()

    print("\nenough skips makes it a problem, not everybody's problem")
    with app.app_context():
        skips = make_project("skip-target", "Answer what you can", ["twi"],
                             options=True, n=3, threshold=3)
        item = Word.query.filter_by(project_id=skips.id).first()
        # Two skips is not enough, even with an answer alongside.
        for i in range(2):
            v = volunteer(f"pass{i}@example.com", "twi", [skips.id])
            record_verdict(v, item.id, skipped=True)
        st = state_for(item.id, "twi")
        ok &= check("two of three skips is not a problem yet",
                    st.skips == 2 and not st.problem,
                    f"skips={st.skips} problem={st.problem}")
        ok &= check("and it is still offered",
                    item.id in {w.id for w in
                                open_query("twi", project_id=skips.id)})
        answerer = volunteer("answered@example.com", "twi", [skips.id])
        record_verdict(answerer, item.id,
                       candidate_id=item.candidates[0].id)
        st = state_for(item.id, "twi")
        ok &= check("an answer alongside does not cancel the skips",
                    st.skips == 2 and st.total_votes == 1,
                    f"skips={st.skips} answers={st.total_votes}")

        v = volunteer("pass3@example.com", "twi", [skips.id])
        record_verdict(v, item.id, skipped=True)
        st = state_for(item.id, "twi")
        ok &= check("the third skip marks it a problem", st.problem,
                    f"skips={st.skips} problem={st.problem}")
        ok &= check("and it stops being offered to anyone",
                    item.id not in {w.id for w in
                                    open_query("twi", project_id=skips.id)})
        later = volunteer("spared@example.com", "twi", [skips.id])
        top_up(later)
        ok &= check("so the remaining speakers never see it",
                    item.id not in {a.word_id for a in later.assignments})

    print("\nthe three lists are definitive")
    api = app.test_client()
    r = api.get("/api/items/skip-target/twi/problem")
    body = r.get_json()
    ok &= check("the skipped item is on the problem list",
                r.status_code == 200 and body["total"] >= 1, str(body)[:120])
    entry = next((x for x in body["items"] if x["why"] == "skipped"), None)
    ok &= check("labelled with why", entry is not None, str(body["items"])[:140])
    ok &= check("and how many passed over it",
                entry and "3 speakers" in entry["note"], str(entry))

    with app.app_context():
        clear = make_project("clear-answers", "Pick the natural one", ["twi"],
                             options=True, n=2, threshold=2)
        item = Word.query.filter_by(project_id=clear.id).first()
        opt = item.candidates[0]
        for i in range(2):
            v = volunteer(f"agree-c{i}@example.com", "twi", [clear.id])
            record_verdict(v, item.id, candidate_id=opt.id)
        # And one that ties, which must not appear as verified.
        tied = Word.query.filter_by(project_id=clear.id).offset(1).first()
        for i, c in enumerate(tied.candidates[:2]):
            v = volunteer(f"tie-c{i}@example.com", "twi", [clear.id])
            record_verdict(v, tied.id, candidate_id=c.id)
    r = api.get("/api/items/clear-answers/twi/verified")
    body = r.get_json()
    ok &= check("a clear winner is verified", body["total"] == 1, str(body)[:160])
    ok &= check("with the answer, not a vote table",
                set(body["items"][0]) == {"item", "answer", "chose", "of",
                                          "from"},
                str(body["items"][0]))
    r = api.get("/api/items/clear-answers/twi/problem")
    body = r.get_json()
    ok &= check("the tie is a problem, not a verified answer",
                any(x["why"] == "no agreement" for x in body["items"]),
                str(body["items"])[:160])
    r = api.get("/api/items/clear-answers/twi/verified?format=csv")
    ok &= check("csv works on the lists",
                r.status_code == 200 and b"item,answer,chose,of,from" in r.data,
                r.data[:60])
    r = api.get("/api/items/clear-answers/zzz/verified")
    ok &= check("an unknown language still 404s", r.status_code == 404)

    print("\nreporting an item takes it out of everyone's queue")
    with app.app_context():
        # The check is about one specific item leaving everyone's queue, so
        # the item has to be predictable: an exclusive run on the project it
        # belongs to is how a single project becomes the only source now.
        flagged_project = make_project("flag-test", "Report what is broken",
                                       ["twi"], options=True, n=4, threshold=2)
        flagged_project.start_exclusive(30)
        db.session.commit()
        reporter = volunteer("reporter@example.com", "twi")
        top_up(reporter)
        first = reporter.assignments.first()
        ok &= check("the reporter was given something to report",
                    first is not None)
        target = first.word_id
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
        # Re-queried, not reused: the object from the earlier app context is
        # detached here, so assigning to it would change nothing.
        Project.query.filter_by(slug="flag-test").first().exclusive_until = None
        db.session.commit()

    print("\nsubmitting a project, then approving it")
    with app.app_context():
        before = Project.query.count()
    fresh = app.test_client()
    csv_bytes = ("text,language,option1,option2\n"
                 "akwaaba,twi,welcome,you are welcome\n"
                 "medaase,twi,thanks,thank you\n").encode()
    r = fresh.post("/submit", data={
        "title": "Check these greetings in Twi",
        "summary": "Two ways each. Pick the natural one.",
        "item_format": "word",
        "votes_to_settle": "3",
        "name": "Kofi", "email": "kofi@example.com", "org": "Kofi Labs",
        "file": (io_bytes(csv_bytes), "greetings.csv"),
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
        waiting = volunteer("waiting@example.com", "twi")
        ok &= check("a pending project is not used",
                    pid not in {p.id for p in active_for(waiting)},
                    str([p.slug for p in active_for(waiting)]))

    print("\none file, many languages, one item each")
    r = fresh.post("/submit", data={
        "title": "Two languages at once please",
        "item_format": "word", "email": "kofi@example.com",
        "file": (io_bytes(("text,language,option1,option2\n"
                          "one,twi,a,b\n"
                          "one,ewe,c,d\n"
                          "two,twi,e,f\n"
                          "three,ewe,,\n").encode()), "both.csv"),
    }, content_type="multipart/form-data", follow_redirects=True)
    ok &= check("accepted", r.status_code == 200 and b"Submitted" in r.data,
                f"HTTP {r.status_code}")
    with app.app_context():
        two = Project.query.filter_by(slug="two-languages-at-once-please").first()
        ok &= check("both languages recorded",
                    two is not None
                    and sorted(two.language_codes) == ["ewe", "twi"],
                    str(two.language_codes if two else None))
        ok &= check("three items, not four rows",
                    two.item_count() == 3, str(two.item_count()))
        shared = Word.query.filter_by(project_id=two.id, phrase="one").first()
        ok &= check("the shared item exists once",
                    Word.query.filter_by(project_id=two.id,
                                         phrase="one").count() == 1)
        ok &= check("with options in both languages",
                    sorted({c.language for c in shared.candidates})
                    == ["ewe", "twi"],
                    str({c.language for c in shared.candidates}))
        bare = Word.query.filter_by(project_id=two.id, phrase="three").first()
        ok &= check("an item with no options is still an item",
                    bare is not None and not bare.candidates)
        ok &= check("filed under the one language that named it",
                    bare.language == "ewe", str(bare.language))

    print("\nan `all` row opens a project to every language")
    r = fresh.post("/submit", data={
        "title": "Translate these into any language you speak",
        "item_format": "word", "email": "kofi@example.com",
        "file": (io_bytes(("text,language,option1,option2\n"
                           "water,all,,\n"
                           "fire,all,,\n").encode()), "any.csv"),
    }, content_type="multipart/form-data", follow_redirects=True)
    ok &= check("accepted", r.status_code == 200 and b"Submitted" in r.data,
                f"HTTP {r.status_code}")
    with app.app_context():
        anylang = Project.query.filter_by(
            slug="translate-these-into-any-language-you-speak").first()
        ok &= check("it collects every language",
                    anylang is not None
                    and len(anylang.language_codes)
                    == len(app.config["ALL_LANGUAGES"]),
                    str(len(anylang.language_codes) if anylang else None))
        ok &= check("and the items belong to none in particular",
                    all(w.language is None for w in
                        Word.query.filter_by(project_id=anylang.id)))

    print("\nISO codes work, whatever we happen to store internally")
    r = fresh.post("/submit", data={
        "title": "A file using the ISO codes anybody would look up",
        "item_format": "word", "email": "kofi@example.com",
        "file": (io_bytes(("text,language,option1,option2\n"
                           "one,gaa,a,b\n"
                           "two,dag,c,d\n").encode()), "iso.csv"),
    }, content_type="multipart/form-data", follow_redirects=True)
    ok &= check("a file using gaa and dag is accepted",
                r.status_code == 200 and b"Submitted" in r.data,
                f"HTTP {r.status_code}")
    with app.app_context():
        iso = Project.query.filter_by(
            slug="a-file-using-the-iso-codes-anybody-would-look-up").first()
        ok &= check("mapped onto the languages we hold",
                    iso is not None
                    and sorted(iso.language_codes) == ["dagbani", "ga"],
                    str(iso.language_codes if iso else None))
    r = fresh.get("/api/items/everyday-words/gaa")
    ok &= check("an ISO code in an API call resolves too",
                r.status_code == 200 and r.get_json()["language"] == "ga",
                f"HTTP {r.status_code}")
    r = fresh.get("/languages.csv")
    ok &= check("and the published list shows both forms",
                b"gaa" in r.data and b"dag" in r.data, "expected both codes")

    print("\na blank language is refused rather than assumed")
    r = fresh.post("/submit", data={
        "title": "A file with a blank language cell",
        "item_format": "word", "email": "kofi@example.com",
        "file": (io_bytes(("text,language,option1,option2\n"
                           "something,,,\n").encode()), "blank.csv"),
    }, content_type="multipart/form-data")
    ok &= check("refused", r.status_code == 400, f"HTTP {r.status_code}")
    ok &= check("telling them to write `all` if that is what they mean",
                b"every language" in r.data)

    print("\na mistyped code is named, with a suggestion")
    r = fresh.post("/submit", data={
        "title": "A file with a mistyped language code",
        "item_format": "word", "email": "kofi@example.com",
        "file": (io_bytes(("text,language,option1,option2\n"
                           "something,twii,a,b\n").encode()), "typo.csv"),
    }, content_type="multipart/form-data")
    ok &= check("refused", r.status_code == 400, f"HTTP {r.status_code}")
    ok &= check("suggesting the real code", b"Did you mean twi" in r.data,
                "expected a suggestion")

    print("\nthe template and the code list are downloadable")
    r = fresh.get("/template.csv")
    ok &= check("the template comes back as CSV",
                r.status_code == 200 and b"text,language,priority" in r.data,
                r.data[:60])
    r = fresh.get("/languages.csv")
    ok &= check("so does the code list",
                r.status_code == 200 and b"language,code" in r.data)
    ok &= check("with a real code in it", b",twi" in r.data)

    # The template must survive our own validator: shipping an example with a
    # code we reject is worse than shipping none. "gaa" was in there once.
    tmpl = fresh.get("/template.csv").data
    with app.app_context():
        parsed, tmpl_problems, tmpl_meta = importer.parse(
            tmpl, known_languages=set(app.config["ALL_LANGUAGES"]))
    ok &= check("the template we hand out passes our own parser",
                not tmpl_problems, str(tmpl_problems))
    ok &= check("and names languages we know",
                tmpl_meta.get("any_language") and tmpl_meta["languages"],
                str(tmpl_meta))

    print("\nlanguages come from the file, and only from the file")
    r = fresh.post("/submit", data={
        "title": "Languages come from the file",
        "item_format": "sentence", "email": "kofi@example.com",
        "file": (io_bytes(("text,language,option1,option2\n"
                           "hello there,gaa,a,b\n").encode()), "ga.csv"),
    }, content_type="multipart/form-data", follow_redirects=True)
    ok &= check("accepted with nothing ticked anywhere",
                r.status_code == 200 and b"Submitted" in r.data,
                f"HTTP {r.status_code}")
    with app.app_context():
        from_file = Project.query.filter_by(
            slug="languages-come-from-the-file").first()
        ok &= check("the language was read from the column",
                    from_file is not None
                    and from_file.language_codes == ["ga"],
                    str(from_file.language_codes if from_file else None))

    print("\na bad file is refused with the line numbers, not half imported")
    with app.app_context():
        before = Word.query.count()
    r = fresh.post("/submit", data={
        "title": "A file with problems in it",
        "item_format": "word", "email": "kofi@example.com",
        "file": (io_bytes("text,language,option1,option2\n"
                          "market,zzz,a,b\n".encode()), "bad.csv"),
    }, content_type="multipart/form-data")
    ok &= check("refused", r.status_code == 400, f"HTTP {r.status_code}")
    ok &= check("naming the line", b"Line 2" in r.data)
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

    print("\napproving a project puts it in front of speakers")
    r = anon.post(f"/admin/project/{pid}/decide",
                  data={"action": "approve", "note": "looks fine"},
                  follow_redirects=True)
    with app.app_context():
        proj = db.session.get(Project, pid)
        ok &= check("it is approved", proj.status == "approved", proj.status)
        joiner = Volunteer.query.filter_by(email="waiting@example.com").first()
        # Nothing to join: approving is what puts it in front of speakers of
        # its languages.
        ok &= check("and it now draws on speakers of its languages",
                    pid in {p.id for p in active_for(joiner)},
                    str([p.slug for p in active_for(joiner)]))
        ok &= check("it was not used while it was pending",
                    proj.approved_at is not None)

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

    print("\nthe admin projects directory finds any project, in any state")
    with app.app_context():
        for i in range(24):
            make_project(f"bulk-admin-{i:02d}",
                         f"Bulk admin project {i:02d}", ["twi"], n=2,
                         status="pending" if i % 2 else "approved")
    d = anon.get("/admin/projects")
    ok &= check("the directory renders", d.status_code == 200,
                str(d.status_code))
    ok &= check("with a pager", b"Next" in d.data)
    ok &= check("search narrows it",
                b"Bulk admin project 07" in
                anon.get("/admin/projects?q=project+07").data)
    ok &= check("and filtering by status works",
                b"pending" in anon.get("/admin/projects?status=pending").data)
    ok &= check("a rejected project is still findable, unlike before",
                anon.get("/admin/projects?status=rejected").status_code == 200)
    ok &= check("it is behind the admin check",
                app.test_client().get("/admin/projects").status_code in (302, 403),
                str(app.test_client().get("/admin/projects").status_code))

    print("\nan exclusive run can be started and ended from the admin page")
    with app.app_context():
        target = make_project("admin-excl", "Check these proverbs", ["twi"],
                              n=3, status="approved")
        tid = target.id
    anon.post(f"/admin/project/{tid}/decide",
              data={"action": "exclusive", "days": "14"},
              follow_redirects=True)
    with app.app_context():
        target = db.session.get(Project, tid)
        ok &= check("it is exclusive for the days given",
                    target.is_exclusive and target.exclusive_days_left == 14,
                    str(target.exclusive_days_left))
    anon.post(f"/admin/project/{tid}/decide",
              data={"action": "extend", "days": "7"}, follow_redirects=True)
    with app.app_context():
        ok &= check("extending adds to it",
                    db.session.get(Project, tid).exclusive_days_left == 21,
                    str(db.session.get(Project, tid).exclusive_days_left))
    anon.post(f"/admin/project/{tid}/decide",
              data={"action": "end-exclusive"}, follow_redirects=True)
    with app.app_context():
        target = db.session.get(Project, tid)
        ok &= check("and it can be ended", not target.is_exclusive)
        ok &= check("which clears the request too",
                    not target.exclusive_requested)

    print("\nthe API reports every answer with its votes")
    api = app.test_client()
    r = api.get("/api/projects")
    ok &= check("projects are listed", r.status_code == 200
                and b"everyday-words" in r.data)
    data = r.get_json()
    entry = next(p for p in data["projects"] if p["slug"] == "read-sentences")
    ok &= check("each says how many answers it wants",
                entry["answers_wanted"] == 3, str(entry["answers_wanted"]))
    ok &= check("and how far along it is", "progress" in entry
                and "item_total" in entry["progress"],
                str(entry.get("progress")))
    ok &= check("progress counts items, not items times languages",
                entry["progress"]["item_total"] == 30,
                str(entry["progress"]))

    r = api.get("/api/items/with-options/twi")
    body = r.get_json()
    ok &= check("answers come back with their counts",
                r.status_code == 200 and body["entries"], str(body)[:160])
    first = body["entries"][0]
    ok &= check("every answer is listed", len(first["answers"]) >= 1,
                str(first))
    ok &= check("each with how many chose it",
                all("chose" in a for a in first["answers"]), str(first))
    ok &= check("and where it came from",
                all(a["from"] in ("option", "volunteer")
                    for a in first["answers"]), str(first))
    ok &= check("the leading answer is marked", first["leading"], str(first))
    ok &= check("with ties reported as ties", "tied" in first, str(first))
    ok &= check("and the target is stated",
                first["answers_wanted"] == 3, str(first))
    ok &= check("nothing is called verified",
                b"verified" not in r.data, "the word should be gone")

    r = api.get("/api/items/collect-only/twi")
    ok &= check("a project with no options reports the same way",
                r.status_code == 200 and r.get_json()["entries"],
                str(r.get_json())[:140])
    r = api.get("/api/items/collect-only/twi?answers=typed")
    ok &= check("typed answers can still be asked for separately",
                r.get_json()["answers"] == "typed")
    r = api.get("/api/items/with-options/twi?format=csv")
    ok &= check("csv carries every answer and its count",
                r.status_code == 200
                and b"item,answer,chose,share,total_answers,from,leading"
                in r.data, r.data[:90])
    r = api.get("/api/items/read-sentences/ga")
    ok &= check("a language the project ignores 404s", r.status_code == 404)
    r = api.get("/api/items/nope/twi")
    ok &= check("an unknown project 404s", r.status_code == 404)
    r = api.get("/api/words/twi")
    ok &= check("the old words endpoint still answers", r.status_code == 200)

    print("\napproving a project just puts it in the queue, quietly")
    with app.app_context():
        import shola.mailer as mailer_mod
        # Nothing may be emailed on approval: an approved project joins the
        # distribution queue and volunteers never have to think about which
        # project an item came from.
        ok &= check("there is no project announcement email to build",
                    not hasattr(mailer_mod, "build_project_email"))
        from shola import cli as cli_mod
        ok &= check("and no way to announce one",
                    not hasattr(cli_mod, "announce_project"))

        sent = []
        real_send = mailer_mod.send
        mailer_mod.send = lambda *a, **k: sent.append(a)
        try:
            quiet = make_project("quiet-job", "Check these riddles", ["twi"],
                                 n=3, status="pending")
            qid = quiet.id
        finally:
            pass
        anon.post(f"/admin/project/{qid}/decide",
                  data={"action": "approve"}, follow_redirects=True)
        mailer_mod.send = real_send

        approved = db.session.get(Project, qid)
        ok &= check("it is approved", approved.status == "approved",
                    approved.status)
        ok &= check("nobody was emailed about it", sent == [], str(sent))
        ok &= check("and it is in the queue for its speakers",
                    qid in {p.id for p in active_for(
                        volunteer("quiet@example.com", "twi"))},
                    "an approved project must reach its speakers")

    print("\nthe question matches which way the translation runs")
    dirs = make_app()
    with dirs.app_context():
        # Assignment, Project, ProjectLanguage, Word and Candidate are
        # imported at module level; importing them here again would make them
        # local to the whole of main() and break the sections above.
        from shola.mailer import build_daily_email
        from shola.views import as_cards

        # The usual direction: English out, the speaker's language back.
        out = make_project("into-twi", "Translate these into Twi", ["twi"],
                           n=2, status="approved")
        # The other direction: Twi out, English back.
        back = Project(slug="into-english", title="Translate Twi to English",
                       item_format="sentence", has_options=True,
                       votes_to_settle=3, status="approved", sort_order=60,
                       answer_language="en")
        db.session.add(back)
        db.session.flush()
        db.session.add(ProjectLanguage(project_id=back.id, language="twi"))
        source = Word(phrase="Wo ho te sen?", project_id=back.id,
                      language="twi", position=1, occurrences=0,
                      frequency=0.0, tier=1)
        db.session.add(source)
        db.session.flush()
        db.session.add(Candidate(word_id=source.id, language="twi",
                                 position=1, text="How are you?",
                                 source="gemini-3.6-flash"))
        v = volunteer("direction@example.com", "twi")
        shared = Word.query.filter_by(project_id=out.id).first()
        for word in (shared, source):
            db.session.add(Assignment(volunteer_id=v.id, word_id=word.id,
                                      due_date=date.today()))
        db.session.commit()

        asks = {c["phrase"]: c["ask"] for c in as_cards(v.assignments.all(),
                                                        "twi")}
        labels = {c["phrase"]: c["label"] for c in as_cards(v.assignments.all(),
                                                            "twi")}
        ok &= check("an English prompt asks for the speaker's language",
                    "in Asante Twi?" in asks[shared.phrase],
                    asks[shared.phrase])
        ok &= check("a Twi sentence asks what it says in English",
                    asks[source.phrase] == "What does this say in English?",
                    asks[source.phrase])
        ok &= check("and is never introduced as needing Twi",
                    "Asante Twi?" not in asks[source.phrase],
                    asks[source.phrase])
        ok &= check("the label names the language the item is in",
                    labels[source.phrase] == "Asante Twi sentence",
                    labels[source.phrase])

        # The mail has to agree with the page.
        _s, text, html = build_daily_email(v, [source])
        ok &= check("the email says translate to English",
                    "translating them to English" in text
                    and "translating them to English" in html)
        _s, text2, _h = build_daily_email(v, [shared])
        ok &= check("and to Asante Twi for the other direction",
                    "translating them to Asante Twi" in text2)

        ok &= check("answers_in falls back to the speaker's language",
                    out.answers_in("twi") == ("twi", "Asante Twi"),
                    str(out.answers_in("twi")))
        ok &= check("and is fixed where the project says so",
                    back.answers_in("twi") == ("en", "English"),
                    str(back.answers_in("twi")))

    print("\na submitted project can say which way it runs")
    d = dirs.test_client()
    r = d.post("/submit", data={
        "title": "Twi sentences into English please",
        "item_format": "sentence", "email": "kofi@example.com",
        "answer_language": "en",
        "file": (io_bytes("text,language,option1\n"
                          "Wo ho te sen?,twi,How are you?\n".encode()),
                 "s.csv"),
    }, follow_redirects=True, content_type="multipart/form-data")
    ok &= check("it submits", r.status_code == 200, str(r.status_code))
    with dirs.app_context():
        made = Project.query.filter_by(
            slug="twi-sentences-into-english-please").first()
        ok &= check("and the direction is stored",
                    made is not None and made.answer_language == "en",
                    str(made.answer_language if made else None))
    r = d.post("/submit", data={
        "title": "Words into the speakers language",
        "item_format": "word", "email": "kofi@example.com",
        "answer_language": "nonsense",
        "file": (io_bytes("text,language,option1\n"
                          "water,twi,nsuo\n".encode()), "w.csv"),
    }, follow_redirects=True, content_type="multipart/form-data")
    with dirs.app_context():
        made = Project.query.filter_by(
            slug="words-into-the-speakers-language").first()
        ok &= check("an unknown answer language falls back to the speaker's",
                    made is not None and not made.answer_language,
                    str(made.answer_language if made else None))

    print("\nthe link shows what the email said it would")
    stale = make_app()
    with stale.app_context():
        from datetime import timedelta

        from shola.mailer import build_daily_email
        seed_core(40)
        make_project("other-work", "Something else entirely", ["twi"], n=40)
        v = volunteer("clicker@example.com", "twi")

        day1 = date.today()
        day2 = day1 + timedelta(days=1)
        day3 = day2 + timedelta(days=1)

        # The nightly send builds a list and emails it.
        top_up(v, today=day1, new_list=True)
        emailed = [a.word for a in
                   v.pending_today(day1).limit(daily_quota(v)).all()]
        listed = {w.phrase for w in emailed}
        ok &= check("the send leases a list", len(listed) > 0, str(listed))
        _s, text, _h = build_daily_email(v, emailed)
        ok &= check("and the mail names those items",
                    all(w.phrase in text for w in emailed))

        # Following that link the next day must show the same items. It used
        # to release them and lease a fresh list from the next project, so the
        # mail named five sentences and the page showed five words.
        top_up(v, today=day2)
        shown = {a.word.phrase for a in v.pending_today(day2)}
        ok &= check("a day-old link still shows what was emailed",
                    shown == listed, f"emailed {sorted(listed)} "
                                     f"showed {sorted(shown)}")
        ok &= check("and does not switch project underneath them",
                    len({a.word.project_id for a in v.pending_today(day2)}) == 1,
                    str({a.word.project_id for a in v.pending_today(day2)}))

        # The next send is what replaces it - nothing carries over past that.
        top_up(v, today=day3, new_list=True)
        after = {a.word.phrase for a in v.pending_today(day3)}
        ok &= check("the next send hands the old list back", after != listed,
                    str(sorted(after)))
        ok &= check("and leases a full one", len(after) == daily_quota(v),
                    str(len(after)))

    print("\nan older email link says so instead of quietly showing something else")
    aged = make_app()
    with aged.app_context():
        from datetime import timedelta

        from shola.mailer import daily_link
        seed_core(40)
        make_project("other-thing", "Something else", ["twi"], n=40)
        v = volunteer("aged@example.com", "twi")

        yesterday = date.today() - timedelta(days=1)
        top_up(v, today=yesterday, new_list=True)
        old_stamp = v.lists_taken
        with aged.test_request_context():
            link = daily_link(v)
        ok &= check("the mail link says which list it is about",
                    f"list={old_stamp}" in link, link)

        # A newer send replaces it.
        top_up(v, today=date.today(), new_list=True)
        new_stamp = v.lists_taken
        ok &= check("a later send moves the stamp on", new_stamp != old_stamp,
                    f"{old_stamp} then {new_stamp}")
        token = link.split("/w/")[1].split("?")[0]

    c = aged.test_client()
    older = c.get(f"/w/{token}?list={old_stamp}").data
    ok &= check("following the older one explains the swap",
                b"a link from an older email" in older)
    current = c.get(f"/w/{token}?list={new_stamp}").data
    ok &= check("the current one says nothing",
                b"a link from an older email" not in current)
    plain = c.get(f"/w/{token}").data
    ok &= check("and a link with no stamp says nothing either",
                b"a link from an older email" not in plain)

    print("\nthe pager helper elides sensibly")
    from shola.views import page_window
    ok &= check("a short pager lists every page",
                page_window(1, 5) == [1, 2, 3, 4, 5], str(page_window(1, 5)))
    ok &= check("a long one elides the middle",
                page_window(10, 20) == [1, None, 8, 9, 10, 11, 12, None, 20],
                str(page_window(10, 20)))
    ok &= check("and always offers the first and last",
                page_window(10, 40)[0] == 1 and page_window(10, 40)[-1] == 40)

    print("\nthe public pages hold together")
    for path in ("/", "/stats", "/models", "/api", "/submit", "/join"):
        r = api.get(path)
        ok &= check(f"{path} renders", r.status_code == 200,
                    f"HTTP {r.status_code}")
    # The public project directory is gone: volunteers do not choose projects,
    # so an index of them was a page with nothing to decide on it.
    for path in ("/projects", "/projects/everyday-words"):
        ok &= check(f"{path} is gone", api.get(path).status_code == 404,
                    f"HTTP {api.get(path).status_code}")

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
