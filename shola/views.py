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
import re
import secrets
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import (Blueprint, Response, abort, current_app, flash, jsonify,
                   redirect, render_template, request, send_from_directory,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from .config import canonical_language
from . import consensus
from .assignment import leaderboard, record_verdict
from .tiers import (VOTES_TO_SETTLE, active_tier, answers_needed, daily_quota,
                    recruitment, state_for, tier_progress, tier_progress_all,
                    top_up)
from .mailer import build_otp_email, make_token, read_token
from .models import (Assignment, Candidate, Flag, PendingSignup, Project,
                     ProjectLanguage, Volunteer, Word, WordState, db,
                     site_stats)
from . import importer
from .projects import (active_for, approved_projects, item_counts, joined,
                       opt_in, opt_out)

main = Blueprint("main", __name__)

CODE_TTL_MINUTES = 15
MAX_CODE_ATTEMPTS = 6
MAX_CODE_SENDS = 5


def volunteer_from_token(token, require_active=True):
    """The volunteer a personalised link belongs to, or None.

    `require_active=False` is for the settings page: someone who stopped must
    still be able to open their own link and start again, or stopping would be
    a one-way door.
    """
    vid = read_token(token)
    if not vid:
        return None
    volunteer = db.session.get(Volunteer, vid)
    if not volunteer:
        return None
    if require_active and not volunteer.active:
        return None
    return volunteer


ITEM_FORMATS = {
    "word": "Words or short phrases",
    "sentence": "Sentences",
    "paragraph": "Paragraphs",
}


def unique_slug(title):
    """A readable, stable id from the title, with a suffix only if needed."""
    base = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60] or "project"
    slug, n = base, 1
    while Project.query.filter_by(slug=slug).first() is not None:
        n += 1
        slug = f"{base}-{n}"
    return slug


def notify_admins_of_submission(project, total):
    """Tell the admins there is something waiting, without failing the submit.

    A submission that is safely stored must not be reported as a failure
    because an email did not go out; the dashboard shows it either way.
    """
    from .admin import admin_emails, make_link
    from .mailer import build_link_email, send

    for email in admin_emails():
        try:
            subject, text, html = build_link_email(
                None, make_link(email),
                f"“{project.title}” was submitted with {total:,} items in "
                f"{len(project.language_codes)} language(s), waiting for a "
                f"decision.", name="there")
            send(email, f"New SHOLA project: {project.title}", text, html)
        except Exception as exc:      # noqa: BLE001
            current_app.logger.warning("admin notice failed: %s", exc)


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
    # Paused volunteers are not counted as capacity: they are not answering
    # this week, and the forecast is meant to be pessimistic.
    signed_up = dict(db.session.query(Volunteer.language,
                                      db.func.count(Volunteer.id))
                     .filter(Volunteer.active.is_(True))
                     .filter(db.or_(Volunteer.paused_until.is_(None),
                                    Volunteer.paused_until <= date.today()))
                     .group_by(Volunteer.language).all())
    rate = current_app.config["COMPLETION_RATE"]
    target = current_app.config["WORDS_PER_VOLUNTEER"]

    shown = shown_languages()
    live_projects = approved_projects()

    # One pass for every language rather than a scan of the corpus each. The
    # active tier and the answers still outstanding come out of the same rows,
    # so nothing here re-reads what has already been counted.
    tiers = tier_progress_all(shown)
    by_language = {}
    for code in shown:
        rows = tiers.get(code, [])
        open_rows = [r for r in rows if r["left"] > 0]
        tier = open_rows[0]["tier"] if open_rows else None
        needed = answers_needed(code, tier) if tier is not None else 0
        by_language[code] = {
            "tiers": rows,
            "active": tier,
            "recruit": recruitment(code, target, rate, signed_up.get(code, 0),
                                   tier=tier, needed_answers=needed),
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
                           words_per_day=current_app.config["WORDS_PER_DAY"],
                           projects=live_projects,
                           per_project=[{
                               "project": p,
                               "verified": sum(consensus.verified_counts(
                                   project_id=p.id).values()),
                               "typed": sum(consensus.typed_counts(
                                   project_id=p.id).values()),
                               "item_total": p.item_count(),
                           } for p in live_projects],
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


def exclusive_from_request():
    """The project a share link arrived with, if any.

    A project's author gets a link with `?project=<slug>`. Someone joining
    through it works on that project first; that is the whole difference.
    """
    slug = (request.values.get("project") or "").strip()
    if not slug:
        return None
    return Project.query.filter_by(slug=slug, status="approved").first()


@main.route("/join", methods=["GET", "POST"])
def join():
    if request.method == "GET":
        pinned = exclusive_from_request()
        return render_template("join.html", pinned=pinned,
                               all_projects=approved_projects())

    name = (request.form.get("name") or "").strip()
    email = (request.form.get("email") or "").strip().lower()
    language = request.form.get("language") or ""
    if language == "other":
        language = request.form.get("other_language") or ""
    # An old code in a bookmarked link or a shared form still resolves.
    language = canonical_language(language)
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

    # At least one project, and only ones that collect their language: a
    # volunteer with no project would receive an empty list for ever.
    pinned = exclusive_from_request()
    open_ids = {p.id for p in approved_projects(language)} if language else set()
    chosen = [int(p) for p in request.form.getlist("projects")
              if p.isdigit() and int(p) in open_ids]
    if pinned is not None and pinned.id in open_ids and pinned.id not in chosen:
        chosen.append(pinned.id)
    if not chosen:
        errors.append("Choose at least one thing to work on." if open_ids
                      else "Nothing is collecting in that language yet.")

    photo_name = None
    if not errors:
        try:
            photo_name = save_photo(request.files.get("photo"))
        except ValueError as exc:
            errors.append(str(exc))

    if errors:
        for e in errors:
            flash(e, "error")
        return render_template("join.html", form=request.form,
                               pinned=exclusive_from_request(),
                               all_projects=approved_projects()), 400

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
    pending.project_ids = ",".join(str(c) for c in chosen)
    pending.exclusive_project_id = pinned.id if pinned is not None else None
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
        return render_template("join.html", form=request.form,
                               pinned=exclusive_from_request(),
                               all_projects=approved_projects()), 503

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
    db.session.flush()

    # Opt-ins are created here, from the choices held on the pending signup:
    # there was nothing to attach them to until the address proved real.
    wanted = [int(p) for p in (pending.project_ids or "").split(",") if p]
    opt_in(volunteer, wanted, exclusive_id=pending.exclusive_project_id)
    db.session.delete(pending)
    db.session.commit()

    token = make_token(volunteer)
    session["token"] = token
    session.pop("signup_email", None)

    given = top_up(volunteer)
    return render_template("joined.html", volunteer=volunteer, assigned=given,
                           token=token, projects=[p for _vp, p in
                                                 joined(volunteer)])


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
            top_up(volunteer)
            words = [a.word for a in volunteer.pending_today()
                     .limit(current_app.config["WORDS_PER_DAY"])]
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
    """Turn assignments into the card payload the evaluate page renders.

    Each card carries its own wording, because one list can mix projects: a
    sentence to read and a word to translate should not both be introduced as
    "English word".
    """
    lang_name = language_label(language)
    items = []
    for assignment in assignments:
        word = assignment.word
        project = word.project
        noun = project.item_noun if project else "word"
        items.append({
            "word_id": word.id,
            "phrase": word.phrase,
            "project": project.title if project else "",
            "label": ("English " + noun) if word.language is None
                     else noun.capitalize(),
            "ask": (f"How would you say this in {lang_name}?"
                    if word.language is None
                    else f"What should this be in {lang_name}?"),
            "long": bool(project and project.item_format == "paragraph"),
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
    top_up(volunteer)

    queue, working_ahead = queue_payload(volunteer)
    remaining = (volunteer.upcoming().count() if working_ahead
                 else volunteer.pending_today().count())
    lang = language_info(volunteer.language)
    return render_template("evaluate.html", volunteer=volunteer, queue=queue,
                           remaining=remaining, lang=lang, token=token,
                           working_ahead=working_ahead,
                           flag_reasons=FLAG_REASONS,
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
            return jsonify({"error": "empty answer"}), 400
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


PAUSE_LENGTHS = [(7, "a week"), (30, "a month"), (90, "three months")]


@main.route("/w/<token>/weekly")
def go_weekly(token):
    """One tap from the email: switch to a single, longer send each week.

    A GET because it is a link in an email, and mail clients cannot POST. It is
    safe to repeat and simple to undo from the settings page, which is where it
    lands.
    """
    volunteer = volunteer_from_token(token, require_active=False)
    if not volunteer:
        flash("That link is not valid any more. We can email you a new one.",
              "error")
        return redirect(url_for("main.resend"))

    # Keep the day they already had if they had one, so the change is only to
    # how often. Saturday otherwise - most people's freest day.
    days = volunteer.day_numbers
    chosen = days[0] if days else 5
    volunteer.available_days = str(chosen)
    volunteer.active = True
    volunteer.paused_until = None
    volunteer.missed_in_a_row = 0
    db.session.commit()

    day = current_app.config["DAY_NAMES"][chosen]
    flash(f"Done — one email a week now, on {day}.", "ok")
    return redirect(url_for("main.settings", token=token))


@main.route("/w/<token>/settings", methods=["GET", "POST"])
def settings(token):
    """Change days, take a break, or stop - all from the emailed link.

    No login here either. The signed token is the credential, the same one the
    daily email already carries.
    """
    volunteer = volunteer_from_token(token, require_active=False)
    if not volunteer:
        flash("That link is not valid any more. We can email you a new one.",
              "error")
        return redirect(url_for("main.resend"))

    if request.method == "GET":
        if volunteer.active:
            # So the nav offers a way back to their words, as on the evaluate
            # page. Remembered only for that; never trusted as a credential.
            session["token"] = token
        return render_template("settings.html", volunteer=volunteer,
                               token=token, pause_lengths=PAUSE_LENGTHS,
                               send_size=daily_quota(volunteer))

    action = request.form.get("action") or "save"

    if action == "save":
        days = [d for d in request.form.getlist("days") if d.isdigit()
                and 0 <= int(d) <= 6]
        volunteer.available_days = ",".join(sorted(days, key=int))
        window = request.form.get("time_window") or "anytime"
        volunteer.time_window = (window if window in
                                 current_app.config["TIME_WINDOWS"]
                                 else "anytime")
        # Saving settings is also a way back: someone who paused and then
        # changed their days plainly means to carry on.
        volunteer.active = True
        volunteer.paused_until = None
        db.session.commit()
        flash("Saved. Your next words arrive on the days you chose.", "ok")

    elif action == "pause":
        try:
            days = int(request.form.get("pause_days") or 0)
        except ValueError:
            days = 0
        if days > 0:
            volunteer.paused_until = date.today() + timedelta(days=days)
            volunteer.active = True     # a pause is not a stop
            db.session.commit()
            flash(f"Paused. Nothing arrives until "
                  f"{volunteer.paused_until.strftime('%-d %B')}.", "ok")
        else:
            # No end date: stopped until they say otherwise.
            volunteer.active = False
            volunteer.paused_until = None
            db.session.commit()
            flash("Paused. Nothing arrives until you start again.", "ok")

    elif action == "resume":
        volunteer.active = True
        volunteer.paused_until = None
        db.session.commit()
        flash("Welcome back. Your next words arrive on your next day.", "ok")

    elif action == "stop":
        volunteer.active = False
        volunteer.paused_until = None
        # Outstanding words go back to the queue rather than sitting with
        # someone who has left.
        released = (volunteer.assignments
                    .filter(Assignment.status == "pending")
                    .update({"status": "expired"}, synchronize_session=False))
        db.session.commit()
        current_app.logger.info("volunteer %s stopped, released %s words",
                                volunteer.id, released)
        flash("Stopped. No more emails. This link still works if you change "
              "your mind.", "ok")

    return redirect(url_for("main.settings", token=token))


PROJECTS_PER_PAGE = 12


def page_window(page, pages, span=2):
    """Page numbers to show, with None where a gap is elided.

    Listing every page is fine for two and unusable for forty, which is the same
    mistake the whole page was making before it was paged at all.
    """
    if pages <= 7:
        return list(range(1, pages + 1))
    keep = {1, pages}
    keep.update(n for n in range(page - span, page + span + 1)
                if 1 <= n <= pages)
    out, last = [], 0
    for n in sorted(keep):
        if last and n > last + 1:
            out.append(None)
        out.append(n)
        last = n
    return out

PROJECT_SORTS = {
    "recommended": "Recommended",
    "newest": "Newest first",
    "name": "A to Z",
}


@main.route("/projects")
def projects_page():
    """The index of work available: searchable, filterable, paged.

    Built for a list that grows. Counts come from two grouped queries rather
    than one per project, and only the page being shown is measured - the
    previous version called item_count() and preview() for every project, which
    was fine for three and would not have been for thirty.
    """
    q = (request.args.get("q") or "").strip()
    language = canonical_language(request.args.get("language") or "")
    kind = request.args.get("kind") or ""
    sort = request.args.get("sort") or "recommended"
    page = max(1, request.args.get("page", 1, type=int))

    query = Project.query.filter(Project.status.in_(("approved", "paused")))
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(Project.title.ilike(like),
                                    Project.summary.ilike(like)))
    if language:
        query = (query.join(ProjectLanguage,
                            ProjectLanguage.project_id == Project.id)
                 .filter(ProjectLanguage.language == language))
    if kind in ITEM_FORMATS:
        query = query.filter(Project.item_format == kind)

    if sort == "newest":
        query = query.order_by(Project.created_at.desc(), Project.id.desc())
    elif sort == "name":
        query = query.order_by(Project.title.asc())
    else:
        query = query.order_by(Project.sort_order, Project.id)

    total = query.count()
    pages = max(1, -(-total // PROJECTS_PER_PAGE))
    page = min(page, pages)
    shown = (query.limit(PROJECTS_PER_PAGE)
             .offset((page - 1) * PROJECTS_PER_PAGE).all())

    ids = [p.id for p in shown]
    items = dict(db.session.query(Word.project_id, db.func.count(Word.id))
                 .filter(Word.project_id.in_(ids))
                 .group_by(Word.project_id).all()) if ids else {}
    langs = dict(db.session.query(ProjectLanguage.project_id,
                                  db.func.count(ProjectLanguage.id))
                 .filter(ProjectLanguage.project_id.in_(ids))
                 .group_by(ProjectLanguage.project_id).all()) if ids else {}
    done = dict(db.session.query(Word.project_id,
                                 db.func.count(WordState.id))
                .join(WordState, WordState.word_id == Word.id)
                .filter(Word.project_id.in_(ids),
                        WordState.done.is_(True))
                .group_by(Word.project_id).all()) if ids else {}

    # Languages worth offering as a filter: those some project collects.
    filter_languages = sorted(
        {code for (code,) in db.session.query(ProjectLanguage.language)
         .distinct()},
        key=lambda c: current_app.config["ALL_LANGUAGES"].get(
            c, {}).get("name", c))

    return render_template(
        "projects.html", projects=shown, counts=items, langs=langs, done=done,
        total=total, page=page, pages=pages, q=q, language=language,
        kind=kind, sort=sort, sorts=PROJECT_SORTS, formats=ITEM_FORMATS,
        filter_languages=filter_languages,
        window=page_window(page, pages))


@main.route("/projects/<slug>")
def project_page(slug):
    proj = Project.query.filter_by(slug=slug).first()
    if not proj or proj.status not in ("approved", "paused"):
        abort(404)
    language = canonical_language(request.args.get("language") or "") or None
    if language not in proj.language_codes:
        language = proj.language_codes[0] if proj.language_codes else None
    return render_template(
        "project.html", project=proj, language=language,
        preview=proj.preview(language, limit=6), counts=item_counts(proj),
        progress=proj.progress(language),
        verified=consensus.verified_counts(project_id=proj.id),
        typed=consensus.typed_counts(project_id=proj.id),
        share_link=(current_app.config["SITE_URL"].rstrip("/")
                    + url_for("main.join", project=proj.slug)))


@main.route("/w/<token>/projects", methods=["GET", "POST"])
def my_projects(token):
    """Opt in or out, from the volunteer's own link."""
    volunteer = volunteer_from_token(token, require_active=False)
    if not volunteer:
        flash("That link is not valid any more. We can email you a new one.",
              "error")
        return redirect(url_for("main.resend"))

    available = approved_projects(volunteer.language)
    mine = {vp.project_id: vp for vp, _p in joined(volunteer)}

    if request.method == "POST":
        wanted = {int(p) for p in request.form.getlist("projects")
                  if p.isdigit()}
        if not wanted:
            flash("Keep at least one — otherwise there is nothing to send you.",
                  "error")
            return redirect(url_for("main.my_projects", token=token))
        opt_in(volunteer, wanted - set(mine))
        for pid in set(mine) - wanted:
            opt_out(volunteer, pid)
        # Their current list may hold items from a project they just left; hand
        # those back rather than asking for work they opted out of.
        stale = (Assignment.query
                 .join(Word, Word.id == Assignment.word_id)
                 .filter(Assignment.volunteer_id == volunteer.id,
                         Assignment.status == "pending",
                         ~Word.project_id.in_(wanted))
                 .all())
        for item in stale:
            item.status = "expired"
        db.session.commit()
        top_up(volunteer)
        flash("Saved. Your next list comes from the projects you chose.", "ok")
        return redirect(url_for("main.my_projects", token=token))

    return render_template("my_projects.html", volunteer=volunteer, token=token,
                           projects=available, mine=mine,
                           counts={p.id: p.item_count(volunteer.language)
                                   for p in available},
                           previews={p.id: p.preview(volunteer.language,
                                                     limit=3)
                                     for p in available})


FLAG_REASONS = {
    "not-english": "The item is not in the language it should be",
    "nonsense": "The item makes no sense",
    "offensive": "The item is offensive",
    "options-wrong": "None of the options are close",
    "duplicate": "I have seen this one already",
    "other": "Something else",
}


@main.route("/w/<token>/<int:word_id>/flag", methods=["POST"])
def flag_item(token, word_id):
    """Report a problem with an item, mid-task.

    The people reading the data are the only ones who will notice a broken
    item, so they need to be able to say so without leaving the page. A flagged
    item stops being handed out until someone has looked at it.
    """
    volunteer = volunteer_from_token(token)
    if not volunteer:
        return jsonify({"error": "link no longer valid"}), 401

    item = db.session.get(Word, word_id)
    if not item:
        return jsonify({"error": "unknown item"}), 404

    reason = request.form.get("reason") or "other"
    if reason not in FLAG_REASONS:
        reason = "other"
    note = (request.form.get("note") or "").strip()[:600]

    already = Flag.query.filter_by(word_id=word_id,
                                   volunteer_id=volunteer.id).first()
    if not already:
        db.session.add(Flag(word_id=word_id, volunteer_id=volunteer.id,
                            language=volunteer.language, reason=reason,
                            note=note))
    # The item leaves their list either way: they should not be asked again for
    # a verdict they have just told us they cannot give.
    (Assignment.query
     .filter_by(volunteer_id=volunteer.id, word_id=word_id, status="pending")
     .update({"status": "expired"}, synchronize_session=False))
    db.session.commit()
    top_up(volunteer)

    if request.headers.get("X-Requested-With") == "shola":
        nxt, ahead = queue_payload(volunteer, limit=4)
        remaining = (volunteer.upcoming().count() if ahead
                     else volunteer.pending_today().count())
        return jsonify({"ok": True, "remaining": remaining,
                        "done_total": volunteer.done_count(), "next": nxt})
    return redirect(url_for("main.evaluate", token=token))


@main.route("/template.csv")
def template_csv():
    """The example file, so nobody has to retype the header from a screenshot."""
    # Codes come from the config, not written out here: a template carrying a
    # code our own validator rejects is worse than no template, and that is
    # exactly what shipped when this file said "gaa" while the stored code was
    # still "ga".
    seeded = [c for c in current_app.config["LANGUAGES"]][:3]
    while len(seeded) < 3:
        seeded.append(next(iter(current_app.config["ALL_LANGUAGES"])))
    rows = [
        ["Where is the market?", seeded[0], "1", "first way to say it",
         "second way", ""],
        ["Where is the market?", seeded[1], "1", "how it goes here", "", ""],
        ["Where is the market?", seeded[2], "1", "", "", ""],
        ["How much is this?", "all", "2", "", "", ""],
    ]
    return _csv_response(
        ["text", "language", "priority", "option1", "option2", "option3"],
        rows, "shola-template")


@main.route("/languages.csv")
def languages_csv():
    """Every language and its code, for looking up what to put in the column."""
    from .config import ISO_CODES

    rows = []
    for code, info in sorted(current_app.config["ALL_LANGUAGES"].items(),
                             key=lambda kv: kv[1]["name"]):
        iso = ISO_CODES.get(code, code)
        # Both, where they differ for the two languages seeded before ISO codes
        # were used. Either works in a file; nobody should have to guess which.
        rows.append([info["name"], code, iso])
    return _csv_response(["language", "code", "iso_639_3"], rows,
                         "shola-language-codes")


@main.route("/submit", methods=["GET", "POST"])
def submit_project():
    """Anyone can propose a body of work. An admin decides whether it runs."""
    if request.method == "GET":
        return render_template("submit.html", formats=ITEM_FORMATS,
                               languages=current_app.config["ALL_LANGUAGES"])

    title = (request.form.get("title") or "").strip()[:160]
    summary = (request.form.get("summary") or "").strip()[:600]
    item_format = request.form.get("item_format") or "word"

    name = (request.form.get("name") or "").strip()[:120]
    email = (request.form.get("email") or "").strip().lower()[:255]
    org = (request.form.get("org") or "").strip()[:160]
    try:
        threshold = int(request.form.get("votes_to_settle") or VOTES_TO_SETTLE)
    except ValueError:
        threshold = VOTES_TO_SETTLE

    errors = []
    if len(title) < 8:
        errors.append("Give it a title that says what a volunteer will do, "
                      "like “Translate everyday Ghanaian words”.")
    if item_format not in ITEM_FORMATS:
        errors.append("Choose whether the items are words, sentences or "
                      "paragraphs.")
    if "@" not in email or "." not in email.split("@")[-1]:
        errors.append("We need an email address to tell you the outcome.")
    if not 2 <= threshold <= 20:
        errors.append("Each item needs between 2 and 20 answers.")

    # One file, parsed before anything is written: a project that is half
    # imported is worse than one that was refused.
    all_languages = current_app.config["ALL_LANGUAGES"]
    upload = request.files.get("file")
    items, problems, meta = [], [], {}
    if upload is None or not upload.filename:
        problems.append("Choose the CSV file.")
    else:
        items, problems, meta = importer.parse(upload.stream,
                                               known_languages=set(all_languages))

    # The file decides which languages the project collects. `all` on any row
    # means every language, which is what the translation project is.
    languages = sorted(meta.get("languages") or ())
    if meta.get("any_language"):
        languages = sorted(all_languages)

    if errors or problems or not items:
        for e in errors + problems[:20]:
            flash(e, "error")
        return render_template("submit.html", formats=ITEM_FORMATS,
                               languages=all_languages,
                               form=request.form), 400

    has_options = any(item["options"] for item in items)

    project = Project(
        slug=unique_slug(title), title=title, summary=summary,
        item_format=item_format, votes_to_settle=threshold,
        has_options=has_options, status="pending",
        submitter_name=name, submitter_email=email, submitter_org=org,
        sort_order=100)
    db.session.add(project)
    db.session.flush()
    for code in languages:
        db.session.add(ProjectLanguage(project_id=project.id, language=code))
    db.session.flush()

    total, options_made = importer.import_items(project, items)

    current_app.logger.info("project %s submitted: %s items, %s options, %s",
                            project.slug, total, options_made,
                            ",".join(languages))
    notify_admins_of_submission(project, total)
    return render_template("submitted.html", project=project, total=total,
                           options=options_made, languages=languages)


@main.route("/w/<token>/done")
def done(token):
    volunteer = volunteer_from_token(token)
    if not volunteer:
        return redirect(url_for("main.index"))
    # Their own count, plus what their language has confirmed - a shared
    # total to belong to, now that there is no personal target to finish.
    return render_template(
        "done.html", volunteer=volunteer, token=token,
        done_total=volunteer.done_count(),
        language_confirmed=consensus.verified_count(volunteer.language),
        language_name=language_label(volunteer.language))


# ------------------------------------------------------------------------- api

@main.route("/api")
def api_docs():
    """Human-readable documentation for the words API."""
    shown = shown_languages()
    counts = {code: sum(1 for _ in consensus.verified_rows(code))
              for code in shown}
    return render_template("api.html", counts=counts,
                           SHOWN_LANGUAGES=shown,
                           projects=approved_projects())


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


@main.route("/api/projects")
def api_projects():
    """Every project, what it collects, and how far along it is."""
    out = []
    for proj in Project.query.filter(
            Project.status.in_(("approved", "paused"))).order_by(
            Project.sort_order, Project.id).all():
        # Two grouped queries per project rather than two per language: with 88
        # languages the per-language helpers turned this page into 176 queries.
        settled = consensus.settled_counts(project_id=proj.id)
        typed = consensus.typed_counts(project_id=proj.id)
        out.append({
            "slug": proj.slug,
            "title": proj.title,
            "summary": proj.summary,
            "item_format": proj.item_format,
            "status": proj.status,
            "languages": proj.language_codes,
            "items": proj.item_count(),
            "answers_wanted": proj.votes_to_settle,
            "progress": proj.progress(),
            "items_complete": {code: settled.get(code, 0)
                               for code in proj.language_codes},
            "typed_answers": {code: typed.get(code, 0)
                              for code in proj.language_codes},
        })
    return jsonify({"projects": out, "count": len(out)})


def _project_or_404(slug, language):
    """(project, language, error response). One place, three endpoints."""
    proj = Project.query.filter_by(slug=slug).first()
    if not proj:
        return None, None, (jsonify({
            "error": "unknown project",
            "projects": [p.slug for p in
                         Project.query.filter_by(status="approved").all()],
        }), 404)
    code = canonical_language(language)
    if code not in proj.language_codes:
        return None, None, (jsonify({
            "error": "this project does not collect that language",
            "languages": proj.language_codes,
        }), 404)
    return proj, code, None


def _page(rows, limit_default=1000):
    limit = min(max(1, request.args.get("limit", limit_default, type=int)), 5000)
    offset = max(0, request.args.get("offset", 0, type=int))
    return rows[offset:offset + limit], limit, offset


@main.route("/api/items/<slug>/<language>/verified")
def api_verified(slug, language):
    """Items with one clear answer. The list to build a dictionary from."""
    proj, code, err = _project_or_404(slug, language)
    if err:
        return err
    rows = list(consensus.verified_rows(code, project_id=proj.id))
    page, limit, offset = _page(rows)
    if (request.args.get("format") or "").lower() == "csv":
        return _csv_response(
            ["item", "answer", "chose", "of", "from"],
            [[r["item"], r["answer"], r["chose"], r["of"], r["from"]]
             for r in page], f"shola-{proj.slug}-{code}-verified")
    return jsonify({"project": proj.slug, "language": code, "set": "verified",
                    "total": len(rows), "returned": len(page),
                    "offset": offset, "limit": limit, "items": page})


@main.route("/api/items/<slug>/<language>/problem")
def api_problem(slug, language):
    """Items needing a human: skipped past the target, reported, or tied."""
    proj, code, err = _project_or_404(slug, language)
    if err:
        return err
    rows = list(consensus.problem_rows(code, project_id=proj.id))
    page, limit, offset = _page(rows)
    if (request.args.get("format") or "").lower() == "csv":
        return _csv_response(
            ["item", "why", "note"],
            [[r["item"], r["why"], r["note"] or ""] for r in page],
            f"shola-{proj.slug}-{code}-problem")
    return jsonify({"project": proj.slug, "language": code, "set": "problem",
                    "total": len(rows), "returned": len(page),
                    "offset": offset, "limit": limit, "items": page})


@main.route("/api/items/<slug>/<language>")
def api_items(slug, language):
    """Everything collected, with vote counts. The full record."""
    proj, language, err = _project_or_404(slug, language)
    if err:
        return err

    limit = min(max(1, request.args.get("limit", 100, type=int)), 1000)
    offset = max(0, request.args.get("offset", 0, type=int))
    min_votes = max(0, request.args.get("min_votes", 0, type=int))
    fmt = (request.args.get("format") or "json").lower()
    want = (request.args.get("answers") or "all").lower()

    if want == "typed":
        rows = list(consensus.typed_rows(language, project_id=proj.id))
        page = rows[offset:offset + limit]
        if fmt == "csv":
            return _csv_response(["item", "answer", "answered_on"],
                                 [list(r) for r in page],
                                 f"shola-{proj.slug}-{language}-typed")
        return jsonify({
            "project": proj.slug, "language": language, "answers": "typed",
            "total": len(rows), "returned": len(page), "offset": offset,
            "limit": limit,
            "entries": [{"item": r[0], "answer": r[1], "answered_on": r[2]}
                        for r in page],
        })

    ids = consensus.item_ids_with_answers(language, project_id=proj.id)
    page_ids = ids[offset:offset + limit]
    entries, flat = [], []
    for wid in page_ids:
        t = consensus.tally(wid, language)
        if not t["ranked"]:
            continue
        if min_votes and t["ranked"][0]["votes"] < min_votes:
            continue
        item = db.session.get(Word, wid)
        top = consensus.leaders(wid, language)
        state = state_for(wid, language, create=False)
        entries.append({
            "item": item.phrase,
            "total_answers": t["votes"],
            "answers_wanted": proj.votes_to_settle,
            "complete": t["votes"] >= proj.votes_to_settle,
            "skipped_by": state.skips if state else 0,
            "problem": bool(state and state.problem),
            "leading": [r["text"] for r in top],
            "tied": len(top) > 1,
            "answers": [{"answer": r["text"], "chose": r["votes"],
                         "share": round(r["share"], 3),
                         "from": r["source"]} for r in t["ranked"]],
        })
        for r in t["ranked"]:
            flat.append([item.phrase, r["text"], r["votes"],
                         round(r["share"], 3), t["votes"], r["source"],
                         "yes" if r["votes"] == t["ranked"][0]["votes"]
                         else "no"])

    if fmt == "csv":
        return _csv_response(
            ["item", "answer", "chose", "share", "total_answers", "from",
             "leading"], flat, f"shola-{proj.slug}-{language}")

    return jsonify({
        "project": proj.slug, "language": language, "answers": "all",
        "answers_wanted": proj.votes_to_settle,
        "min_votes": min_votes,
        "total": len(ids), "returned": len(entries),
        "offset": offset, "limit": limit, "entries": entries,
    })


def _csv_response(header, rows, stem):
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition":
                             f'attachment; filename="{stem}.csv"'})


@main.route("/api/words/<language>")
def api_words(language):
    """Verified words for one language.

    Only entries where at least `min_votes` speakers chose the same wording.
    """
    language = canonical_language(language)
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
    """Every answer on one entry, with its vote count."""
    language = canonical_language(language)
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
    out = {"ok": True, "today": date.today().isoformat(),
           "build": cfg.get("BUILD", "unknown"),
           "email": bool(cfg.get("SMTP_USER") and cfg.get("SMTP_PASSWORD"))}

    # Free space where the database lives, and how big it has got. A migration
    # failed with "database or disk is full" and there was no way to see that
    # from outside the machine; SQLite needs room for a journal as large as the
    # rows a transaction touches, so headroom is not a detail.
    try:
        import shutil
        from pathlib import Path

        db_path = Path(cfg["SQLALCHEMY_DATABASE_URI"].replace("sqlite:///", ""))
        usage = shutil.disk_usage(db_path.parent if db_path.parent.exists()
                                  else "/")
        out["disk"] = {
            "free_mb": usage.free // (1024 * 1024),
            "total_mb": usage.total // (1024 * 1024),
            "used_pct": round((usage.total - usage.free) / usage.total * 100, 1),
            "db_mb": (db_path.stat().st_size // (1024 * 1024)
                      if db_path.exists() else None),
        }
    except Exception as exc:      # noqa: BLE001 - health must not fail
        out["disk"] = {"error": exc.__class__.__name__}

    # How long since a backup reached the bucket. Twice now the off-site copy
    # has stopped for days while everything looked fine, so it is reported
    # rather than waiting for somebody to think of checking.
    try:
        from pathlib import Path

        marker = Path(current_app.config["UPLOAD_DIR"]).parent / "last-backup.txt"
        if marker.exists():
            when = datetime.fromisoformat(marker.read_text().strip())
            days = (datetime.utcnow() - when).days
            out["backup"] = {"last": when.date().isoformat(), "days_ago": days,
                             "stale": days > 2}
        else:
            out["backup"] = {"last": None, "stale": True,
                             "note": "no successful backup recorded"}
    except Exception as exc:      # noqa: BLE001
        out["backup"] = {"error": exc.__class__.__name__}
    return out
