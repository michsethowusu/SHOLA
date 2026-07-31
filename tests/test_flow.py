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
                          PendingSignup, Volunteer, Word, db)

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
        # Descending frequency, so word 0 is the commonest.
        w = Word(phrase=f"word {i}", frequency=float(n_words - i))
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

    print("\ncommon words go out first")
    with app.app_context():
        g = add_volunteer("g@example.com")
        assign_words(g, 5)
        picked = [a.word.phrase for a in g.assignments.order_by(Assignment.id)]
        freqs = [db.session.get(Word, a.word_id).frequency
                 for a in g.assignments.order_by(Assignment.id)]
        ok &= check("frequency descending within a coverage level",
                    freqs == sorted(freqs, reverse=True), str(picked))

    print("\nsignup needs a code before anything is created")
    sent = {}

    def fake_send(to, subject, text, html):
        sent["to"] = to
        sent["code"] = "".join(ch for ch in subject if ch.isdigit())

    import shola.mailer as mailer_mod
    import shola.views as views_mod
    mailer_mod.send = fake_send
    views_mod.build_otp_email = mailer_mod.build_otp_email

    client = app.test_client()
    r = client.post("/join", data={
        "name": "Ama Serwaa", "email": "ama@example.com", "language": "twi",
        "days": ["0", "1"], "time_window": "morning"}, follow_redirects=True)
    ok &= check("join asks for the code", r.status_code == 200
                and b"Enter the code" in r.data)
    with app.app_context():
        ok &= check("no volunteer exists yet",
                    Volunteer.query.filter_by(email="ama@example.com").first() is None)
        ok &= check("signup is held pending",
                    PendingSignup.query.filter_by(email="ama@example.com").first()
                    is not None)
    ok &= check("code was emailed", sent.get("to") == "ama@example.com")

    r = client.post("/verify", data={"email": "ama@example.com",
                                     "code": "000000"})
    wrong_ok = r.status_code == 400
    with app.app_context():
        wrong_ok = wrong_ok and Volunteer.query.filter_by(
            email="ama@example.com").first() is None
    ok &= check("a wrong code creates nothing", wrong_ok)

    r = client.post("/verify", data={"email": "ama@example.com",
                                     "code": sent["code"]})
    ok &= check("the right code completes signup", r.status_code == 200
                and b"Akwaaba" in r.data)
    with app.app_context():
        vol = Volunteer.query.filter_by(email="ama@example.com").first()
        ok &= check("volunteer now exists", vol is not None)
        ok &= check("pending record cleared",
                    PendingSignup.query.filter_by(email="ama@example.com").first()
                    is None)
        ok &= check("words assigned on confirmation", vol.assignments.count() > 0)
        with app.test_request_context():
            from shola.mailer import make_token
            token = make_token(vol)
        item = vol.pending_today().first() or vol.assignments.first()
        wid = item.word_id
        cid = [c for c in item.word.candidates if c.language == "twi"][0].id

    print("\nthe link alone is enough - no session, no cookies")
    fresh = app.test_client()          # never visited, holds no cookie
    r = fresh.get(f"/w/{token}")
    ok &= check("personalised link opens the flow", r.status_code == 200
                and b"Type your own translation" in r.data)

    nocookie = app.test_client()
    r = nocookie.post(f"/w/{token}/{wid}", data={"choice": str(cid)},
                      headers={"X-Requested-With": "shola"})
    ok &= check("verdict posts with no session at all", r.status_code == 200
                and r.get_json().get("ok") is True)

    r = nocookie.post(f"/w/{token}xx/{wid}", data={"choice": str(cid)},
                      headers={"X-Requested-With": "shola"})
    ok &= check("a tampered link is refused", r.status_code == 401)

    r = fresh.get("/w/not-a-real-token", follow_redirects=True)
    ok &= check("a junk link sends you to get a new one",
                r.status_code == 200 and b"Send me my link" in r.data)

    r = nocookie.post(f"/w/{token}/{wid}", data={"choice": "custom",
                                                 "custom_text": ""},
                      headers={"X-Requested-With": "shola"})
    ok &= check("empty typed answer is rejected", r.status_code == 400)

    print("\nan answer can be changed")
    other = None
    with app.app_context():
        vol = Volunteer.query.filter_by(email="ama@example.com").first()
        cands = [c.id for c in db.session.get(Word, wid).candidates
                 if c.language == "twi"]
        other = cands[1]
    r = nocookie.post(f"/w/{token}/{wid}", data={"choice": str(other)},
                      headers={"X-Requested-With": "shola"})
    ok &= check("re-answering is accepted", r.status_code == 200)
    with app.app_context():
        evs = Evaluation.query.filter_by(word_id=wid).all()
        ok &= check("still exactly one verdict for that word", len(evs) == 1,
                    f"{len(evs)} verdicts")
        ok &= check("the verdict now holds the new choice",
                    evs[0].candidate_id == other)

    print("\nthe closed sheet cannot swallow clicks")
    css = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "shola/static/css/shola.css")).read()
    sheet = css.split(".sheet {")[1].split("}")[0]
    ok &= check("closed sheet is pointer-transparent",
                "pointer-events: none" in sheet and "visibility: hidden" in sheet,
                "an invisible overlay over the word card blocks the options")
    opened = css.split(".sheet.open {")[1].split("}")[0]
    ok &= check("open sheet takes pointer events back",
                "pointer-events: auto" in opened)

    r = fresh.get(f"/w/{token}")
    ok &= check("evaluate page offers the back control",
                b'id="back-btn"' in r.data)

    r = fresh.get("/api/consensus/twi")
    ok &= check("consensus API responds", r.status_code == 200)
    r = fresh.get("/api/consensus/nope")
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
        ok &= check("html carries the personalised link", "/w/" in html)
        link = daily_link(vol)
        token = link.rsplit("/", 1)[-1]
        ok &= check("token round-trips to the volunteer", read_token(token) == vol.id)
        ok &= check("tampered token is refused", read_token(token + "x") is None)

    print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
