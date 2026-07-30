"""Routes.

The evaluation flow is the whole product, so it is built to be fast: one word
per screen, the verdict posts in the background, and the next word is already
in the page. It also works with JavaScript switched off, in which case each
verdict is a normal form post and a redirect.
"""

import secrets
from datetime import date
from pathlib import Path

from flask import (Blueprint, abort, current_app, flash, jsonify, redirect,
                   render_template, request, send_from_directory, session,
                   url_for)
from werkzeug.utils import secure_filename

from . import consensus
from .assignment import assign_words, leaderboard, record_verdict, redistribute
from .mailer import read_token
from .models import Candidate, Volunteer, db, site_stats

main = Blueprint("main", __name__)


def current_volunteer():
    vid = session.get("volunteer_id")
    if not vid:
        return None
    return db.session.get(Volunteer, vid)


def require_volunteer():
    v = current_volunteer()
    if not v:
        abort(redirect(url_for("main.resend")))
    return v


# ----------------------------------------------------------------- public pages

@main.route("/")
def index():
    return render_template("index.html", stats=site_stats(),
                           champions=leaderboard(limit=5))


@main.route("/about")
def about():
    return render_template("about.html", stats=site_stats())


@main.route("/champions")
def champions():
    return render_template("champions.html", champions=leaderboard(limit=100),
                           stats=site_stats())


@main.route("/stats")
def stats():
    return render_template("stats.html", stats=site_stats(),
                           per_language=consensus.language_progress())


@main.route("/photo/<path:filename>")
def photo(filename):
    return send_from_directory(current_app.config["UPLOAD_DIR"], filename)


# ---------------------------------------------------------------- registration

def save_photo(file_storage):
    """Store a square thumbnail; returns the stored filename or None."""
    if not file_storage or not file_storage.filename:
        return None
    ext = Path(secure_filename(file_storage.filename)).suffix.lower()
    if ext not in current_app.config["ALLOWED_PHOTO_EXT"]:
        raise ValueError("Photos need to be a JPG, PNG or WEBP file.")

    name = f"{secrets.token_hex(8)}{ext}"
    path = Path(current_app.config["UPLOAD_DIR"]) / name
    try:
        from PIL import Image, ImageOps
        img = Image.open(file_storage.stream)
        img = ImageOps.exif_transpose(img)
        edge = current_app.config["PHOTO_SIZE"]
        img = ImageOps.fit(img.convert("RGB"), (edge, edge))
        img.save(path, quality=88)
    except ImportError:
        file_storage.stream.seek(0)
        file_storage.save(path)
    return name


@main.route("/join", methods=["GET", "POST"])
def join():
    if request.method == "GET":
        return render_template("join.html")

    name = (request.form.get("name") or "").strip()
    email = (request.form.get("email") or "").strip().lower()
    language = request.form.get("language") or ""
    days = request.form.getlist("days")
    window = request.form.get("time_window") or "anytime"
    consent = bool(request.form.get("photo_consent"))

    errors = []
    if len(name) < 2:
        errors.append("Tell us the name you want on the leaderboard.")
    if "@" not in email or "." not in email.split("@")[-1]:
        errors.append("That email address does not look complete.")
    if language not in current_app.config["LANGUAGES"]:
        errors.append("Choose the language you speak.")
    if Volunteer.query.filter_by(email=email).first():
        errors.append("That email is already signed up. Ask for a new link "
                      "instead.")

    photo_name = None
    if not errors:
        try:
            photo_name = save_photo(request.files.get("photo"))
        except ValueError as exc:
            errors.append(str(exc))

    if errors:
        for e in errors:
            flash(e, "error")
        return render_template("join.html", form=request.form), 400

    volunteer = Volunteer(
        name=name, email=email, language=language, photo=photo_name,
        photo_consent=consent and bool(photo_name),
        available_days=",".join(sorted(d for d in days if d.isdigit())),
        time_window=window if window in current_app.config["TIME_WINDOWS"]
        else "anytime")
    db.session.add(volunteer)
    db.session.commit()

    given = assign_words(volunteer, current_app.config["WORDS_PER_VOLUNTEER"],
                         horizon_days=current_app.config["COMMITMENT_DAYS"])
    session["volunteer_id"] = volunteer.id
    return render_template("joined.html", volunteer=volunteer, assigned=given)


@main.route("/resend", methods=["GET", "POST"])
def resend():
    """A volunteer who lost the email can ask for a fresh link."""
    if request.method == "GET":
        return render_template("resend.html")

    email = (request.form.get("email") or "").strip().lower()
    volunteer = Volunteer.query.filter_by(email=email).first()
    if volunteer:
        from .mailer import build_daily_email, send
        words = [a.word for a in volunteer.pending_today().limit(200)]
        try:
            subject, text, html = build_daily_email(volunteer, words)
            send(volunteer.email, subject, text, html)
        except Exception as exc:      # noqa: BLE001 - show the operator cause
            current_app.logger.warning("resend failed: %s", exc)
    # Same answer either way: never reveal whether an address is registered.
    flash("If that address is signed up, a fresh link is on its way.", "ok")
    return redirect(url_for("main.resend"))


@main.route("/start/<token>")
def start(token):
    """Open the daily link from an email and begin evaluating."""
    vid = read_token(token)
    if not vid:
        flash("That link has expired. Ask for a fresh one below.", "error")
        return redirect(url_for("main.resend"))
    volunteer = db.session.get(Volunteer, vid)
    if not volunteer or not volunteer.active:
        return redirect(url_for("main.index"))
    session["volunteer_id"] = volunteer.id
    return redirect(url_for("main.evaluate"))


@main.route("/leave")
def leave():
    session.clear()
    return redirect(url_for("main.index"))


# ------------------------------------------------------------ evaluation flow

def as_cards(assignments, language):
    """Turn assignments into the card payload the evaluate page renders."""
    items = []
    for assignment in assignments:
        word = assignment.word
        items.append({
            "word_id": word.id,
            "phrase": word.phrase,
            "options": [{"id": c.id, "text": c.text}
                        for c in sorted(word.options(language),
                                        key=lambda c: c.position)],
        })
    return items


def queue_payload(volunteer, limit=12):
    """Words due now; failing that, the next scheduled ones."""
    due = volunteer.pending_today().limit(limit).all()
    if due:
        return as_cards(due, volunteer.language), False
    ahead = volunteer.upcoming().limit(limit).all()
    return as_cards(ahead, volunteer.language), bool(ahead)


@main.route("/evaluate")
def evaluate():
    volunteer = current_volunteer()
    if not volunteer:
        return redirect(url_for("main.resend"))

    queue, working_ahead = queue_payload(volunteer)
    remaining = (volunteer.upcoming().count() if working_ahead
                 else volunteer.pending_today().count())
    lang = current_app.config["LANGUAGES"][volunteer.language]
    return render_template("evaluate.html", volunteer=volunteer, queue=queue,
                           remaining=remaining, lang=lang,
                           working_ahead=working_ahead,
                           done_total=volunteer.done_count())


@main.route("/evaluate/<int:word_id>", methods=["POST"])
def submit(word_id):
    volunteer = current_volunteer()
    if not volunteer:
        return jsonify({"error": "session expired"}), 401

    choice = request.form.get("choice") or ""
    custom = (request.form.get("custom_text") or "").strip()

    candidate_id = None
    skipped = False
    if choice == "skip":
        skipped = True
    elif choice == "custom":
        if not custom:
            return jsonify({"error": "empty translation"}), 400
    elif choice.isdigit():
        candidate = db.session.get(Candidate, int(choice))
        if not candidate or candidate.word_id != word_id:
            return jsonify({"error": "unknown option"}), 400
        candidate_id = candidate.id
        custom = ""          # a chosen option and own wording are exclusive
    else:
        return jsonify({"error": "no choice"}), 400

    record_verdict(volunteer, word_id, candidate_id=candidate_id,
                   custom_text=custom if choice == "custom" else None,
                   skipped=skipped)

    if request.headers.get("X-Requested-With") == "shola":
        nxt, ahead = queue_payload(volunteer, limit=4)
        remaining = (volunteer.upcoming().count() if ahead
                     else volunteer.pending_today().count())
        return jsonify({"ok": True, "remaining": remaining,
                        "done_total": volunteer.done_count(), "next": nxt})
    return redirect(url_for("main.evaluate"))


@main.route("/done")
def done():
    volunteer = current_volunteer()
    if not volunteer:
        return redirect(url_for("main.index"))
    return render_template("done.html", volunteer=volunteer,
                           done_total=volunteer.done_count(),
                           target=current_app.config["WORDS_PER_VOLUNTEER"])


# ------------------------------------------------------------------------- api

@main.route("/api/consensus/<language>")
def api_consensus(language):
    if language not in current_app.config["LANGUAGES"]:
        abort(404)
    min_votes = request.args.get("min_votes", 2, type=int)
    limit = min(request.args.get("limit", 100, type=int), 1000)
    rows = []
    for phrase, text, votes, share, total in consensus.export_rows(
            language, min_votes=min_votes):
        rows.append({"phrase": phrase, "translation": text, "votes": votes,
                     "share": share, "total_votes": total})
        if len(rows) >= limit:
            break
    return jsonify({"language": language, "min_votes": min_votes,
                    "count": len(rows), "results": rows})


@main.route("/api/word/<int:word_id>/<language>")
def api_word(word_id, language):
    if language not in current_app.config["LANGUAGES"]:
        abort(404)
    return jsonify({"word_id": word_id, "language": language,
                    "tally": consensus.tally(word_id, language),
                    "agreed": consensus.best(word_id, language)})


@main.route("/healthz")
def healthz():
    return {"ok": True, "today": date.today().isoformat()}
