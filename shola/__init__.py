"""SHOLA - Share Your Language."""

import os

from flask import Flask

from .config import (ALL_LANGUAGES, DAY_NAMES, INSTANCE_DIR, LANGUAGES,
                     OTHER_LANGUAGES, TIME_WINDOWS, Config)
from .models import (adopt_orphan_items, db, ensure_columns,
                     ensure_indexes)
from .tiers import VOTES_TO_SETTLE


def create_app(config_object=Config):
    app = Flask(__name__, instance_path=str(INSTANCE_DIR),
                instance_relative_config=True)
    app.config.from_object(config_object)
    app.config["LANGUAGES"] = LANGUAGES
    app.config["DAY_NAMES"] = DAY_NAMES
    app.config["TIME_WINDOWS"] = TIME_WINDOWS
    app.config["OTHER_LANGUAGES"] = OTHER_LANGUAGES
    app.config["ALL_LANGUAGES"] = ALL_LANGUAGES

    os.makedirs(app.config["UPLOAD_DIR"], exist_ok=True)

    db.init_app(app)

    from .views import main
    app.register_blueprint(main)

    from .admin import admin
    app.register_blueprint(admin)

    @app.before_request
    def canonical_host():
        """Send a retired hostname to the one the site answers on now.

        301 rather than 302: the old address is not coming back, and search
        engines and browsers should stop asking for it.
        """
        from flask import redirect, request
        host = (request.host or "").split(":")[0].lower()
        if host and host in app.config["OLD_HOSTS"]:
            target = (app.config["SITE_URL"].rstrip("/")
                      + request.full_path.rstrip("?"))
            return redirect(target, code=301)

    from .cli import shola_cli
    app.cli.add_command(shola_cli)

    with app.app_context():
        # SQLite defaults to a single writer that blocks readers. With several
        # gunicorn workers that surfaces as "database is locked" the moment a
        # verdict lands while someone else is reading. WAL lets readers carry
        # on during a write, and a busy timeout makes concurrent writers wait
        # their turn rather than fail outright.
        if app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite"):
            from sqlalchemy import event

            @event.listens_for(db.engine, "connect")
            def _sqlite_pragmas(dbapi_connection, _record):   # noqa: ANN001
                cur = dbapi_connection.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA busy_timeout=10000")
                cur.execute("PRAGMA synchronous=NORMAL")
                cur.close()

            with db.engine.connect() as conn:
                conn.exec_driver_sql("PRAGMA journal_mode=WAL")

        db.create_all()
        added = ensure_columns()
        if added:
            app.logger.info("added columns: %s", ", ".join(added))
        reindexed = ensure_indexes()
        if reindexed:
            app.logger.info("reindexed: %s", ", ".join(reindexed))
        # Everything collected before projects existed becomes a project, so no
        # code downstream needs a special case for "the original words".
        _core, adopted, joined_up = adopt_orphan_items()
        if adopted or joined_up:
            app.logger.info("adopted %s items and %s volunteers into %s",
                            adopted, joined_up, _core.slug)

    @app.context_processor
    def inject_globals():
        return {"LANGUAGES": LANGUAGES, "DAY_NAMES": DAY_NAMES,
                "TIME_WINDOWS": TIME_WINDOWS, "OTHER_LANGUAGES": OTHER_LANGUAGES,
                "ALL_LANGUAGES": ALL_LANGUAGES,
                "LANGUAGE_COUNT": len(ALL_LANGUAGES),
                "VOTES_TO_SETTLE": VOTES_TO_SETTLE,
                "WORDS_PER_DAY": app.config["WORDS_PER_DAY"]}

    @app.url_defaults
    def version_static(endpoint, values):
        """Stamp every static URL with the file's modification time.

        Without this, a volunteer who has visited before keeps whatever CSS and
        JS their browser cached. A stale stylesheet is not a cosmetic problem
        here: the last release changed which elements accept clicks, so an old
        file leaves the options looking fine and doing nothing.
        """
        if endpoint != "static" or "filename" not in values:
            return
        path = os.path.join(app.static_folder, values["filename"])
        try:
            values["v"] = int(os.stat(path).st_mtime)
        except OSError:
            pass

    @app.template_filter("thousands")
    def thousands(n):
        return f"{n:,}"

    return app
