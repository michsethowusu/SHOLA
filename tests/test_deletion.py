"""Deleting an account forgets the person and keeps the contribution.

The distinction this has to hold: their name and email go, their answers stay.
An item they helped settle was settled, other people's answers were counted
against theirs, and the published data never carried a name in the first place.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shola import create_app                                   # noqa: E402
from shola.assignment import leaderboard, record_verdict       # noqa: E402
from shola.config import Config                                # noqa: E402
from shola.consensus import tally                              # noqa: E402
from shola.mailer import make_token                            # noqa: E402
from shola.models import Evaluation, Volunteer, Word, db       # noqa: E402
from shola.projects import core_project                        # noqa: E402

PASSED = []


def check(label, cond, detail=""):
    PASSED.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}"
          + (f" — {detail}" if detail and not cond else ""))


def make_app():
    tmp = tempfile.mkdtemp()

    class T(Config):
        TESTING = True
        SECRET_KEY = "test"
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{tmp}/t.db"
        SMTP_USER = "x@example.com"
        SMTP_PASSWORD = "y"

    return create_app(T)


def main():
    app = make_app()
    with app.app_context():
        project = core_project()
        leaver = Volunteer(name="Leaving Person", email="bye@example.com",
                           language="twi", available_days="0,1,2,3,4,5,6")
        stayer = Volunteer(name="Staying Person", email="stay@example.com",
                           language="twi", available_days="0,1,2,3,4,5,6")
        db.session.add_all([leaver, stayer])
        w = Word(phrase="water", project_id=project.id, position=1,
                 occurrences=99, frequency=0.0, tier=1)
        db.session.add(w)
        db.session.commit()
        wid, vid = w.id, leaver.id
        record_verdict(leaver, wid, custom_text="nsuo")
        record_verdict(stayer, wid, custom_text="nsuo")
        db.session.commit()
        before = tally(wid, "twi")["voters"]
        token = make_token(leaver)

    client = app.test_client()

    print("\nIt will not happen by accident")
    client.post(f"/w/{token}/settings", data={"action": "delete"})
    with app.app_context():
        still = db.session.get(Volunteer, vid)
        check("an unconfirmed request does nothing",
              still.email == "bye@example.com", still.email)
    client.post(f"/w/{token}/settings",
                data={"action": "delete", "confirm": "yes please"})
    with app.app_context():
        still = db.session.get(Volunteer, vid)
        check("and nor does the wrong word",
              still.email == "bye@example.com", still.email)

    print("\nThe person is forgotten")
    r = client.post(f"/w/{token}/settings",
                    data={"action": "delete", "confirm": "delete"})
    check("confirming redirects away", r.status_code == 302, str(r.status_code))
    with app.app_context():
        gone = db.session.get(Volunteer, vid)
        check("the row survives", gone is not None)
        check("their email is gone",
              "bye@example.com" not in gone.email, gone.email)
        check("their name is gone", gone.name == "", repr(gone.name))
        check("they are marked erased", gone.deleted_at is not None)
        check("and no longer listed anywhere", gone.listed is False)
        check("no record anywhere holds the address",
              Volunteer.query.filter_by(email="bye@example.com").first() is None)

    print("\nThe contribution stays")
    with app.app_context():
        check("their answer is still recorded",
              Evaluation.query.filter_by(volunteer_id=vid).count() == 1)
        check("the item still has both voters",
              tally(wid, "twi")["voters"] == before,
              str(tally(wid, "twi")["voters"]))
        check("and the wording is still published",
              "nsuo" in tally(wid, "twi")["ranked"][0]["text"])

    print("\nThe link is dead")
    r = client.get(f"/w/{token}", follow_redirects=False)
    check("their words no longer open", r.status_code == 302, str(r.status_code))
    r = client.get(f"/w/{token}/settings", follow_redirects=False)
    check("and neither do their settings — unlike stopping",
          r.status_code == 302, str(r.status_code))

    print("\nAnd they cannot be mailed a way back in")
    import shola.mailer as mailer_mod
    sent = {}
    original = mailer_mod.send
    mailer_mod.send = lambda to, *a, **k: sent.setdefault("to", to)
    try:
        client.post("/login", data={"email": "bye@example.com"})
        check("nothing is sent to the erased address", "to" not in sent,
              str(sent))
        client.post("/login", data={"email": "stay@example.com"})
        check("but a current volunteer still gets one",
              sent.get("to") == "stay@example.com", str(sent))
    finally:
        mailer_mod.send = original

    print("\nEverybody else is untouched")
    with app.app_context():
        check("the other volunteer is intact",
              Volunteer.query.filter_by(email="stay@example.com").count() == 1)
        check("and still on the champions page",
              "Staying Person" in [v.name for v, _ in leaderboard()])

    failed = PASSED.count(False)
    print(f"\n{len(PASSED)} checks, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
