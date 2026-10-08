"""File upload, review and approval.

Uploading only *stages* a file: it is kept as received and validated, and the
review page shows the column mapping, every rejected or flagged row, and a
preview of what will be imported. Nothing reaches the emissions tables until
the user approves. CAMPD API retrievals are staged the same way and share the
review page (see app/routes/retrieval.py).
"""

from __future__ import annotations

import io
from typing import Any

import pandas as pd
from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.utils import secure_filename

from app import db
from app.models import UploadedFile
from app.services.exports import requested_format, tabular_response
from app.services.ingest import (
    discard_staged,
    earlier_import_of,
    import_staged,
    query_params_of,
    stage_bytes,
    validate_staged,
)
from app.services.validation import (
    ALLOWED_EXTENSIONS,
    COLUMN_ALIASES,
    FIELD_LABELS,
    REJECTED,
    REQUIRED_FIELDS,
    read_table,
)

upload_bp = Blueprint("upload", __name__)

ISSUE_PREVIEW_LIMIT = 200
ROW_PREVIEW_LIMIT = 25

ISSUE_EXPORT_COLUMNS = [
    ("source_row", "source_row"),
    ("severity", "severity"),
    ("issue_type", "issue_type"),
    ("field", "field"),
    ("facility_id", "facility_id"),
    ("epa_unit_id", "unit_id"),
    ("reporting_year", "reporting_year"),
    ("message", "message"),
    ("raw_values", "original_row_json"),
]

PREVIEW_COLUMNS = [
    ("source_row", "Row"),
    ("facility_id", "Facility ID"),
    ("facility_name", "Facility"),
    ("state_code", "State"),
    ("epa_unit_id", "Unit"),
    ("reporting_year", "Year"),
    ("unit_type", "Unit type"),
    ("primary_fuel", "Fuel"),
    ("operating_time", "Op. hours"),
    ("gross_load", "Gross load (MWh)"),
    ("heat_input", "Heat input (MMBtu)"),
    ("co2_mass", "CO2 (tons)"),
    ("so2_mass", "SO2 (tons)"),
    ("nox_mass", "NOx (tons)"),
]


def _staged_or_redirect(file_id: int) -> UploadedFile | Any:
    staged = db.get_or_404(UploadedFile, file_id)
    if staged.status == "imported" and staged.dataset_id:
        flash("That file has already been imported.", "info")
        return redirect(url_for("main.dataset_detail", dataset_id=staged.dataset_id))
    if staged.status != "staged":
        flash("That file was discarded and can no longer be imported.", "warning")
        return redirect(url_for("upload.upload_form"))
    return staged


def _start_page(staged: UploadedFile) -> str:
    if staged.source_type == "api":
        return url_for("retrieval.retrieval_form")
    return url_for("upload.upload_form")


@upload_bp.route("/upload", methods=["GET"])
def upload_form():
    """Render the upload form, plus any staged files still awaiting review."""
    pending = db.session.scalars(
        select(UploadedFile)
        .where(UploadedFile.status == "staged")
        .order_by(UploadedFile.uploaded_at.desc())
    ).all()
    return render_template(
        "upload.html",
        pending=pending,
        max_mb=(current_app.config.get("MAX_CONTENT_LENGTH") or 0) // (1024 * 1024),
        required_fields=[FIELD_LABELS[f] for f in REQUIRED_FIELDS],
        optional_fields=[FIELD_LABELS[f] for f in COLUMN_ALIASES if f not in REQUIRED_FIELDS],
    )


@upload_bp.route("/upload", methods=["POST"])
def upload_file():
    """Check, keep and stage an uploaded file, then send the user to review it."""
    uploaded = request.files.get("data_file")
    if uploaded is None or not uploaded.filename:
        flash("Choose a .csv or .xlsx file to upload.", "warning")
        return redirect(url_for("upload.upload_form"))

    filename = secure_filename(uploaded.filename)
    if not filename:
        flash("That filename could not be processed. Rename the file and retry.", "danger")
        return redirect(url_for("upload.upload_form"))

    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if extension not in ALLOWED_EXTENSIONS:
        flash(
            f"Unsupported file type '.{extension}'. Upload a .csv or .xlsx file.",
            "danger",
        )
        return redirect(url_for("upload.upload_form"))

    data = uploaded.read()
    if not data:
        flash(f"{filename} is empty.", "warning")
        return redirect(url_for("upload.upload_form"))

    # pandas raises a wide range of exception types on malformed input
    # (ParserError, ValueError, UnicodeDecodeError, zipfile.BadZipFile from
    # openpyxl), so the parse is guarded broadly and logged with a traceback.
    try:
        frame = read_table(io.BytesIO(data), extension)
    except Exception as exc:  # noqa: BLE001 - arbitrary user-supplied file
        current_app.logger.exception("Could not parse upload %s", filename)
        flash(f"Could not read {filename}: {exc}", "danger")
        return redirect(url_for("upload.upload_form"))

    if frame.empty:
        flash(f"{filename} contains no data rows.", "warning")
        return redirect(url_for("upload.upload_form"))

    staged = stage_bytes(
        data,
        original_filename=filename,
        extension=extension,
        source_type="upload",
        dataset_name=(request.form.get("dataset_name") or "").strip() or filename,
    )
    return redirect(url_for("upload.review", file_id=staged.id))


@upload_bp.route("/review/<int:file_id>", methods=["GET"])
def review(file_id: int):
    """Validation report and preview for a staged file; nothing is saved yet."""
    staged = _staged_or_redirect(file_id)
    if not isinstance(staged, UploadedFile):
        return staged

    try:
        result = validate_staged(staged)
    except Exception as exc:  # noqa: BLE001 - the file was readable when staged
        current_app.logger.exception("Could not validate staged file %s", staged.id)
        flash(f"Could not read the staged file: {exc}", "danger")
        return redirect(_start_page(staged))

    issues = sorted(
        result.issues, key=lambda i: (i["severity"] != REJECTED, i["source_row"])
    )
    # pd.NA cannot be tested for truth in a template, so blanks become None.
    preview = [
        {key: (None if pd.isna(value) else value) for key, value in row.items()}
        for row in result.clean.head(ROW_PREVIEW_LIMIT).to_dict(orient="records")
    ]
    return render_template(
        "review.html",
        staged=staged,
        result=result,
        issues=issues[:ISSUE_PREVIEW_LIMIT],
        issue_total=len(issues),
        preview=preview,
        preview_columns=PREVIEW_COLUMNS,
        earlier=earlier_import_of(staged),
        query_params=query_params_of(staged),
    )


@upload_bp.route("/review/<int:file_id>/report", methods=["GET"])
def review_report(file_id: int):
    """Download the validation report for a staged file, before approving it."""
    staged = db.get_or_404(UploadedFile, file_id)
    if staged.status != "staged":
        abort(404)
    result = validate_staged(staged)
    issues = sorted(result.issues, key=lambda i: (i["source_row"], i["severity"]))
    return tabular_response(
        [header for _, header in ISSUE_EXPORT_COLUMNS],
        ([issue.get(key) for key, _ in ISSUE_EXPORT_COLUMNS] for issue in issues),
        f"validation-report-{staged.id}",
        requested_format(request.args.get("format")),
    )


@upload_bp.route("/review/<int:file_id>/confirm", methods=["POST"])
def confirm_import(file_id: int):
    """Approve a staged file: write its valid rows and its quality report."""
    staged = _staged_or_redirect(file_id)
    if not isinstance(staged, UploadedFile):
        return staged

    dataset_name = (request.form.get("dataset_name") or "").strip() or None
    try:
        dataset, result = import_staged(staged, dataset_name=dataset_name)
    except ValueError as exc:
        db.session.rollback()
        flash(f"Nothing was imported: {exc}", "warning")
        return redirect(url_for("upload.review", file_id=staged.id))
    except SQLAlchemyError as exc:
        db.session.rollback()
        current_app.logger.exception("Database error importing staged file %s", staged.id)
        flash(f"Database error while importing: {exc}", "danger")
        return redirect(url_for("upload.review", file_id=staged.id))

    stats = result.stats
    flash(
        f"Imported {dataset.accepted_records:,} record(s) from {stats['raw_records']:,} "
        f"row(s) as dataset #{dataset.id}.",
        "success",
    )
    if stats["rejected_records"] or stats["warning_records"]:
        flash(
            f"{stats['rejected_records']:,} row(s) were rejected and "
            f"{stats['warning_records']:,} imported with warnings; every one is listed "
            "in the data-quality report below.",
            "warning",
        )
    return redirect(url_for("main.dataset_detail", dataset_id=dataset.id))


@upload_bp.route("/review/<int:file_id>/cancel", methods=["POST"])
def cancel_import(file_id: int):
    """Discard a staged file without importing anything."""
    staged = _staged_or_redirect(file_id)
    if not isinstance(staged, UploadedFile):
        return staged
    discard_staged(staged)
    flash(f"Discarded {staged.original_filename}; nothing was imported.", "info")
    return redirect(_start_page(staged))


@upload_bp.app_errorhandler(413)
def handle_oversized_upload(error):  # noqa: ARG001 - Flask passes the exception
    """Turn Werkzeug's 413 into a flash rather than a bare error page."""
    limit = current_app.config.get("MAX_CONTENT_LENGTH") or 0
    flash(f"That file is larger than the {limit // (1024 * 1024)} MB upload limit.", "danger")
    return redirect(url_for("upload.upload_form"))
