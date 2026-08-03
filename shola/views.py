"""Routes.

There is no login. A volunteer's personalised link carries a signed token that
identifies them, and every evaluation URL includes it, so the flow works from
the email on any device with no account, no password and no dependence on
cookies. The session is only ever used to remember the token for the nav bar;
authorisation always comes from the token in the URL.

An address is confirmed by a one-time code at signup, so the daily emails only
ever go to a mailbox someone actually opened.
"""

import csv
import io
import secrets
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import (Blueprint, Response, abort, current_app, flash, jsonify,
                   redirect, render_template, request, send_from_directory,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from . import consensus
from .assignment import leaderboard, record_verdict, redistribute
from .tiers import active_tier, recruitment, tier_progress, top_up
from .mailer import build_otp_email, make_token, read_token
from .models import Candidate, PendingSignup, Volunteer, Word, db, site_stats

main = Blueprint("main", __name__)

CODE_TTL_MINUTES = 15
MAX_CODE_ATTEMPTS = 6
MAX_CODE_SENDS = 5


def volunteer_from_token(token):
    """The volunteer a personalised link belongs to, or None."""
    vid = read_token(token)
    if not vid:
        return None
    volunteer = db.session.get(Volunteer, vid)
    if not volunteer or not volunteer.active:
        return None
    return volunteer


def language_info(code):
    """Name, special characters and long-press map for any language."""
    return current_app.config["ALL_LANGUAGES"].get(code)


def language_label(code):
    info = language_info(code)
    return info["name"] if info else code


def remembered_token():
    """Token kept from an earlier visit, only so the nav can link onward."""
    return session.get("token")


def shown_languages():
    """Languages worth a row on a table.

    All 88 are open, but a table of 88 mostly-empty rows tells nobody
    anything. A language appears once it has translations loaded, someone
    signed up for it, or a wording typed in it.
    """
    all_langs = current_app.config["ALL_LANGUAGES"]
    live = {code for code, in db.session.query(Volunteer.language).distinct()}
    live |= {code for code, in db.session.query(Candidate.language).distinct()}
    return {code: info for code, info in all_langs.items()
            if info.get("seeded") or code in live}


# ----------------------------------------------------------------- public pages

@main.route("/")
def index():
    return render_template("index.html", stats=site_stats(),
                           champions=leaderboard(limit=5))


@main.route("/champions")
def champions():
    return render_template("champions.html", champions=leaderboard(limit=100),
                           stats=site_stats())


@main.route("/stats")
def stats():
    # Each language works through the tiers at its own pace.
    signed_up = dict(db.session.query(Volunteer.language,
                                      db.func.count(Volunteer.id))
                     .filter(Volunteer.active.is_(True))
                     .group_by(Volunteer.language).all())
    rate = current_app.config["COMPLETION_RATE"]
    target = current_app.config["WORDS_PER_VOLUNTEER"]

    shown = shown_languages()
    by_language = {}
    for code in shown:
        by_language[code] = {
            "tiers": tier_progress(code),
            "active": active_tier(code),
            "recruit": recruitment(code, target, rate,
                                   signed_up.get(code, 0)),
        }
    totals = {
        "volunteers_needed": sum(v["recruit"]["volunteers_needed"]
                                 for v in by_language.values()),
        "still_to_recruit": sum(v["recruit"]["still_to_recruit"]
                                for v in by_language.values()),
        "answers_needed": sum(v["recruit"]["answers_needed"]
                              for v in by_language.values()),
    }
    return render_template("stats.html", stats=site_stats(),
                           per_language=consensus.language_progress(),
                           by_language=by_language, totals=totals,
                           completion_rate=rate, words_per_volunteer=target,
                           SHOWN_LANGUAGES=shown)


@main.route("/brand")
def brand():
    """Media kit. Public on purpose: an influencer should not have to ask."""
    assets = [
        ("story-why-1080x1920.png", 1080, 1920, "WhatsApp Status / story"),
        ("story-how-1080x1920.png", 1080, 1920, "Story — the two minutes"),
        ("story-ask-1080x1920.png", 1080, 1920, "Story — other languages"),
        ("square-why-1080x1080.png", 1080, 1080, "Instagram / Facebook"),
        ("square-ask-1080x1080.png", 1080, 1080, "Instagram / Facebook"),
        ("youtube-lowerthird-1920x1080.png", 1920, 1080,
         "YouTube overlay — lower third, transparent"),
        ("youtube-sidepanel-1920x1080.png", 1920, 1080,
         "YouTube overlay — side panel, transparent"),
        ("youtube-thumbnail-1280x720.png", 1280, 720, "YouTube thumbnail"),
        ("x-post-1600x900.png", 1600, 900, "X"),
        ("facebook-link-1200x630.png", 1200, 630, "Facebook link preview"),
        ("avatar-1080x1080.png", 1080, 1080, "Profile picture"),
        ("wordmark-light-1200x400.png", 1200, 400, "Logo, light background"),
        ("wordmark-dark-1200x400.png", 1200, 400, "Logo, dark background"),
    ]
    n = len(current_app.config["ALL_LANGUAGES"])
    site = current_app.config["SITE_HOST"]
    captions = [
        ("Short", f"Keep your language alive. Two minutes a day. {site}"),
        ("Short", f"{n} Ghanaian languages, and yours is one of them. A few "
                  "words a day helps build accurate translations everyone can "
                  f"use. {site}"),
        ("Pidgin", "You fit speak any Ghanaian language? Give am 2 minutes "
                   f"every day make we keep your language alive. {site}"),
        ("Starting a language", "Nobody has added words in your language yet? "
                               "Then you go be the first. Whatever you type "
                               f"becomes the option others vote on. {site}"),
    ]
    palette = [
        ("Kente red", "#c0392b", "The Ɔ, buttons, links"),
        ("Ink", "#1a1815", "All text"),
        ("Sand", "#faf7f2", "Backgrounds"),
        ("Gold", "#d99b2b", "Highlights only"),
        ("Forest", "#1a635a", "Agreement and success"),
    ]
    return render_template("brand.html", assets=assets, captions=captions,
                           palette=palette)


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


def issue_code(pending):
    """Generate a fresh code, store its hash, and email it."""
    code = f"{secrets.randbelow(1000000):06d}"
    pending.code_hash = generate_password_hash(code)
    pending.attempts = 0
    pending.expires_at = datetime.utcnow() + timedelta(minutes=CODE_TTL_MINUTES)
    db.session.commit()

    from .mailer import send
    subject, text, html = build_otp_email(pending.name, code, CODE_TTL_MINUTES)
    send(pending.email, subject, text, html)


@main.route("/join", methods=["GET", "POST"])
def join():
    if request.method == "GET":
        return render_template("join.html")

    name = (request.form.get("name") or "").strip()
    email = (request.form.get("email") or "").strip().lower()
    language = request.form.get("language") or ""
    if language == "other":
        language = (request.form.get("other_language") or "").strip()
    days = request.form.getlist("days")
    window = request.form.get("time_window") or "anytime"
    consent = bool(request.form.get("photo_consent"))

    errors = []
    if len(name) < 2:
        errors.append("Tell us the name you want on the leaderboard.")
    if "@" not in email or "." not in email.split("@")[-1]:
        errors.append("That email address does not look complete.")
    if language not in current_app.config["ALL_LANGUAGES"]:
        errors.append("Choose the language you speak.")
    if Volunteer.query.filter_by(email=email).first():
        errors.append("That email is already signed up. Ask for your link "
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

    # Held, not created. The Volunteer only exists once the code comes back.
    pending = PendingSignup.query.filter_by(email=email).first()
    if not pending:
        pending = PendingSignup(email=email, code_hash="",
                                expires_at=datetime.utcnow())
        db.session.add(pending)
    pending.name = name
    pending.language = language
    pending.available_days = ",".join(sorted(d for d in days if d.isdigit()))
    pending.time_window = (window if window in current_app.config["TIME_WINDOWS"]
                           else "anytime")
    pending.photo = photo_name or pending.photo
    pending.photo_consent = consent
    pending.sends = 1
    db.session.commit()

    try:
        issue_code(pending)
    except Exception as exc:      # noqa: BLE001 - surface a usable message
        current_app.logger.warning("otp send failed: %s", exc)
        flash("We could not send the code just now. Your details are saved — "
              "try again in a few minutes and we will send a fresh code.",
              "error")
        # Deliberately not 502: Cloudflare replaces a 502 from the origin with
        # its own error page, so the volunteer saw a gateway error instead of
        # this message. 503 reaches them, and still reads as our fault.
        return render_template("join.html", form=request.form), 503

    session["signup_email"] = email
    return redirect(url_for("main.verify"))


@main.route("/verify", methods=["GET", "POST"])
def verify():
    email = (request.form.get("email") or request.args.get("email")
             or session.get("signup_email") or "").strip().lower()
    pending = PendingSignup.query.filter_by(email=email).first() if email else None

    if request.method == "GET":
        if not pending:
            return redirect(url_for("main.join"))
        return render_template("verify.html", email=email)

    if not pending:
        flash("Start again — we have no signup waiting for that address.",
              "error")
        return redirect(url_for("main.join"))

    if pending.expired():
        flash("That code has expired. We can send you a new one.", "error")
        return render_template("verify.html", email=email), 400

    if pending.attempts >= MAX_CODE_ATTEMPTS:
        flash("Too many tries. Ask for a new code.", "error")
        return render_template("verify.html", email=email), 429

    code = (request.form.get("code") or "").strip().replace(" ", "")
    pending.attempts += 1
    db.session.commit()

    if not check_password_hash(pending.code_hash, code):
        left = MAX_CODE_ATTEMPTS - pending.attempts
        flash(f"That code is not right. {left} tries left." if left > 0
              else "That code is not right.", "error")
        return render_template("verify.html", email=email), 400

    # Confirmed: now the volunteer exists and the words are theirs.
    volunteer = Volunteer(
        name=pending.name, email=pending.email, language=pending.language,
        photo=pending.photo,
        photo_consent=pending.photo_consent and bool(pending.photo),
        available_days=pending.available_days,
        time_window=pending.time_window)
    db.session.add(volunteer)
    db.session.delete(pending)
    db.session.commit()

    token = make_token(volunteer)
    session["token"] = token
    session.pop("signup_email", None)

    given = top_up(volunteer, target=current_app.config["WORDS_PER_VOLUNTEER"])
    return render_template("joined.html", volunteer=volunteer, assigned=given,
                           token=token)


@main.route("/verify/resend", methods=["POST"])
def verify_resend():
    email = (request.form.get("email") or session.get("signup_email")
             or "").strip().lower()
    pending = PendingSignup.query.filter_by(email=email).first() if email else None
    if not pending:
        return redirect(url_for("main.join"))
    if pending.sends >= MAX_CODE_SENDS:
        flash("We have sent that address several codes already. "
              "Check your spam folder, or start again later.", "error")
        return render_template("verify.html", email=email), 429
    pending.sends += 1
    db.session.commit()
    try:
        issue_code(pending)
        flash("New code sent.", "ok")
    except Exception as exc:      # noqa: BLE001
        current_app.logger.warning("otp resend failed: %s", exc)
        flash("We could not send that code. Try again in a moment.", "error")
    return render_template("verify.html", email=email)


@main.route("/resend", methods=["GET", "POST"])
def resend():
    """A volunteer who lost the email can ask for a fresh link."""
    if request.method == "GET":
        return render_template("resend.html")

    email = (request.form.get("email") or "").strip().lower()
    volunteer = Volunteer.query.filter_by(email=email).first()
    if volunteer:
        from .mailer import (build_daily_email, build_link_email, daily_link,
                             send)
        try:
            # Someone asking for a link has time to spare. If they have
            # already cleared today's list, give them a fresh one rather than
            # an email announcing zero words.
            top_up(volunteer, target=current_app.config["WORDS_PER_VOLUNTEER"])
            words = [a.word for a in volunteer.pending_today().limit(400)]
            if words:
                message = build_daily_email(volunteer, words)
            else:
                message = build_link_email(
                    volunteer, daily_link(volunteer),
                    "Nothing is waiting for you right now — every word in "
                    "your language is either confirmed or with another "
                    "speaker. We will email you as soon as there is more.")
            send(volunteer.email, *message)
        except Exception as exc:      # noqa: BLE001 - show the operator cause
            current_app.logger.warning("resend failed: %s", exc)
    # Same answer either way: never reveal whether an address is registered.
    flash("If that address is signed up, a fresh link is on its way.", "ok")
    return redirect(url_for("main.resend"))


@main.route("/start/<token>")
def start(token):
    """Older links pointed here; keep them working."""
    return redirect(url_for("main.evaluate", token=token))


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


@main.route("/w/<token>")
def evaluate(token):
    """The personalised link. The token is the only credential needed."""
    volunteer = volunteer_from_token(token)
    if not volunteer:
        flash("That link is not valid any more. We can email you a new one.",
              "error")
        return redirect(url_for("main.resend"))

    # Remembered only so the nav bar can offer a way back; never trusted.
    session["token"] = token

    # Lease whatever the project needs right now, up to today's quota.
    top_up(volunteer, target=current_app.config["WORDS_PER_VOLUNTEER"])

    queue, working_ahead = queue_payload(volunteer)
    remaining = (volunteer.upcoming().count() if working_ahead
                 else volunteer.pending_today().count())
    lang = language_info(volunteer.language)
    return render_template("evaluate.html", volunteer=volunteer, queue=queue,
                           remaining=remaining, lang=lang, token=token,
                           working_ahead=working_ahead,
                           done_total=volunteer.done_count())


@main.route("/w/<token>/<int:word_id>", methods=["POST"])
def submit(token, word_id):
    volunteer = volunteer_from_token(token)
    if not volunteer:
        return jsonify({"error": "link no longer valid"}), 401

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
    return redirect(url_for("main.evaluate", token=token))


@main.route("/w/<token>/done")
def done(token):
    volunteer = volunteer_from_token(token)
    if not volunteer:
        return redirect(url_for("main.index"))
    return render_template("done.html", volunteer=volunteer, token=token,
                           done_total=volunteer.done_count(),
                           target=current_app.config["WORDS_PER_VOLUNTEER"])


# ------------------------------------------------------------------------- api

@main.route("/api")
def api_docs():
    """Human-readable documentation for the words API."""
    shown = shown_languages()
    counts = {code: consensus.verified_count(code) for code in shown}
    sample = consensus.sample_entries("twi", limit=3)
    return render_template("api.html", counts=counts, sample=sample,
                           SHOWN_LANGUAGES=shown)


def _words(language, min_votes, limit, offset):
    rows = []
    for i, row in enumerate(consensus.export_rows(language, min_votes=min_votes)):
        if i < offset:
            continue
        phrase, text, votes, share, total = row
        rows.append({"phrase": phrase, "translation": text, "votes": votes,
                     "agreement": share, "total_votes": total})
        if len(rows) >= limit:
            break
    return rows


@main.route("/api/words/<language>")
def api_words(language):
    """Verified words for one language.

    Only entries where at least `min_votes` speakers chose the same wording.
    """
    if language not in current_app.config["ALL_LANGUAGES"]:
        return jsonify({"error": "unknown language",
                        "languages": list(current_app.config["ALL_LANGUAGES"])}), 404

    from .tiers import VOTES_TO_SETTLE
    min_votes = max(1, request.args.get("min_votes", VOTES_TO_SETTLE, type=int))
    limit = min(max(1, request.args.get("limit", 100, type=int)), 1000)
    offset = max(0, request.args.get("offset", 0, type=int))
    fmt = (request.args.get("format") or "json").lower()

    rows = _words(language, min_votes, limit, offset)

    if fmt == "csv":
        out = io.StringIO()
        w = csv.writer(out, lineterminator="\n")
        w.writerow(["phrase", language, "votes", "agreement", "total_votes"])
        for r in rows:
            w.writerow([r["phrase"], r["translation"], r["votes"],
                        r["agreement"], r["total_votes"]])
        return Response(
            out.getvalue(), mimetype="text/csv",
            headers={"Content-Disposition":
                     f'attachment; filename="shola-{language}.csv"'})

    return jsonify({
        "language": language,
        "language_name": current_app.config["ALL_LANGUAGES"][language]["name"],
        "min_votes": min_votes,
        "offset": offset,
        "limit": limit,
        "returned": len(rows),
        "total_verified": consensus.verified_count(language, min_votes),
        "entries": rows,
    })


# Earlier names, kept so anything already pointing at them keeps working.
@main.route("/api/vocabulary/<language>")
@main.route("/api/consensus/<language>")
def api_consensus(language):
    return api_words(language)


@main.route("/api/entry/<int:word_id>/<language>")
@main.route("/api/word/<int:word_id>/<language>")
def api_word(word_id, language):
    """Every vote on one entry, including wordings that did not win."""
    if language not in current_app.config["ALL_LANGUAGES"]:
        abort(404)
    word = db.session.get(Word, word_id)
    if not word:
        abort(404)
    return jsonify({"word_id": word_id, "phrase": word.phrase,
                    "language": language,
                    "tally": consensus.tally(word_id, language),
                    "agreed": consensus.best(word_id, language)})


@main.route("/healthz")
def healthz():
    """Liveness, plus which build is serving.

    The commit matters: /healthz answers 200 from the old container throughout a
    rolling update, so a deploy that only waits for 200 reports success before
    the new code is live.
    """
    # Whether we can email at all. Nothing else on the site reveals this, and
    # a missing password only surfaced when a volunteer tried to sign up.
    cfg = current_app.config
    return {"ok": True, "today": date.today().isoformat(),
            "build": cfg.get("BUILD", "unknown"),
            "email": bool(cfg.get("SMTP_USER") and cfg.get("SMTP_PASSWORD"))}
