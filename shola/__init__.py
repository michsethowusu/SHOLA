"""SHOLA - Share Your Language."""

import os

from flask import Flask

from .config import DAY_NAMES, INSTANCE_DIR, LANGUAGES, TIME_WINDOWS, Config
from .models import db


def create_app(config_object=Config):
    app = Flask(__name__, instance_path=str(INSTANCE_DIR),
                instance_relative_config=True)
    app.config.from_object(config_object)
    app.config["LANGUAGES"] = LANGUAGES
    app.config["DAY_NAMES"] = DAY_NAMES
    app.config["TIME_WINDOWS"] = TIME_WINDOWS

    os.makedirs(app.config["UPLOAD_DIR"], exist_ok=True)

    db.init_app(app)

    from .views import main
    app.register_blueprint(main)

    from .cli import shola_cli
    app.cli.add_command(shola_cli)

    with app.app_context():
        db.create_all()

    @app.context_processor
    def inject_globals():
        return {"LANGUAGES": LANGUAGES, "DAY_NAMES": DAY_NAMES,
                "TIME_WINDOWS": TIME_WINDOWS}

    @app.template_filter("thousands")
    def thousands(n):
        return f"{n:,}"

    return app
