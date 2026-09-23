"""Checks for the model scoreboard.

The point being tested is that the numbers are earned. An option a volunteer
typed must never be scored as a machine's, and a model whose wording a speaker
types out by hand must get the credit anyway.

The CSV `model` column that used to feed this is gone with the upload path.
`Candidate.source` is set by `import-words` and by `shola name-model`, and that
is what the board reads.
"""

import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shola import create_app                                    # noqa: E402
from shola import scoreboard                                    # noqa: E402
from shola.assignment import record_verdict                     # noqa: E402
from shola.config import Config                                 # noqa: E402
from shola.models import (Candidate, Project, ProjectLanguage,   # noqa: E402
                          Volunteer, Word, db)

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
        SMTP_USER = "x@example.com"
        SMTP_PASSWORD = "y"

    return create_app(T)


def build(app, rows, slug="scored"):
    """A project holding `rows`, each (text, [(option, model), ...]).

    Built directly rather than through a file: options carry the name of
    whatever wrote them, and that is all the board needs.
    """
    project = Project(slug=slug, title=slug, item_format="word",
                      has_options=True, votes_to_settle=3, status="approved",
                      sort_order=50)
    db.session.add(project)
    db.session.flush()
    for code in ("twi", "ewe"):
        db.session.add(ProjectLanguage(project_id=project.id, language=code))
    for text, options in rows:
        word = Word(phrase=text, project_id=project.id, position=1,
                    occurrences=0, frequency=0.0, tier=1)
        db.session.add(word)
        db.session.flush()
        for slot, (option, model) in enumerate(options, start=1):
            db.session.add(Candidate(word_id=word.id, language="twi",
                                     position=slot, text=option, source=model))
    db.session.commit()
    return project


def speaker(email, language="twi"):
    v = Volunteer(name="Test Person", email=email, language=language)
    db.session.add(v)
    db.session.commit()
    return v


def option(project, text, language="twi"):
    return (Candidate.query.join(Word, Candidate.word_id == Word.id)
            .filter(Word.project_id == project.id,
                    Candidate.language == language,
                    Candidate.text == text).first())


def test_scores(app):
    print("\nScoring what speakers picked")
    with app.app_context():
        project = build(app, [(f"item {i}", [(f"a-{i}", "model-a"),
                                            (f"b-{i}", "model-b")])
                              for i in range(4)])

        check("nothing is scored before anyone answers",
              scoreboard.scores(project.id) == [])

        # Three speakers pick model-a's wording on three items, one picks
        # model-b's on the fourth.
        for i in range(3):
            v = speaker(f"p{i}@example.com")
            opt = option(project, f"a-{i}")
            record_verdict(v, opt.word_id, candidate_id=opt.id)
        v = speaker("p3@example.com")
        opt = option(project, "b-3")
        record_verdict(v, opt.word_id, candidate_id=opt.id)
        db.session.commit()

        rows = {r["name"]: r for r in scoreboard.scores(project.id)}
        check("both models are on the board", set(rows) == {"model-a", "model-b"},
              str(sorted(rows)))
        check("model-a offered four times, picked three",
              rows["model-a"]["offered"] == 4 and rows["model-a"]["picked"] == 3,
              str(rows.get("model-a")))
        check("model-b offered four times, picked once",
              rows["model-b"]["offered"] == 4 and rows["model-b"]["picked"] == 1,
              str(rows.get("model-b")))
        check("rate is picked over offered",
              abs(rows["model-a"]["rate"] - 0.75) < 1e-9,
              str(rows["model-a"]["rate"]))
        check("the better model sorts first",
              scoreboard.scores(project.id)[0]["name"] == "model-a")
        check("wins are its own where the models differed",
              rows["model-a"]["sole"] == 3, str(rows["model-a"]["sole"]))

        h2h = {(r["left"], r["right"]): r
               for r in scoreboard.head_to_head(project.id)}
        row = h2h.get(("model-a", "model-b"))
        check("head to head compares only where they disagreed",
              row is not None and row["compared"] == 4, str(row))
        check("head to head is 3-1", row and row["left_wins"] == 3
              and row["right_wins"] == 1, str(row))


def test_credit_and_baseline(app):
    print("\nCredit where it is due")
    with app.app_context():
        project = build(app, [("greeting", [("agoo", "model-a"),
                                           ("mema wo akye", "model-b")])],
                        slug="credit")
        v = speaker("typer@example.com")
        opt = option(project, "agoo")
        # Typed out by hand rather than tapped, and in a different case.
        record_verdict(v, opt.word_id, custom_text="AGOO")
        db.session.commit()

        rows = {r["name"]: r for r in scoreboard.scores(project.id)}
        check("a model gets the credit when its wording is typed, not tapped",
              rows["model-a"]["picked"] == 1, str(rows.get("model-a")))
        check("the model that was not agreed with scores nothing",
              rows["model-b"]["picked"] == 0, str(rows.get("model-b")))
        check("a typed wording is not itself scored as a system",
              "volunteer" not in rows, str(sorted(rows)))

        # A wording nobody offered credits nobody, but the offer still counts
        # against both: they were on screen and were not chosen.
        v2 = speaker("other@example.com")
        record_verdict(v2, opt.word_id, custom_text="mema wo adwo")
        db.session.commit()
        rows = {r["name"]: r for r in scoreboard.scores(project.id)}
        check("an answer neither model proposed is a miss for both",
              rows["model-a"]["offered"] == 2 and rows["model-a"]["picked"] == 1
              and rows["model-b"]["picked"] == 0, str(rows))

        # An unattributed project is the human baseline, not a nameless model.
        human = build(app, [("water", [("nsuo", "human"), ("nsu", "human")])],
                      slug="unattributed")
        v3 = speaker("third@example.com")
        h_opt = option(human, "nsuo")
        record_verdict(v3, h_opt.word_id, candidate_id=h_opt.id)
        db.session.commit()
        rows = {r["name"]: r for r in scoreboard.scores(human.id)}
        check("options naming no model score as people",
              list(rows) == ["human"] and rows["human"]["picked"] == 1,
              str(rows))
        check("the human row is flagged as the baseline",
              rows["human"]["human"] is True)

        check("a project's board only counts its own answers",
              {r["name"] for r in scoreboard.scores(project.id)}
              == {"model-a", "model-b"})
        check("site-wide covers every project",
              {r["name"] for r in scoreboard.scores()}
              >= {"model-a", "model-b", "human"})
        check("min_offered filters thin evidence",
              scoreboard.scores(project.id, min_offered=99) == [])


def test_shared_wording(app):
    print("\nWhen two systems say the same thing")
    with app.app_context():
        # One option, both names on it - which is how a file records two
        # systems that agreed, rather than showing the same option twice.
        project = build(app, [("greeting", [("agoo", "model-a;model-b"),
                                           ("mema wo akye", "model-c")])],
                        slug="agreed")
        v = speaker("shared@example.com")
        opt = option(project, "agoo")
        record_verdict(v, opt.word_id, candidate_id=opt.id)
        db.session.commit()

        rows = {r["name"]: r for r in scoreboard.scores(project.id)}
        check("both systems behind the option are on the board",
              set(rows) == {"model-a", "model-b", "model-c"}, str(sorted(rows)))
        check("and both are credited for the pick",
              rows["model-a"]["picked"] == 1 and rows["model-b"]["picked"] == 1,
              str(rows))
        check("a shared win is not counted as its own",
              rows["model-a"]["sole"] == 0, str(rows["model-a"]["sole"]))
        check("the system that was not picked scores nothing",
              rows["model-c"]["picked"] == 0, str(rows.get("model-c")))
        check("named_models lists them separately",
              scoreboard.named_models(project.id)
              == ["model-a", "model-b", "model-c"],
              str(scoreboard.named_models(project.id)))


def test_api(app):
    print("\nThe endpoint")
    with app.app_context():
        project = build(app, [("greeting", [("agoo", "model-a"),
                                           ("mema wo akye", "model-b")])],
                        slug="served")
        v = speaker("api@example.com")
        opt = option(project, "agoo")
        record_verdict(v, opt.word_id, candidate_id=opt.id)
        db.session.commit()
        slug = project.slug

    client = app.test_client()
    r = client.get("/api/models")
    check("/api/models answers", r.status_code == 200, str(r.status_code))
    names = {m["name"] for m in r.get_json()["models"]}
    check("it lists the systems", {"model-a", "model-b"} <= names, str(names))

    r = client.get(f"/api/models/{slug}")
    check("per-project answers", r.status_code == 200, str(r.status_code))
    check("and names the project", r.get_json()["project"] == slug)

    r = client.get(f"/api/models/{slug}?format=csv")
    check("csv works", r.status_code == 200
          and b"model" in r.data.split(b"\n")[0], r.data[:60])

    r = client.get("/api/models/no-such-project")
    check("an unknown project is a 404", r.status_code == 404,
          str(r.status_code))

    r = client.get("/models")
    check("the scoreboard has its own page", r.status_code == 200,
          str(r.status_code))
    check("and lists the systems on it", b"model-a" in r.data)
    check("with the heading", b"What speakers agree with" in r.data)

    r = client.get("/stats")
    check("progress still renders", r.status_code == 200, str(r.status_code))
    check("without the board on it",
          b"What speakers agree with" not in r.data)
    check("and links to it from the footer", b"/models" in r.data)


def main():
    app = make_app()
    print("Model attribution and the scoreboard")
    test_scores(make_app())
    test_credit_and_baseline(make_app())
    test_shared_wording(make_app())
    test_api(make_app())
    failed = PASSED.count(False)
    print(f"\n{len(PASSED)} checks, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
