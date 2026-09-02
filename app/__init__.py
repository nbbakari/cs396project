"""Application factory and shared extension instances for epaData."""

import os
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm import DeclarativeBase

# Project root (the directory that contains run.py).
BASE_DIR = Path(__file__).resolve().parent.parent

# Load environment variables from .env before any config is read.
load_dotenv(BASE_DIR / ".env")


class Base(DeclarativeBase):
    """Declarative base using SQLAlchemy 2.0 typed-mapping style."""


# Extensions are created unbound and attached to the app in create_app().
db = SQLAlchemy(model_class=Base)
migrate = Migrate()


def create_app(config_overrides: dict | None = None) -> Flask:
    """Build and configure a Flask application instance.

    Args:
        config_overrides: Optional mapping applied last, used by tests to swap
            in an in-memory database.
    """
    app = Flask(__name__, instance_relative_config=True)

    default_db_uri = f"sqlite:///{BASE_DIR / 'data' / 'epadata.sqlite3'}"
    app.config.from_mapping(
        SECRET_KEY=os.getenv("SECRET_KEY", "dev-secret-key"),
        SQLALCHEMY_DATABASE_URI=os.getenv("DATABASE_URL", default_db_uri),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )

    if config_overrides:
        app.config.update(config_overrides)

    db.init_app(app)
    migrate.init_app(app, db)

    # Imported for the side effect of registering the mappers, so that
    # `flask db migrate` can see every table.
    from app import models  # noqa: F401

    @app.route("/")
    def index():
        return {"application": "epaData", "status": "ok"}

    @app.shell_context_processor
    def shell_context():
        return {
            "db": db,
            "Dataset": models.Dataset,
            "Facility": models.Facility,
            "Unit": models.Unit,
            "AnnualRecord": models.AnnualRecord,
        }

    return app
