"""The admin side: approving projects and reading flags.

Access works the way the rest of the site works - a signed link, emailed. There
is no password to leak, reuse or store, and the allowlist lives in
`SHOLA_ADMINS` rather than in the database, so gaining admin rights means
changing the deployment rather than editing a row.

The link is short-lived and the session it opens is short-lived, because unlike
a volunteer link this one can approve work and read every report.
"""

from datetime import datetime, timedelta
from functools import wraps

from flask import (Blueprint, current_app, flash, redirect, render_template,
                   request, session, url_for)
from itsdangerous import URLSafeTimedSerializer

from .models import Flag, Project, Volunteer, Word, db
from .projects import item_counts

admin = Blueprint("admin", __name__, url_prefix="/admin")

LINK_MINUTES = 30
SESSION_HOURS = 8


def _serializer():
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"],
                                  salt="shola-admin")


def admin_emails():
    raw = current_app.config.get("ADMIN_EMAILS") or ""
    return [e.strip().lower() for e in raw.split(",") if e.strip()]


def is_admin_email(email):
    return (email or "").strip().lower() in admin_emails()


def make_link(email):
    token = _serializer().dumps({"a": email.strip().lower()})
    base = current_app.config["SITE_URL"].rstrip("/")
    return f"{base}/admin/open/{token}"


def current_admin():
    """The signed-in admin's email, or None.

    Re-checked against the allowlist on every request, not just at sign-in:
    removing someone from SHOLA_ADMINS must lock them out now, not in eight
    hours when their session happens to lapse.
    """
    email = session.get("admin")
    since = session.get("admin_since")
    if not email or not since:
        return None
    try:
        started = datetime.fromisoformat(since)
    except (TypeError, ValueError):
        return None
    if datetime.utcnow() - started > timedelta(hours=SESSION_HOURS):
        return None
    if not is_admin_email(email):
        return None
    return email


def require_admin(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_admin():
            return redirect(url_for("admin.sign_in"))
        return view(*args, **kwargs)
    return wrapped


@admin.route("/", methods=["GET"])
def sign_in():
    if current_admin():
        return redirect(url_for("admin.dashboard"))
    return render_template("admin/sign_in.html")


@admin.route("/link", methods=["POST"])
def send_link():
    """Email a sign-in link, but only ever to an allowlisted address."""
    email = (request.form.get("email") or "").strip().lower()
    if is_admin_email(email):
        from .mailer import build_link_email, send
        try:
            subject, text, html = build_link_email(
                None, make_link(email),
                f"Here is your SHOLA admin link. It works for "
                f"{LINK_MINUTES} minutes.", name="there")
            send(email, subject, text, html)
        except Exception as exc:      # noqa: BLE001
            current_app.logger.warning("admin link failed: %s", exc)
    # Same answer either way: this page must not reveal who the admins are.
    flash("If that address can administer SHOLA, a link is on its way.", "ok")
    return redirect(url_for("admin.sign_in"))


@admin.route("/open/<token>")
def open_link(token):
    try:
        data = _serializer().loads(token, max_age=LINK_MINUTES * 60)
    except Exception:      # noqa: BLE001 - any failure is the same failure
        flash("That link has expired. Ask for another.", "error")
        return redirect(url_for("admin.sign_in"))
    email = (data or {}).get("a", "")
    if not is_admin_email(email):
        flash("That address cannot administer SHOLA.", "error")
        return redirect(url_for("admin.sign_in"))
    session["admin"] = email
    session["admin_since"] = datetime.utcnow().isoformat()
    return redirect(url_for("admin.dashboard"))


@admin.route("/out")
def sign_out():
    session.pop("admin", None)
    session.pop("admin_since", None)
    flash("Signed out.", "ok")
    return redirect(url_for("admin.sign_in"))


@admin.route("/dashboard")
@require_admin
def dashboard():
    pending = (Project.query.filter(Project.status == "pending")
               .order_by(Project.created_at).all())
    live = (Project.query.filter(Project.status == "approved")
            .order_by(Project.sort_order, Project.id).all())
    rejected = (Project.query.filter(Project.status == "rejected")
                .order_by(Project.created_at.desc()).limit(10).all())
    flags = (Flag.query.filter(Flag.resolved.is_(False))
             .order_by(Flag.created_at.desc()).limit(100).all())
    return render_template("admin/dashboard.html", pending=pending, live=live,
                           rejected=rejected, flags=flags,
                           counts={p.id: item_counts(p) for p in pending + live},
                           admin_email=current_admin())


@admin.route("/project/<int:project_id>")
@require_admin
def project(project_id):
    proj = db.session.get(Project, project_id)
    if not proj:
        flash("No such project.", "error")
        return redirect(url_for("admin.dashboard"))
    previews = {code: proj.preview(code, limit=8)
                for code in proj.language_codes}
    return render_template("admin/project.html", project=proj,
                           previews=previews, counts=item_counts(proj),
                           admin_email=current_admin())


@admin.route("/project/<int:project_id>/decide", methods=["POST"])
@require_admin
def decide(project_id):
    proj = db.session.get(Project, project_id)
    if not proj:
        flash("No such project.", "error")
        return redirect(url_for("admin.dashboard"))

    action = request.form.get("action")
    note = (request.form.get("note") or "").strip()[:600]

    if action == "approve":
        if not proj.item_count():
            flash("That project has no items loaded, so there is nothing to "
                  "approve.", "error")
            return redirect(url_for("admin.project", project_id=proj.id))
        proj.status = "approved"
        proj.approved_at = datetime.utcnow()
        proj.review_note = note
        db.session.commit()
        flash(f"Approved. Announce it when you are ready and volunteers can "
              f"opt in.", "ok")
    elif action == "reject":
        proj.status = "rejected"
        proj.review_note = note
        db.session.commit()
        flash("Rejected. The note is kept with the project.", "ok")
    elif action == "announce":
        from .cli import announce_project
        sent = announce_project(proj)
        flash(f"Emailed {sent} volunteers who speak a language this project "
              f"collects.", "ok")
    elif action == "pause":
        proj.status = "paused"
        db.session.commit()
        flash("Paused. No more items from it go out.", "ok")
    elif action == "resume":
        proj.status = "approved"
        db.session.commit()
        flash("Live again.", "ok")
    else:
        flash("Unknown action.", "error")
    return redirect(url_for("admin.project", project_id=proj.id))


@admin.route("/flag/<int:flag_id>", methods=["POST"])
@require_admin
def resolve_flag(flag_id):
    flag = db.session.get(Flag, flag_id)
    if not flag:
        flash("No such report.", "error")
        return redirect(url_for("admin.dashboard"))

    action = request.form.get("action")
    if action == "remove":
        # The item leaves the queue for good. Answers already given stay: they
        # are evidence about the item, and the export can exclude it by project.
        flag.word.tier = 0
        flag.resolved, flag.resolution = True, "removed"
        from .tiers import state_for
        for code in {flag.language} | set(
                c.language for c in flag.word.candidates):
            row = state_for(flag.word.id, code)
            row.contested = True
        db.session.commit()
        flash("Item withdrawn and the report closed.", "ok")
    elif action == "keep":
        flag.resolved, flag.resolution = True, "kept"
        db.session.commit()
        flash("Report closed, item back in the queue.", "ok")
    else:
        flash("Unknown action.", "error")
    return redirect(url_for("admin.dashboard"))
