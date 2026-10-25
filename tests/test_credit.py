"""Staying off the leaderboard, and changing your mind either way.

The choice is offered at sign-up and again in settings. What must hold is that
it only ever affects one page: an unlisted volunteer's answers still count,
still settle items, and still reach the published data. It is not anonymity -
the published answers carry no name either way.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shola import create_app                                   # noqa: E402
from shola.assignment import leaderboard, record_verdict       # noqa: E402
from shola.config import Config                                # noqa: E402
from shola.mailer import make_token                            # noqa: E402
from shola.models import Volunteer, Word, db                   # noqa: E402
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
        shy = Volunteer(name="Quiet One", email="q@example.com", language="twi",
                        listed=False, available_days="0,1,2,3,4,5,6")
        loud = Volunteer(name="Named One", email="n@example.com", language="twi",
                         available_days="0,1,2,3,4,5,6")
        db.session.add_all([shy, loud])
        words = []
        for i in range(4):
            w = Word(phrase=f"word {i}", project_id=project.id, position=1,
                     occurrences=99, frequency=0.0, tier=1)
            db.session.add(w)
            words.append(w)
        db.session.commit()
        # The quiet one does more work than the named one.
        for w in words:
            record_verdict(shy, w.id, custom_text="nsuo")
        record_verdict(loud, words[0].id, custom_text="nsuo")
        db.session.commit()

        print("\nThe champions page")
        names = [v.name for v, _n in leaderboard()]
        check("somebody who stays off it is not listed",
              "Quiet One" not in names, str(names))
        check("everybody else still is", "Named One" in names, str(names))

        print("\nTheir work still counts")
        check("their answers are recorded",
              shy.evaluations.count() == 4, str(shy.evaluations.count()))
        from shola.consensus import tally
        t = tally(words[0].id, "twi")
        check("and count towards settling an item",
              t["voters"] >= 2, str(t["voters"]))

        tok_shy = make_token(shy)

    print("\nChanging your mind, from settings")
    client = app.test_client()
    r = client.get(f"/w/{tok_shy}/settings")
    check("the setting is on the page", b'name="unlisted"' in r.data)
    check("and shows their current choice",
          r.data.count(b'name="unlisted"') == 1 and b"checked" in r.data)

    client.post(f"/w/{tok_shy}/settings",
                data={"action": "save", "days": ["0", "1"],
                      "time_window": "anytime"})
    with app.app_context():
        again = Volunteer.query.filter_by(email="q@example.com").first()
        check("unticking it asks to be credited", again.listed is True)
        check("and the name appears on the board",
              "Quiet One" in [v.name for v, _n in leaderboard()])

    client.post(f"/w/{tok_shy}/settings",
                data={"action": "save", "days": ["0", "1"],
                      "time_window": "anytime", "unlisted": "1"})
    with app.app_context():
        again = Volunteer.query.filter_by(email="q@example.com").first()
        check("and ticking it again steps back out", again.listed is False)
        check("removing the name once more",
              "Quiet One" not in [v.name for v, _n in leaderboard()])

    print("\nThe way back in")
    r = client.get("/login")
    check("the login page answers", r.status_code == 200, str(r.status_code))
    check("and calls itself Log in", b"Log in" in r.data)
    # Every email already sent points at the old address.
    r = client.get("/resend", follow_redirects=False)
    check("the old address still works", r.status_code == 301, str(r.status_code))
    check("and points at the new one",
          r.headers.get("Location", "").endswith("/login"),
          r.headers.get("Location", ""))
    # This client has been to its settings, so the nav is holding its link.
    r = client.get("/")
    check("the footer offers a way out once you are in", b"Log out" in r.data)
    check("and a way to their settings", b">Settings</a>" in r.data)
    r = client.get("/logout", follow_redirects=False)
    check("logging out redirects away", r.status_code == 302, str(r.status_code))
    r = client.get("/")
    check("and the footer offers the way back in", b"Log in" in r.data)
    check("without still offering to log out", b"Log out" not in r.data)
    check("or a settings link that would not work",
          b">Settings</a>" not in r.data)
    r = client.get("/join")
    check("the sign-up form offers the choice",
          b'name="unlisted"' in r.data)

    failed = PASSED.count(False)
    print(f"\n{len(PASSED)} checks, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
