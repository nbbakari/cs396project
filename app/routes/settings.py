"""Settings: the CAMPD and Gemini API keys and the team's shared data.

Each API key is entered in the browser once, checked with its provider, and
saved in this machine's .env file (CAMPD_API_KEY, GEMINI_API_KEY) -- the
server-side environment the app reads it from. A key is never sent back to
the browser; pages show only its last four characters.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from dotenv import set_key, unset_key
from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app import db
from app.models import AnnualRecord, Dataset
from app.services.assistant import KEY_SIGNUP_URL as GEMINI_KEY_SIGNUP_URL
from app.services.assistant import AssistantError, is_installed, verify_key
from app.services.campd import API_KEY_SIGNUP_URL, CampdError, verify_api_key
from app.services.shared import SharedDataError, local_has_data, pull_shared

settings_bp = Blueprint("settings", __name__)

# api.data.gov keys (which CAMPD uses) are 40 letters and digits; allow some slack.
_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
# Gemini keys are about 39 characters; allow other lengths.
_GEMINI_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{20,200}$")


def _masked(config_name: str) -> str | None:
    key = current_app.config.get(config_name)
    return f"••••{key[-4:]}" if key else None


def masked_api_key() -> str | None:
    return _masked("CAMPD_API_KEY")


def _save_env(name: str, value: str | None) -> Path:
    """Write (or with None, remove) one setting in .env and the running app."""
    env_file = Path(current_app.config["ENV_FILE"])
    if value is None:
        if env_file.exists():
            unset_key(env_file, name, quote_mode="never")
        os.environ.pop(name, None)
    else:
        set_key(env_file, name, value, quote_mode="never")
        os.environ[name] = value
    current_app.config[name] = value
    return env_file


def _next_url() -> str:
    """Where to go after a form post: a local path only, never another site."""
    target = request.form.get("next") or ""
    if target.startswith("/") and not target.startswith("//"):
        return target
    return url_for("settings.settings_page")


@settings_bp.app_context_processor
def inject_api_key_status():
    return {
        "api_key_masked": masked_api_key(),
        "api_key_signup_url": API_KEY_SIGNUP_URL,
        "gemini_key_masked": _masked("GEMINI_API_KEY"),
        "gemini_installed": is_installed(),
        "gemini_key_signup_url": GEMINI_KEY_SIGNUP_URL,
    }


@settings_bp.route("/settings")
def settings_page():
    uri = current_app.config["SQLALCHEMY_DATABASE_URI"]
    return render_template(
        "settings.html",
        local_database=uri.removeprefix("sqlite:///") if uri.startswith("sqlite:///") else uri,
        shared_configured=bool(current_app.config.get("SHARED_DATABASE_URL")),
        local_has_data=local_has_data(),
        dataset_count=db.session.scalar(select(func.count(Dataset.id))) or 0,
        record_count=db.session.scalar(select(func.count(AnnualRecord.id))) or 0,
        upload_folder=current_app.config["UPLOAD_FOLDER"],
    )


@settings_bp.route("/settings/api-key", methods=["POST"])
def save_api_key():
    key = (request.form.get("api_key") or "").strip()
    next_url = _next_url()
    if not _KEY_PATTERN.match(key):
        flash(
            "That doesn't look like a CAMPD API key. Paste the whole key from the "
            "EPA's confirmation email (letters and digits, no spaces).",
            "danger",
        )
        return redirect(next_url)

    try:
        verify_api_key(key)
    except CampdError as exc:
        if exc.status in (401, 403):
            flash(
                "The EPA rejected that key. Check that it was copied completely "
                "and that you used the key from the confirmation email.",
                "danger",
            )
            return redirect(next_url)
        flash(f"The key could not be checked with the EPA right now ({exc}); it was saved anyway.", "warning")
    else:
        flash("The EPA accepted your API key.", "success")

    env_file = Path(current_app.config["ENV_FILE"])
    set_key(env_file, "CAMPD_API_KEY", key, quote_mode="never")
    os.environ["CAMPD_API_KEY"] = key
    current_app.config["CAMPD_API_KEY"] = key
    flash(
        f"Saved as CAMPD_API_KEY in {env_file.name} on this computer. You can now "
        "retrieve data from CAMPD.",
        "success",
    )
    return redirect(next_url)


@settings_bp.route("/settings/api-key/remove", methods=["POST"])
def remove_api_key():
    env_file = Path(current_app.config["ENV_FILE"])
    if env_file.exists():
        unset_key(env_file, "CAMPD_API_KEY", quote_mode="never")
    os.environ.pop("CAMPD_API_KEY", None)
    current_app.config["CAMPD_API_KEY"] = None
    flash("The API key was removed from this computer.", "info")
    return redirect(url_for("settings.settings_page"))


@settings_bp.route("/settings/gemini-key", methods=["POST"])
def save_gemini_key():
    """Check a Gemini API key with Google and save it for the AI assistant."""
    key = (request.form.get("gemini_key") or "").strip()
    target = url_for("settings.settings_page", _anchor="gemini-key")
    if not _GEMINI_KEY_PATTERN.match(key):
        flash("That doesn't look like a Gemini API key. Paste the whole key from Google AI Studio.",
              "danger")
        return redirect(target)
    try:
        verify_key(key)
    except AssistantError as exc:
        if exc.status == 401:
            flash("Google rejected that key. Check that it was copied completely.", "danger")
            return redirect(target)
        flash(f"The key could not be checked with Google right now ({exc}); it was saved anyway.",
              "warning")
    else:
        flash("Google accepted your Gemini API key.", "success")
    env_file = _save_env("GEMINI_API_KEY", key)
    flash(f"Saved as GEMINI_API_KEY in {env_file.name} on this computer. "
          "The Ask AI panel is ready.", "success")
    return redirect(target)


@settings_bp.route("/settings/gemini-key/remove", methods=["POST"])
def remove_gemini_key():
    _save_env("GEMINI_API_KEY", None)
    flash("The Gemini API key was removed from this computer.", "info")
    return redirect(url_for("settings.settings_page", _anchor="gemini-key"))


@settings_bp.route("/settings/shared-data/load", methods=["POST"])
def load_shared_data():
    """Seed this computer's empty database from the team's shared database."""
    url = current_app.config.get("SHARED_DATABASE_URL")
    if not url:
        flash(
            "No shared database is configured. Add SHARED_DATABASE_URL to .env "
            "(ask the team for the connection string) and restart the app.",
            "warning",
        )
        return redirect(url_for("settings.settings_page"))

    try:
        counts = pull_shared(url)
    except SharedDataError as exc:
        flash(str(exc), "warning")
        return redirect(url_for("settings.settings_page"))
    except SQLAlchemyError as exc:
        current_app.logger.exception("Loading shared data failed")
        detail = str(getattr(exc, "orig", exc)).strip().splitlines()[0][:200]
        flash(f"Could not load the shared data: {detail}", "danger")
        return redirect(url_for("settings.settings_page"))

    flash(
        f"Loaded {counts.get('annual_records', 0):,} annual records in "
        f"{counts.get('datasets', 0)} datasets from the shared database. They are now "
        "stored on this computer.",
        "success",
    )
    return redirect(url_for("main.index"))
