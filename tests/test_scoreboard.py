"""Checks for model attribution: the CSV column, and the scoreboard it feeds.

The point being tested is that the numbers are earned. A file that names no
model must not put anything on the board, an option a volunteer typed must
never be scored as a machine's, and a model whose wording a speaker types out
by hand must get the credit anyway.
"""

import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shola import create_app                                    # noqa: E402
from shola import importer, scoreboard                          # noqa: E402
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


def parse(text, langs=("twi", "ewe")):
    return importer.parse(text.encode("utf-8"), set(langs))


def build(app, csv_text, slug="scored"):
    """Import a file into a fresh project and return it."""
    items, problems, _ = parse(csv_text)
    assert not problems, problems
    project = Project(slug=slug, title=slug, item_format="sentence",
                      has_options=True, votes_to_settle=3, status="approved",
                      sort_order=50)
    db.session.add(project)
    db.session.flush()
    for code in ("twi", "ewe"):
        db.session.add(ProjectLanguage(project_id=project.id, language=code))
    importer.import_items(project, items)
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


# --------------------------------------------------------------- the column

def test_column(app):
    print("\nThe model column")
    with app.app_context():
        items, problems, _ = parse(
            "text,language,option1,option2,model1,model2\n"
            "hello,twi,agoo,mema wo akye,model-a,model-b\n")
        check("file with per-option models parses", not problems, str(problems))
        check("models recorded per option",
              items[0]["models"]["twi"] == ["model-a", "model-b"],
              str(items[0].get("models")))

        items, _, _ = parse(
            "text,language,option1,option2,model\n"
            "hello,twi,agoo,mema wo akye,one-model\n")
        check("a single model column covers the row",
              items[0]["models"]["twi"] == ["one-model", "one-model"],
              str(items[0]["models"]["twi"]))

        items, _, _ = parse("text,language,option1,option2\n"
                            "hello,twi,agoo,mema wo akye\n")
        check("no model column means human",
              items[0]["models"]["twi"] == ["human", "human"],
              str(items[0]["models"]["twi"]))

        # A model named for slot 2 must land on the option written under
        # option2, not on whatever happened to be the second non-empty cell.
        items, _, _ = parse(
            "text,language,option1,option2,model2\n"
            "hello,twi,,mema wo akye,model-b\n")
        check("slot numbers survive a blank earlier option",
              items[0]["models"]["twi"] == ["model-b"],
              str(items[0]["models"]["twi"]))

        # priority sits between the language and the options, and must not be
        # mistaken for one of them.
        items, _, _ = parse(
            "text,language,priority,option1,option2,model1,model2\n"
            "hello,twi,2,agoo,mema wo akye,model-a,model-b\n")
        check("priority column is not read as an option",
              items[0]["options"]["twi"] == ["agoo", "mema wo akye"]
              and items[0]["models"]["twi"] == ["model-a", "model-b"],
              f"{items[0]['options']['twi']} / {items[0]['models']['twi']}")

        project = build(app, "text,language,option1,option2,model1,model2\n"
                             "hello,twi,agoo,mema wo akye,model-a,model-b\n",
                        slug="imported")
        sources = sorted(c.source for c in Candidate.query.join(
            Word, Candidate.word_id == Word.id).filter(
            Word.project_id == project.id).all())
        check("import writes the model onto each option",
              sources == ["model-a", "model-b"], str(sources))


# ------------------------------------------------------------ the scoreboard

def test_scores(app):
    print("\nScoring what speakers picked")
    with app.app_context():
        csv_text = ("text,language,option1,option2,model1,model2\n"
                    + "".join(f"item {i},twi,a-{i},b-{i},model-a,model-b\n"
                              for i in range(4)))
        project = build(app, csv_text)

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
        project = build(app, "text,language,option1,option2,model1,model2\n"
                             "greeting,twi,agoo,mema wo akye,model-a,model-b\n",
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
        human = build(app, "text,language,option1,option2\n"
                           "water,twi,nsuo,nsu\n", slug="unattributed")
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
        project = build(app, "text,language,option1,option2,model1,model2\n"
                             "greeting,twi,agoo,mema wo akye,"
                             "model-a;model-b,model-c\n",
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


def test_language_scoping(app):
    print("\nWho sees which item")
    with app.app_context():
        # A Twi sentence whose options are English is a Twi item. Ewe speakers
        # must not be handed it.
        items, problems, _ = parse(
            "text,language,option1,model1\n"
            "Wo ho te sen?,twi,How are you?,model-a\n"
            "Efoa?,ewe,How are you?,model-a\n")
        check("a one-language item with options is filed under it",
              not problems and items[0]["item_language"] == "twi",
              str(items[0].get("item_language")))
        check("and so is the next one, under its own",
              items[1]["item_language"] == "ewe",
              str(items[1].get("item_language")))

        # A shared prompt still reaches everyone.
        items, _, _ = parse("text,language,option1\n"
                            "water,twi,nsuo\n"
                            "water,ewe,tsi\n")
        check("an item under several languages stays open to all",
              items[0]["item_language"] is None,
              str(items[0].get("item_language")))
        items, _, _ = parse("text,language\nwater,all\n")
        check("and so does one marked all", items[0]["item_language"] is None,
              str(items[0].get("item_language")))

        project = build(app, "text,language,option1,model1\n"
                             "Wo ho te sen?,twi,How are you?,model-a\n",
                        slug="scoped")
        from shola.tiers import open_query
        check("Twi speakers are offered it",
              open_query("twi", project.id).count() == 1)
        check("Ewe speakers are not",
              open_query("ewe", project.id).count() == 0)


def test_api(app):
    print("\nThe endpoint")
    with app.app_context():
        project = build(app, "text,language,option1,option2,model1,model2\n"
                             "greeting,twi,agoo,mema wo akye,model-a,model-b\n",
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

    r = client.get("/stats")
    check("the stats page renders with a board", r.status_code == 200,
          str(r.status_code))
    check("and shows the heading", b"What speakers agree with" in r.data)


def main():
    app = make_app()
    print("Model attribution and the scoreboard")
    test_column(app)
    test_scores(make_app())
    test_credit_and_baseline(make_app())
    test_shared_wording(make_app())
    test_language_scoping(make_app())
    test_api(make_app())
    failed = PASSED.count(False)
    print(f"\n{len(PASSED)} checks, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
