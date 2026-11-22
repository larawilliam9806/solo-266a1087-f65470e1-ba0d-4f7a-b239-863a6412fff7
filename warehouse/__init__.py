"""Flask application factory."""

from __future__ import annotations

import os
from pathlib import Path

from flask import Flask

from . import db as db_module


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=False)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-change-me"),
        DATABASE=os.environ.get(
            "DATABASE", str(Path(app.root_path).parent / "data" / "warehouse.db")
        ),
        MAX_CONTENT_LENGTH=8 * 1024 * 1024,
        AUTO_INIT_DB=True,
    )
    if test_config:
        app.config.update(test_config)

    db_module.init_app(app)

    from .views import bp

    app.register_blueprint(bp)

    from .cli import register_cli

    register_cli(app)
    return app
