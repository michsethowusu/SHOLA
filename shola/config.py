"""Configuration. Every secret comes from the environment; see .env.example."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
INSTANCE_DIR = BASE_DIR / "instance"

# The four languages SHOLA collects, with the characters that a standard
# keyboard cannot type. These drive both the long-press keyboard and the
# visual identity, so they live in one place.
LANGUAGES = {
    "twi": {
        "name": "Twi",
        "note": "Asante Twi",
        "special": ["ɛ", "Ɛ", "ɔ", "Ɔ", "ŋ", "Ŋ"],
        # base letter -> variants offered on long press
        "longpress": {"e": ["ɛ", "Ɛ"], "o": ["ɔ", "Ɔ"], "n": ["ŋ", "Ŋ"]},
    },
    "ewe": {
        "name": "Ewe",
        "note": "Eʋegbe",
        "special": ["ɖ", "Ɖ", "ɛ", "Ɛ", "ƒ", "Ƒ", "ɣ", "Ɣ", "ŋ", "Ŋ", "ɔ", "Ɔ",
                    "ʋ", "Ʋ"],
        "longpress": {"d": ["ɖ", "Ɖ"], "e": ["ɛ", "Ɛ"], "f": ["ƒ", "Ƒ"],
                      "g": ["ɣ", "Ɣ"], "n": ["ŋ", "Ŋ"], "o": ["ɔ", "Ɔ"],
                      "v": ["ʋ", "Ʋ"]},
    },
    "ga": {
        "name": "Ga",
        "note": "Gã",
        "special": ["ɛ", "Ɛ", "ŋ", "Ŋ", "ɔ", "Ɔ"],
        "longpress": {"e": ["ɛ", "Ɛ"], "n": ["ŋ", "Ŋ"], "o": ["ɔ", "Ɔ"]},
    },
    "dagbani": {
        "name": "Dagbani",
        "note": "Dagbanli",
        "special": ["ɛ", "Ɛ", "ɣ", "Ɣ", "ŋ", "Ŋ", "ɔ", "Ɔ", "ʒ", "Ʒ"],
        "longpress": {"e": ["ɛ", "Ɛ"], "g": ["ɣ", "Ɣ"], "n": ["ŋ", "Ŋ"],
                      "o": ["ɔ", "Ɔ"], "z": ["ʒ", "Ʒ"]},
    },
}

from .languages import OTHER_BY_CODE, OTHER_LANGUAGES   # noqa: E402,F401

# Characters a phone keyboard hides, across the Ghanaian orthographies. Used for
# every language that does not have its own set listed above: better to offer a
# few unneeded letters than to leave a speaker unable to type their own.
DEFAULT_SPECIAL = ["ɛ", "Ɛ", "ɔ", "Ɔ", "ŋ", "Ŋ", "ɖ", "Ɖ", "ƒ", "Ƒ",
                   "ɣ", "Ɣ", "ʋ", "Ʋ", "ʒ", "Ʒ"]
DEFAULT_LONGPRESS = {"d": ["ɖ", "Ɖ"], "e": ["ɛ", "Ɛ"], "f": ["ƒ", "Ƒ"],
                     "g": ["ɣ", "Ɣ"], "n": ["ŋ", "Ŋ"], "o": ["ɔ", "Ɔ"],
                     "v": ["ʋ", "Ʋ"], "z": ["ʒ", "Ʒ"]}

# Every language a volunteer can sign up for. The four above start with machine
# translations to check; the rest start empty, and the first speakers to arrive
# type the options everyone after them votes on.
ALL_LANGUAGES = dict(LANGUAGES)
for _code, _name, _alt in OTHER_LANGUAGES:
    ALL_LANGUAGES[_code] = {
        "name": _name,
        "note": _alt[0] if _alt else "",
        "special": DEFAULT_SPECIAL,
        "longpress": DEFAULT_LONGPRESS,
        "seeded": False,
    }
for _info in LANGUAGES.values():
    _info["seeded"] = True

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
             "Saturday", "Sunday"]

TIME_WINDOWS = {
    "morning": "Morning",
    "afternoon": "Afternoon",
    "evening": "Evening",
    "anytime": "Any time",
}


def _build_id():
    """Short commit of the running code, for /healthz.

    Read from the environment when set (Coolify passes the commit), otherwise
    from git, otherwise unknown.
    """
    env = os.environ.get("SOURCE_COMMIT") or os.environ.get("SHOLA_BUILD")
    if env:
        return env[:7]
    try:
        import subprocess
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              cwd=BASE_DIR, capture_output=True, text=True,
                              timeout=5).stdout.strip() or "unknown"
    except Exception:      # noqa: BLE001 - a missing git is not an error here
        return "unknown"


def _old_hosts(site_host):
    """Hostnames to redirect away from, never including the current one.

    A class body cannot read its own names from inside a comprehension, and
    filtering matters: redirecting a host to itself is an infinite loop, and
    SHOLA_SITE_URL and SHOLA_OLD_HOSTS get changed by hand at different
    moments during a move.
    """
    current = site_host.split(":")[0].lower()
    listed = os.environ.get("SHOLA_OLD_HOSTS", "shola.inkika.org").split(",")
    return [h for h in (x.strip().lower() for x in listed)
            if h and h != current]


class Config:
    SECRET_KEY = os.environ.get("SHOLA_SECRET_KEY", "dev-only-change-me")
    BUILD = _build_id()
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "SHOLA_DATABASE_URL", f"sqlite:///{INSTANCE_DIR / 'shola.db'}")
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    # How many words each volunteer commits to, and over how long.
    WORDS_PER_VOLUNTEER = int(os.environ.get("SHOLA_WORDS_PER_VOLUNTEER", 1000))
    COMMITMENT_DAYS = int(os.environ.get("SHOLA_COMMITMENT_DAYS", 365))

    # Share of volunteers expected to finish their full commitment. Recruitment
    # targets assume the rest contribute nothing, which is deliberately
    # pessimistic: it is better to over-recruit and finish early than to plan
    # off an optimistic number and miss the year.
    COMPLETION_RATE = float(os.environ.get("SHOLA_COMPLETION_RATE", 0.30))

    # Public base URL used in emails.
    SITE_URL = os.environ.get("SHOLA_SITE_URL", "http://localhost:5000")

    # The bare hostname, for copy and captions where a full URL would read
    # badly. Derived so a move needs only SHOLA_SITE_URL changed.
    SITE_HOST = SITE_URL.split("//", 1)[-1].rstrip("/")

    # Hostnames the site used to answer on. Requests arriving on one are
    # redirected to SITE_URL, so old links and printed media keep working.
    # Listed explicitly rather than redirecting anything that is not the
    # canonical host: Coolify's health check arrives with a container hostname,
    # and redirecting that would fail every deploy.
    # The canonical host is filtered out even if it is listed: redirecting a
    # host to itself is an infinite loop, and the two settings are changed by
    # hand at different moments during a move.
    OLD_HOSTS = _old_hosts(SITE_HOST)

    # Gmail SMTP. Use a Google app password, not the account password.
    SMTP_HOST = os.environ.get("SHOLA_SMTP_HOST", "smtp.gmail.com")
    SMTP_PORT = int(os.environ.get("SHOLA_SMTP_PORT", 465))
    SMTP_USER = os.environ.get("SHOLA_SMTP_USER", "")
    SMTP_PASSWORD = os.environ.get("SHOLA_SMTP_PASSWORD", "")
    MAIL_FROM_NAME = os.environ.get("SHOLA_MAIL_FROM_NAME", "SHOLA")

    UPLOAD_DIR = INSTANCE_DIR / "uploads"
    MAX_CONTENT_LENGTH = 6 * 1024 * 1024      # 6 MB photo ceiling
    ALLOWED_PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp"}
    PHOTO_SIZE = 320                          # square thumbnail edge, px

    # Daily link lifetime. Generous, because a volunteer may open an old email.
    LINK_MAX_AGE_DAYS = int(os.environ.get("SHOLA_LINK_MAX_AGE_DAYS", 45))
