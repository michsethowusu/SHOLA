"""Daily email over Gmail SMTP.

Gmail needs an app password (Google Account -> Security -> App passwords); the
normal account password will be refused. Set SHOLA_SMTP_USER and
SHOLA_SMTP_PASSWORD and nothing else is required.

The email carries the actual words for the day, not just a link, because a
volunteer should be able to see the work before deciding to open it.
"""

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

from flask import current_app, render_template
from itsdangerous import URLSafeTimedSerializer

MAX_WORDS_IN_EMAIL = 25      # longer lists get a "+N more" line instead


def serializer():
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"],
                                  salt="shola-daily-link")


def make_token(volunteer):
    return serializer().dumps({"v": volunteer.id})


def read_token(token):
    """Return the volunteer id, or None if the link is invalid or expired."""
    max_age = current_app.config["LINK_MAX_AGE_DAYS"] * 86400
    try:
        data = serializer().loads(token, max_age=max_age)
    except Exception:
        return None
    return data.get("v")


def build_otp_email(name, code, minutes):
    """The one-time code that confirms an address at signup."""
    first = (name or "").split()[0] if name else "there"
    ctx = {"first": first, "code": code, "minutes": minutes}
    return (f"{code} is your SHOLA code",
            render_template("email/otp.txt", **ctx),
            render_template("email/otp.html", **ctx))


def build_opened_email(volunteer, language_name, link):
    """Sent once, on the day a volunteer's language starts being collected."""
    first = volunteer.name.split()[0] if volunteer.name else "there"
    ctx = {"first": first, "language_name": language_name, "link": link}
    return (f"{language_name} is open — your first SHOLA words are ready",
            render_template("email/opened.txt", **ctx),
            render_template("email/opened.html", **ctx))


def build_link_email(volunteer, link, note):
    """Just the personalised link, for when there are no words to list."""
    first = volunteer.name.split()[0] if volunteer.name else "there"
    return ("Your SHOLA link",
            render_template("email/link.txt", first=first, link=link, note=note),
            render_template("email/link.html", first=first, link=link, note=note))


def daily_link(volunteer):
    """Absolute link to today's list.

    Built by hand rather than with url_for: the daily send runs from the CLI
    with no request context, where url_for needs SERVER_NAME and otherwise
    raises. SITE_URL is the single source of truth for the public address.
    """
    base = current_app.config["SITE_URL"].rstrip("/")
    return f"{base}/w/{make_token(volunteer)}"


def settings_link(volunteer):
    """Absolute link to their own settings, for the footer of every email."""
    base = current_app.config["SITE_URL"].rstrip("/")
    return f"{base}/w/{make_token(volunteer)}/settings"


def build_daily_email(volunteer, words, overdue_count=0):
    """Return (subject, text, html) for today's list."""
    shown = words[:MAX_WORDS_IN_EMAIL]
    more = max(0, len(words) - len(shown))
    link = daily_link(volunteer)
    first = volunteer.name.split()[0] if volunteer.name else "there"

    n = len(words)
    if overdue_count and overdue_count == n:
        subject = f"{n} words waiting for you, {first}"
    elif n == 1:
        subject = f"One word needs your ear, {first}"
    else:
        subject = f"{n} words for you today, {first}"

    ctx = {"volunteer": volunteer, "words": shown, "more": more,
           "total": n, "link": link, "first": first,
           "overdue_count": overdue_count,
           "settings_link": settings_link(volunteer),
           "language_name": current_app.config["ALL_LANGUAGES"][
               volunteer.language]["name"]}
    text = render_template("email/daily.txt", **ctx)
    html = render_template("email/daily.html", **ctx)
    return subject, text, html


def build_weekly_offer_email(volunteer, words):
    """Offer a lighter schedule after several sends went unanswered.

    Sent once. The tone matters: nothing is owed, nothing was lost, and the
    likeliest explanation is that daily is the wrong shape for their week - not
    that they have failed at anything.
    """
    base = current_app.config["SITE_URL"].rstrip("/")
    token = make_token(volunteer)
    first = volunteer.name.split()[0] if volunteer.name else "there"
    ctx = {
        "first": first,
        "words": words[:MAX_WORDS_IN_EMAIL],
        "more": max(0, len(words) - MAX_WORDS_IN_EMAIL),
        "total": len(words),
        "link": f"{base}/w/{token}",
        "weekly_link": f"{base}/w/{token}/weekly",
        "settings_link": settings_link(volunteer),
        "weekly_size": current_app.config["WORDS_PER_WEEKLY_SEND"],
        "language_name": current_app.config["ALL_LANGUAGES"][
            volunteer.language]["name"],
    }
    subject = f"Would once a week suit you better, {first}?"
    return (subject,
            render_template("email/weekly.txt", **ctx),
            render_template("email/weekly.html", **ctx))


def send(to_email, subject, text, html):
    """Send one message. Raises on failure so the caller can count it."""
    cfg = current_app.config
    missing = [name for name in ("SMTP_USER", "SMTP_PASSWORD")
               if not cfg[name]]
    if missing:
        # Name the one that is actually missing: the pair was reported together
        # once and cost a round of looking at the wrong variable.
        raise RuntimeError("not set: "
                           + ", ".join(f"SHOLA_{n}" for n in missing))

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((cfg["MAIL_FROM_NAME"], cfg["SMTP_USER"]))
    msg["To"] = to_email
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    context = ssl.create_default_context()
    if cfg["SMTP_PORT"] == 465:
        with smtplib.SMTP_SSL(cfg["SMTP_HOST"], cfg["SMTP_PORT"],
                              context=context, timeout=30) as s:
            s.login(cfg["SMTP_USER"], cfg["SMTP_PASSWORD"])
            s.send_message(msg)
    else:
        with smtplib.SMTP(cfg["SMTP_HOST"], cfg["SMTP_PORT"], timeout=30) as s:
            s.starttls(context=context)
            s.login(cfg["SMTP_USER"], cfg["SMTP_PASSWORD"])
            s.send_message(msg)
