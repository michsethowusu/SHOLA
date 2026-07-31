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
