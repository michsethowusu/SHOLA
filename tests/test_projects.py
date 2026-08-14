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
from shola.projects import (active_for, joined, opt_in, opt_out,  # noqa: E402
                            shares)
from shola.consensus import tally                               # noqa: E402
from shola.tiers import (answers_target, daily_quota, open_query,  # noqa: E402
                         refresh_word, release_stale, state_for, top_up)

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
        # Answer all three by tapping an option: typing would add options
        # without closing anything, so the project would never run out.
        for a in list(guest.assignments):
            opt = [c for c in a.word.candidates if c.language == "twi"][0]
            record_verdict(guest, a.word_id, candidate_id=opt.id)
        ok &= check("once it runs out the rest open up",
                    len(active_for(guest)) == 2,
                    str([p.slug for p in active_for(guest)]))
        n = top_up(guest)
        ok &= check("and the next list is drawn from them", n > 0, f"{n}")

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
        # Ten volunteers, five items each: with 6 items and a target of 4 there
        # is room for 24 answers, so nothing should get 4 while another gets 0.
        for i in range(10):
            v = volunteer(f"even{i}@example.com", "twi", [even.id])
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

    print("\na skipped item goes back to the pool, but never to the same person")
    with app.app_context():
        skipping = make_project("skip-test", "Skip what you cannot answer",
                                ["twi"], options=True, n=3, threshold=2)
        skipper = volunteer("skipper@example.com", "twi", [skipping.id])
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
        waiting = volunteer("waiting@example.com", "twi", [pid])
        ok &= check("nobody can opt in before approval",
                    len(joined(waiting)) == 0,
                    str([p.slug for _v, p in joined(waiting)]))

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

    print("\nthe projects index searches, filters and pages")
    with app.app_context():
        # Enough to need more than one page.
        for i in range(16):
            make_project(f"bulk-{i}", f"Bulk project {i:02d} about markets",
                         ["twi"] if i % 2 else ["ewe"], options=True, n=2,
                         fmt="sentence" if i % 3 else "word", threshold=2)
        from shola.models import Project as P
        live = P.query.filter(P.status == "approved").count()

    idx = app.test_client()
    r = idx.get("/projects")
    ok &= check("the index renders", r.status_code == 200)
    body = r.data.decode()
    rows = body.count('class="project-row"')
    ok &= check("it pages rather than listing everything", rows <= 12,
                f"{rows} rows on one page")
    ok &= check("and says how many there are and where you are",
                "page 1 of" in body, "expected a page indicator")
    ok &= check("with pager links", 'class="pager"' in body)

    r2 = idx.get("/projects?page=2")
    b2 = r2.data.decode()
    ok &= check("page 2 shows different projects",
                b2.count('class="project-row"') > 0
                and b2 != body, "page 2 looked identical")

    r = idx.get("/projects?q=Bulk+project+03")
    found = r.data.decode().count('class="project-row"')
    ok &= check("search narrows it", found == 1, f"{found} matches")
    r = idx.get("/projects?q=nothing-like-this-exists")
    ok &= check("and says so when nothing matches",
                b"Nothing matches that" in r.data)

    r = idx.get("/projects?language=ewe")
    ok &= check("filtering by language works",
                r.status_code == 200
                and b"Bulk project 00" in r.data, "expected an Ewe project")
    ok &= check("and excludes the others",
                b"Bulk project 01" not in r.data, "a Twi project leaked in")

    r = idx.get("/projects?kind=word")
    ok &= check("filtering by kind of item works", r.status_code == 200)
    r = idx.get("/projects?sort=name")
    ok &= check("sorting by name works", r.status_code == 200)
    r = idx.get("/projects?sort=newest")
    ok &= check("sorting by newest works", r.status_code == 200)

    r = idx.get("/projects?page=999")
    ok &= check("a page past the end lands on the last one, not an error",
                r.status_code == 200 and b"project-row" in r.data)

    from shola.views import page_window
    ok &= check("a short pager lists every page",
                page_window(1, 5) == [1, 2, 3, 4, 5], str(page_window(1, 5)))
    ok &= check("a long one elides the middle",
                page_window(10, 20) == [1, None, 8, 9, 10, 11, 12, None, 20],
                str(page_window(10, 20)))
    ok &= check("and always offers the first and last",
                page_window(10, 40)[0] == 1 and page_window(10, 40)[-1] == 40)

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
