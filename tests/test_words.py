"""End-to-end checks for the work itself: answering, skipping, reporting.

Kept apart from test_flow.py because the fixture is different: this one seeds a
corpus and drives it through consensus, skips and the published lists, where
the other file works the sign-up and email path.

Everything here runs against the one project SHOLA has. There used to be a
platform for adding more, and most of this file tested it; what survived is
what was always the point - a word goes out, speakers answer it, and the
answers come back out through the API.
"""

import os
import sys
import tempfile
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shola import create_app                                    # noqa: E402
from shola import consensus                                     # noqa: E402
from shola.assignment import record_verdict                     # noqa: E402
from shola.config import Config, find_languages                 # noqa: E402
from shola.consensus import tally                               # noqa: E402
from shola.models import (CORE_PROJECT, Candidate, Evaluation,   # noqa: E402
                          Flag, Project, Volunteer, Word, db)
from shola.projects import active_for                           # noqa: E402
from shola.tiers import (answers_target, open_query, refresh_word,  # noqa: E402
                         state_for, top_up)

PASSED = []
SLUG = CORE_PROJECT["slug"]


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
    return Project.query.filter_by(slug=SLUG).first()


def seed(n, languages=("twi",), options=3, start=0, tier=1):
    """Words in the one project, shaped as the real import creates them.

    `options` is how many machine translations each language arrives with.
    Zero is the ordinary case for most of Africa: nothing seeded, and the first
    speaker to answer writes the wording everyone after them votes on.
    """
    project = core()
    made = []
    for i in range(start, start + n):
        word = Word(phrase=f"word {i}", frequency=float(n - i),
                    occurrences=n - i, tier=tier, project_id=project.id)
        db.session.add(word)
        db.session.flush()
        for code in languages:
            for pos in range(1, options + 1):
                db.session.add(Candidate(word_id=word.id, language=code,
                                         position=pos,
                                         text=f"{code}-{i}-{pos}",
                                         source="gemini-3.6-flash"))
        made.append(word)
    db.session.commit()
    return made


def volunteer(email, language="twi"):
    v = Volunteer(name="Test Person", email=email, language=language)
    db.session.add(v)
    db.session.commit()
    return v


def token_for(app, v):
    from shola.mailer import make_token
    with app.test_request_context():
        return make_token(v)


def main():
    app = make_app()
    ok = True

    print("\nthe one project is the words, and it reaches every language")
    with app.app_context():
        project = core()
        ok &= check("it exists and is live", project is not None
                    and project.approved)
        ok &= check("and collects every language we know",
                    project.languages.count()
                    == len(app.config["ALL_LANGUAGES"]),
                    str(project.languages.count()))
        ok &= check("which is now the African list, not the Ghanaian one",
                    len(app.config["ALL_LANGUAGES"]) > 2000,
                    str(len(app.config["ALL_LANGUAGES"])))
        speaker = volunteer("first@example.com", "yor")
        ok &= check("a Yoruba speaker is drawn on",
                    [p.slug for p in active_for(speaker)] == [SLUG],
                    str([p.slug for p in active_for(speaker)]))

    print("\na language with nothing seeded collects answers all the same")
    with app.app_context():
        # No options anywhere: this is what almost every African language
        # looks like on day one.
        word = seed(1, languages=(), options=0)[0]
        for i in range(6):
            v = volunteer(f"typer{i}@example.com", "hau")
            record_verdict(v, word.id, custom_text="ruwa")
        ok &= check("the answers are counted",
                    tally(word.id, "hau")["ranked"][0]["votes"] == 6,
                    str(tally(word.id, "hau")["ranked"]))
        ok &= check("the leading answer is reported",
                    consensus.best(word.id, "hau") is not None)
        ok &= check("the word closed once it hit its target",
                    state_for(word.id, "hau").done)
        ok &= check("every answer is exported",
                    len(list(consensus.typed_rows("hau",
                                                  project_id=core().id))) == 6)
        ok &= check("and the wording is now an option for the next speaker",
                    any(c.source == "volunteer" for c in word.candidates))

    print("\nthe target is what finishes a word, and progress follows it")
    with app.app_context():
        word = seed(1, languages=("twi",), options=2, start=100)[0]
        target = answers_target(core())
        ok &= check("the target is the site setting", target == 5, str(target))
        opt = [c for c in word.candidates if c.language == "twi"][0]
        for i in range(target):
            v = volunteer(f"target{i}@example.com", "twi")
            record_verdict(v, word.id, candidate_id=opt.id)
        refresh_word(word.id, "twi")
        ok &= check("it is done at the target",
                    state_for(word.id, "twi").done)
        ok &= check("and out of the queue",
                    word.id not in {w.id for w in
                                    open_query("twi", core().id).all()})

    print("\na skipped word goes back to the pool, never to the same person")
    with app.app_context():
        word = seed(1, languages=("ewe",), options=2, start=200)[0]
        skipper = volunteer("skipper@example.com", "ewe")
        record_verdict(skipper, word.id, skipped=True)
        ok &= check("a skip is not an answer",
                    state_for(word.id, "ewe").total_votes == 0)
        ok &= check("and does not finish it",
                    not state_for(word.id, "ewe").done)
        ok &= check("it stays in the pool for others",
                    word.id in {w.id for w in open_query("ewe", core().id)})

    print("\nenough skips makes it a problem, not everybody's problem")
    with app.app_context():
        word = seed(1, languages=("ga",), options=2, start=300)[0]
        for i in range(answers_target(core())):
            v = volunteer(f"skip{i}@example.com", "ga")
            record_verdict(v, word.id, skipped=True)
        refresh_word(word.id, "ga")
        st = state_for(word.id, "ga")
        ok &= check("it is marked a problem", st.problem, str(st.problem))
        ok &= check("and stops going out",
                    word.id not in {w.id for w in
                                    open_query("ga", core().id)})
        rows = list(consensus.problem_rows("ga", project_id=core().id))
        ok &= check("it is published as one, with a reason",
                    any(r["item"] == word.phrase and r["why"] for r in rows),
                    str(rows[:2]))

    print("\nreporting a word takes it out of everyone's queue")
    with app.app_context():
        word = seed(1, languages=("twi",), options=2, start=400)[0]
        reporter = volunteer("reporter@example.com", "twi")
        wid, tok = word.id, token_for(app, reporter)
    app.test_client().post(f"/w/{tok}/{wid}/flag",
                           data={"reason": "nonsense", "note": "not a word"})
    with app.app_context():
        ok &= check("the report is recorded",
                    Flag.query.filter_by(word_id=wid).count() == 1)
        ok &= check("and it leaves the queue for everyone",
                    wid not in {w.id for w in open_query("twi", core().id)})
        ok &= check("with no verdict recorded against it",
                    Evaluation.query.filter_by(word_id=wid).count() == 0)

    print("\nthe three lists are definitive")
    with app.app_context():
        agreed = seed(1, languages=("twi",), options=2, start=500)[0]
        opt = [c for c in agreed.candidates if c.language == "twi"][0]
        for i in range(answers_target(core())):
            v = volunteer(f"agree{i}@example.com", "twi")
            record_verdict(v, agreed.id, candidate_id=opt.id)
        refresh_word(agreed.id, "twi")
    api = app.test_client()
    r = api.get(f"/api/items/{SLUG}/twi/verified")
    ok &= check("verified answers", r.status_code == 200
                and r.get_json()["total"] >= 1, str(r.status_code))
    ok &= check("as an answer, not a vote table",
                "answer" in (r.get_json()["items"] or [{}])[0],
                str(r.get_json()["items"][:1]))
    r = api.get(f"/api/items/{SLUG}/ga/problem")
    ok &= check("problems are their own list", r.status_code == 200
                and r.get_json()["total"] >= 1)
    r = api.get(f"/api/items/{SLUG}/twi")
    ok &= check("and everything, with counts", r.status_code == 200
                and r.get_json()["entries"])
    r = api.get(f"/api/items/{SLUG}/twi/verified?format=csv")
    ok &= check("csv works on the lists", r.status_code == 200
                and b"item,answer,chose,of,from" in r.data, r.data[:60])
    ok &= check("an unknown language 404s",
                api.get(f"/api/items/{SLUG}/zzz").status_code == 404)
    ok &= check("the old words endpoint still answers",
                api.get("/api/words/twi").status_code == 200)

    print("\nthe admin side needs an allowlisted address")
    anon = app.test_client()
    ok &= check("the dashboard is closed to strangers",
                anon.get("/admin/dashboard").status_code in (302, 403),
                str(anon.get("/admin/dashboard").status_code))

    print("\nthe link shows what the email said it would")
    stale = make_app()
    with stale.app_context():
        from shola.mailer import build_daily_email, daily_link
        seed(40)
        v = volunteer("clicker@example.com", "twi")
        yesterday = date.today() - timedelta(days=1)
        top_up(v, today=yesterday, new_list=True)
        emailed = [a.word for a in v.pending_today(yesterday).all()]
        listed = {w.phrase for w in emailed}
        ok &= check("the send leases a list", bool(listed))
        _s, text, _h = build_daily_email(v, emailed)
        ok &= check("the mail names those words",
                    all(w.phrase in text for w in emailed))
        old_stamp = v.lists_taken
        with stale.test_request_context():
            link = daily_link(v)
        ok &= check("and the link says which list it is about",
                    f"list={old_stamp}" in link, link)

        top_up(v, today=date.today())
        shown = {a.word.phrase for a in v.pending_today()}
        ok &= check("a day-old link still shows what was emailed",
                    shown == listed,
                    f"emailed {len(listed)}, showed {len(shown)}")

        # The next send, dated today so that visiting the site sees it as the
        # current list rather than leasing another one.
        top_up(v, today=date.today(), new_list=True)
        ok &= check("and the next send replaces it",
                    {a.word.phrase for a in v.pending_today()} != listed)
        new_stamp = v.lists_taken
        ok &= check("which moves the stamp on", new_stamp != old_stamp,
                    f"{old_stamp} then {new_stamp}")
        token = link.split("/w/")[1].split("?")[0]

    print("\nan older email link says so rather than quietly swapping")
    c = stale.test_client()
    ok &= check("following the older one explains it",
                b"a link from an older email"
                in c.get(f"/w/{token}?list={old_stamp}").data)
    ok &= check("the current one says nothing",
                b"a link from an older email"
                not in c.get(f"/w/{token}?list={new_stamp}").data)
    ok &= check("and a link with no stamp says nothing either",
                b"a link from an older email" not in c.get(f"/w/{token}").data)

    print("\nfinding your language among two thousand")
    with app.app_context():
        ok &= check("by name", find_languages("yoruba")[0][0] == "yor",
                    str(find_languages("yoruba")[:1]))
        ok &= check("by a name it also goes by",
                    find_languages("bamanankan")[0][0] == "bam",
                    str(find_languages("bamanankan")[:1]))
        ok &= check("by country",
                    any("GH" in info.get("countries", ())
                        for _code, info in find_languages("Ghana")),
                    str([i["name"] for _c, i in find_languages("Ghana")[:3]]))
        ok &= check("nonsense finds nothing", find_languages("zzzznope") == [])
        ok &= check("and an empty search does not return everything",
                    find_languages("") == [])
    r = app.test_client().get("/languages.json?q=swahili")
    ok &= check("the sign-up box can search", r.status_code == 200
                and any(x["code"] == "swa" for x in r.get_json()),
                str(r.get_json())[:120])

    print("\nthe public pages hold together")
    for path in ("/", "/stats", "/models", "/api", "/join", "/champions",
                 "/languages.csv", "/healthz"):
        r = app.test_client().get(path)
        ok &= check(f"{path} renders", r.status_code == 200,
                    f"HTTP {r.status_code}")
    # The platform is gone: these must not come back by accident.
    for path in ("/submit", "/template.csv", "/projects", "/admin/projects"):
        code = app.test_client().get(path).status_code
        ok &= check(f"{path} is gone", code in (404, 302, 403), f"HTTP {code}")

    print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
    print(f"{sum(PASSED)}/{len(PASSED)} checks passed")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
