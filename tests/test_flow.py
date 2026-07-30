"""End-to-end checks for the parts of the brief that are easy to get wrong:
fair assignment, day scheduling, missed-day carry-forward, mutually exclusive
choices, and consensus from votes.

Run with:  python3 -m pytest tests -q      (or: python3 tests/test_flow.py)
"""

import os
import sys
import tempfile
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shola import create_app                                    # noqa: E402
from shola.assignment import (assign_words, leaderboard,         # noqa: E402
                              record_verdict, redistribute, spread)
from shola.config import Config                                 # noqa: E402
from shola.consensus import best, normalise, tally               # noqa: E402
from shola.models import (Assignment, Candidate, Evaluation,      # noqa: E402
                          Volunteer, Word, db)

LANGS = ["twi", "ewe", "ga", "dagbani"]


def make_app():
    tmp = tempfile.mkdtemp()

    class T(Config):
        TESTING = True
        SECRET_KEY = "test"
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{tmp}/t.db"
        WORDS_PER_VOLUNTEER = 20
        SMTP_USER = "x@example.com"
        SMTP_PASSWORD = "y"

    return create_app(T)


def seed(n_words=30):
    for i in range(n_words):
        w = Word(phrase=f"word {i}")
        db.session.add(w)
        db.session.flush()
        for lang in LANGS:
            for pos in (1, 2, 3):
                db.session.add(Candidate(word_id=w.id, language=lang,
                                         position=pos,
                                         text=f"{lang}-{i}-v{pos}"))
    db.session.commit()


def add_volunteer(email, language="twi", days=""):
    v = Volunteer(name=f"Test {email[0].upper()}", email=email,
                  language=language, available_days=days)
    db.session.add(v)
    db.session.commit()
    return v


def check(label, condition, detail=""):
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail and not condition else ""))
    return condition


def main():
    app = make_app()
    ok = True
    with app.app_context():
        seed(30)

        print("\nspread() splits a day's load evenly, remainder early")
        ok &= check("20 over 3 days", spread(20, 3) == [7, 7, 6], str(spread(20, 3)))
        ok &= check("no days", spread(5, 0) == [])

        print("\ncoverage before duplication")
        a = add_volunteer("a@example.com")
        assign_words(a, 20)
        b = add_volunteer("b@example.com")
        assign_words(b, 20)
        counts = sorted(w.assign_count for w in Word.query.all())
        # 30 words, 40 assignments: 10 words twice, 20 once. No word 3x while
        # another is still at 0.
        ok &= check("every word assigned at least once", counts[0] >= 1,
                    f"min={counts[0]}")
        ok &= check("no word assigned 3x before all have 2", counts[-1] <= 2,
                    f"max={counts[-1]}")
        ok &= check("two volunteers never share all words",
                    len({x.word_id for x in a.assignments}
                        & {x.word_id for x in b.assignments}) == 10)

        print("\nassignments land only on chosen weekdays")
        c = add_volunteer("c@example.com", days="0,3")   # Monday, Thursday
        assign_words(c, 20)
        weekdays = {x.due_date.weekday() for x in c.assignments}
        ok &= check("only Mon/Thu used", weekdays <= {0, 3}, str(weekdays))

        print("\nmissed days carry forward, then redistribute")
        past = date.today() - timedelta(days=5)
        for item in a.assignments.limit(6):
            item.due_date = past
        db.session.commit()
        ok &= check("overdue work shows in today's queue",
                    a.pending_today().count() >= 6)
        moved = redistribute(a)
        ok &= check("redistribute moves overdue forward", moved == 6, f"moved={moved}")
        ok &= check("nothing left in the past",
                    all(x.due_date >= date.today()
                        for x in a.assignments.filter_by(status="pending")))
        ok &= check("no assignment was destroyed", a.assignments.count() == 20)

        print("\none verdict per word, and it closes the assignment")
        first = a.assignments.first()
        cand = [x for x in first.word.candidates if x.language == "twi"][0]
        record_verdict(a, first.word_id, candidate_id=cand.id)
        record_verdict(a, first.word_id, candidate_id=cand.id)   # repeat ignored
        ok &= check("verdict stored once",
                    Evaluation.query.filter_by(volunteer_id=a.id,
                                               word_id=first.word_id).count() == 1)
        ok &= check("assignment closed",
                    db.session.get(Assignment, first.id).status == "done")

        print("\nconsensus counts a typed answer equal to a machine option")
        target = Word.query.filter(Word.phrase == "word 7").first()
        v1 = add_volunteer("d@example.com")
        v2 = add_volunteer("e@example.com")
        v3 = add_volunteer("f@example.com")
        record_verdict(v1, target.id, custom_text="mepɛ nsuo")
        record_verdict(v2, target.id, custom_text="Mepɛ Nsuo")     # case differs
        opt = [x for x in target.candidates if x.language == "twi"][0]
        record_verdict(v3, target.id, candidate_id=opt.id)
        t = tally(target.id, "twi")
        ok &= check("case-folded votes merge", t["ranked"][0]["votes"] == 2,
                    str(t["ranked"]))
        agreed = best(target.id, "twi")
        ok &= check("typed wording can beat the machine option",
                    agreed["text"].lower() == "mepɛ nsuo", str(agreed))
        ok &= check("NFC folding", normalise("nsuo") == "nsuo")

        print("\na single vote is not consensus")
        lone = Word.query.filter(Word.phrase == "word 9").first()
        record_verdict(v1, lone.id, custom_text="only me")
        ok &= check("one vote yields no answer", best(lone.id, "twi") is None)

        print("\nleaderboard ranks by verdicts")
        board = leaderboard()
        ok &= check("most active first", board[0][1] >= board[-1][1])

    print("\nHTTP flow")
    with app.app_context():
        db.create_all()
    client = app.test_client()
    with app.app_context():
        seed_word = Word.query.first()
    r = client.post("/join", data={
        "name": "Ama Serwaa", "email": "ama@example.com", "language": "twi",
        "days": ["0", "1"], "time_window": "morning"}, follow_redirects=True)
    ok &= check("join returns the welcome page", r.status_code == 200
                and b"Akwaaba" in r.data)
    r = client.get("/evaluate")
    ok &= check("evaluate page renders", r.status_code == 200)
    ok &= check("evaluate offers the typed-answer option",
                b"Type your own translation" in r.data)

    with app.app_context():
        vol = Volunteer.query.filter_by(email="ama@example.com").first()
        item = vol.pending_today().first()
        if item is None:                      # all words due later this week
            item = vol.assignments.first()
            item.due_date = date.today()
            db.session.commit()
        wid = item.word_id
        cid = [c for c in item.word.candidates if c.language == "twi"][0].id

    r = client.post(f"/evaluate/{wid}", data={"choice": str(cid)},
                    headers={"X-Requested-With": "shola"})
    ok &= check("verdict posts as JSON", r.status_code == 200
                and r.get_json().get("ok") is True)

    r = client.post(f"/evaluate/{wid}", data={"choice": "custom",
                                              "custom_text": ""},
                    headers={"X-Requested-With": "shola"})
    ok &= check("empty typed answer is rejected", r.status_code == 400)

    r = client.get("/api/consensus/twi")
    ok &= check("consensus API responds", r.status_code == 200)
    r = client.get("/api/consensus/nope")
    ok &= check("unknown language 404s", r.status_code == 404)

    print("\nemail rendering")
    with app.app_context():
        from shola.mailer import build_daily_email, daily_link, read_token
        vol = Volunteer.query.filter_by(email="ama@example.com").first()
        words = [a.word for a in vol.assignments.limit(3)]
        subject, text, html = build_daily_email(vol, words, overdue_count=1)
        ok &= check("subject names the volunteer", "Ama" in subject, subject)
        ok &= check("text lists the words", words[0].phrase in text)
        ok &= check("html lists the words", words[0].phrase in html)
        ok &= check("html carries the daily link", "/start/" in html)
        link = daily_link(vol)
        token = link.rsplit("/", 1)[-1]
        ok &= check("token round-trips to the volunteer", read_token(token) == vol.id)
        ok &= check("tampered token is refused", read_token(token + "x") is None)

    print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
