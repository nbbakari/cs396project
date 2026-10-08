"""Application factory and shared extension instances for epaData."""

import os
import sqlite3
import threading
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase

# Project root (the directory that contains run.py).
BASE_DIR = Path(__file__).resolve().parent.parent

# Default SQLite database file, used whenever DATABASE_URL is not set.
DEFAULT_DATABASE_PATH = BASE_DIR / "data" / "epadata.db"

# Load environment variables from .env before any config is read.
load_dotenv(BASE_DIR / ".env")


class Base(DeclarativeBase):
    """Declarative base using SQLAlchemy 2.0 typed-mapping style."""


# Extensions are created unbound and attached to the app in create_app().
db = SQLAlchemy(model_class=Base)
migrate = Migrate()


def _database_urls() -> tuple[str, str | None]:
    """The local database URL and the shared (team) database URL.

    The app always stores its data in SQLite, the project database. Older
    .env files point DATABASE_URL at the team's Supabase database; such a
    URL is used as the shared source to load from, not as the app database.
    """
    url = os.getenv("DATABASE_URL")
    shared = os.getenv("SHARED_DATABASE_URL")
    if url and not url.startswith("sqlite"):
        shared = shared or url
        url = None
    return url or f"sqlite:///{DEFAULT_DATABASE_PATH}", shared


def _api_key_from_env() -> str | None:
    """CAMPD_API_KEY, ignoring the placeholder that older .env.example files set."""
    key = (os.getenv("CAMPD_API_KEY") or "").strip()
    return None if key in ("", "your-campd-api-key-here") else key


@event.listens_for(Engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection, connection_record):  # noqa: ARG001
    """Turn on foreign-key enforcement for every SQLite connection.

    SQLite ships with it off, so without this the REFERENCES and ON DELETE
    CASCADE clauses in the schema would be decorative.
    """
    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def create_app(config_overrides: dict | None = None) -> Flask:
    """Build and configure a Flask application instance.

    Args:
        config_overrides: Optional mapping applied last, used by tests to swap
            in an in-memory database and a temporary upload folder.
    """
    app = Flask(__name__, instance_relative_config=True)
    database_url, shared_database_url = _database_urls()

    app.config.from_mapping(
        SECRET_KEY=os.getenv("SECRET_KEY", "dev-secret-key"),
        # Each copy of the app keeps its data in its own SQLite file.
        SQLALCHEMY_DATABASE_URI=database_url,
        # The team's shared database (Supabase), used only to seed an empty
        # local database and to publish data to the team.
        SHARED_DATABASE_URL=shared_database_url,
        # Apply pending migrations to the local SQLite file on the first
        # request, so a fresh checkout works without `flask db upgrade`.
        AUTO_MIGRATE=True,
        # Where the Settings page saves the CAMPD API key.
        ENV_FILE=str(BASE_DIR / ".env"),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        # Verify pooled connections before use, so a connection dropped while
        # idle surfaces as a reconnect rather than an error.
        SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True},
        # Reject oversized uploads before they are buffered; Werkzeug raises
        # 413, which the upload blueprint turns into a flash message.
        MAX_CONTENT_LENGTH=int(os.getenv("MAX_UPLOAD_MB", "32")) * 1024 * 1024,
        # Original files and API responses are kept here for provenance.
        UPLOAD_FOLDER=os.getenv("UPLOAD_FOLDER", str(BASE_DIR / "data" / "uploads")),
        # EPA Clean Air Markets Program Data API credentials. The key is read
        # only on the server and sent only in a request header to the EPA.
        CAMPD_API_KEY=_api_key_from_env(),
        CAMPD_API_BASE_URL=os.getenv(
            "CAMPD_API_BASE_URL", "https://api.epa.gov/easey"
        ),
    )

    if config_overrides:
        app.config.update(config_overrides)

    Path(app.config["UPLOAD_FOLDER"]).mkdir(parents=True, exist_ok=True)
    if app.config["SQLALCHEMY_DATABASE_URI"] == f"sqlite:///{DEFAULT_DATABASE_PATH}":
        DEFAULT_DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)

    db.init_app(app)
    migrate.init_app(app, db)

    uri = app.config["SQLALCHEMY_DATABASE_URI"]
    if app.config["AUTO_MIGRATE"] and uri.startswith("sqlite:///") and not app.testing:
        _migrate_on_first_request(app)

    # Imported for the side effect of registering the mappers, so that
    # `flask db migrate` can see every table.
    from app import models  # noqa: F401

    from app.routes.api import api_bp
    from app.routes.download import download_bp
    from app.routes.main import main_bp
    from app.routes.retrieval import retrieval_bp
    from app.routes.settings import settings_bp
    from app.routes.upload import upload_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(upload_bp)
    app.register_blueprint(retrieval_bp)
    app.register_blueprint(download_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(api_bp)

    from app.cli import register_cli
    from app.templating import register_template_helpers

    register_cli(app)
    register_template_helpers(app)

    @app.context_processor
    def inject_sidebar_status():
        """Lightweight status shown in the sidebar/topbar on every page.

        Runs one cheap COUNT per request. A high-traffic app would cache
        this, but that's not worth the complexity for a low-traffic class
        project -- if it ever becomes one, this is the first thing to cache.
        """
        from sqlalchemy import func, select

        try:
            record_count = db.session.execute(
                select(func.count(models.AnnualRecord.id))
            ).scalar() or 0
            db_status = "ok"
        except Exception:  # noqa: BLE001 - any DB failure just degrades the badge
            db.session.rollback()
            record_count = None
            db_status = "down"
        return {"sidebar_record_count": record_count, "sidebar_db_status": db_status}

    @app.shell_context_processor
    def shell_context():
        return {
            "db": db,
            "Dataset": models.Dataset,
            "Facility": models.Facility,
            "Unit": models.Unit,
            "AnnualRecord": models.AnnualRecord,
            "UploadedFile": models.UploadedFile,
            "DataProvenance": models.DataProvenance,
            "DataQualityIssue": models.DataQualityIssue,
        }

    return app


def _migrate_on_first_request(app: Flask) -> None:
    """Run `flask db upgrade` once, before the first request is handled."""
    lock = threading.Lock()
    state = {"done": False}

    @app.before_request
    def apply_pending_migrations():
        if state["done"]:
            return
        with lock:
            if state["done"]:
                return
            from flask_migrate import upgrade

            upgrade(directory=str(BASE_DIR / "migrations"))
            state["done"] = True
