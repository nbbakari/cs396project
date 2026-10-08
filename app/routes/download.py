"""Download page and CSV / Excel export endpoints.

Every records download goes through the data-explorer query builder, so a
download link carries the same query string as the search it came from and
returns exactly the rows that search shows.
"""

from __future__ import annotations

from flask import Blueprint, abort, render_template, request, send_file
from sqlalchemy import func, select

from app import db
from app.models import Dataset, DataProvenance, DataQualityIssue, UploadedFile
from app.routes.upload import ISSUE_EXPORT_COLUMNS
from app.services.exports import FORMATS, requested_format, tabular_response
from app.services.ingest import upload_path
from app.services.search import (
    iter_search_rows,
    parse_search,
    row_values,
    filter_choices,
)

download_bp = Blueprint("download", __name__)

ISSUE_SEVERITIES = ("rejected", "warning")

PROVENANCE_COLUMNS = [
    "dataset_id",
    "dataset_name",
    "data_source",
    "source_type",
    "source_name",
    "source_url",
    "query_params",
    "original_filename",
    "file_sha256",
    "stored_copy",
    "retrieved_at",
    "reporting_years",
    "raw_records",
    "accepted_records",
    "rejected_records",
    "warning_records",
    "notes",
]


@download_bp.route("/downloads")
def downloads_page():
    """Choose what to download: datasets, searches, selections, reports."""
    datasets = db.session.execute(
        select(Dataset, func.count(DataQualityIssue.id))
        .outerjoin(DataQualityIssue, DataQualityIssue.dataset_id == Dataset.id)
        .group_by(Dataset.id)
        .order_by(Dataset.retrieval_date.desc())
    ).all()
    return render_template(
        "downloads.html",
        datasets=datasets,
        formats=FORMATS,
        choices=filter_choices(),
    )


@download_bp.route("/download/records")
def records():
    """Records matching any explorer search (all of them when unfiltered)."""
    search = parse_search(request.args)
    columns = search.export_columns
    stem = "epadata-search" if search.has_filters else "epadata-all-records"
    if search.dataset_ids and len(search.dataset_ids) == 1 and not search.text:
        stem = f"dataset-{search.dataset_ids[0]}-valid-records"
    return tabular_response(
        [header for _, header in columns],
        (row_values(row, columns) for row in iter_search_rows(search)),
        stem,
        requested_format(request.args.get("format")),
    )


@download_bp.route("/download/issues")
def issues():
    """Data-quality report: rejected and flagged rows, optionally for one dataset."""
    statement = select(DataQualityIssue).order_by(
        DataQualityIssue.dataset_id, DataQualityIssue.source_row, DataQualityIssue.id
    )
    stem = "data-quality-report"

    dataset_id = request.args.get("dataset", type=int)
    if dataset_id is not None:
        db.get_or_404(Dataset, dataset_id)
        statement = statement.where(DataQualityIssue.dataset_id == dataset_id)
        stem = f"dataset-{dataset_id}-quality-report"

    severity = request.args.get("severity")
    if severity in ISSUE_SEVERITIES:
        statement = statement.where(DataQualityIssue.severity == severity)
        stem += "-rejected" if severity == "rejected" else "-warnings"

    columns = [("dataset_id", "dataset_id"), *ISSUE_EXPORT_COLUMNS]
    rows = (
        [getattr(issue, key) for key, _ in columns]
        for issue in db.session.scalars(statement.execution_options(yield_per=2000))
    )
    return tabular_response(
        [header for _, header in columns],
        rows,
        stem,
        requested_format(request.args.get("format")),
    )


@download_bp.route("/download/provenance")
def provenance():
    """Where every dataset came from, how, and what happened to its rows."""
    rows = db.session.execute(
        select(Dataset, DataProvenance, UploadedFile)
        .outerjoin(DataProvenance, DataProvenance.dataset_id == Dataset.id)
        .outerjoin(UploadedFile, UploadedFile.dataset_id == Dataset.id)
        .order_by(Dataset.id)
    ).all()

    def values(dataset, prov, stored):
        return [
            dataset.id,
            dataset.dataset_name,
            dataset.data_source,
            prov.source_type if prov else None,
            prov.source_name if prov else None,
            prov.source_url if prov else None,
            prov.query_params if prov else None,
            dataset.original_filename,
            prov.file_sha256 if prov else None,
            stored.stored_filename if stored else None,
            dataset.retrieval_date,
            prov.reporting_years if prov else str(dataset.reporting_year),
            dataset.raw_records,
            dataset.accepted_records,
            prov.rejected_records if prov else None,
            prov.warning_records if prov else None,
            dataset.notes,
        ]

    return tabular_response(
        PROVENANCE_COLUMNS,
        (values(*row) for row in rows),
        "data-provenance",
        requested_format(request.args.get("format")),
    )


@download_bp.route("/download/dataset/<int:dataset_id>/original")
def original_file(dataset_id: int):
    """The source file exactly as uploaded or retrieved."""
    dataset = db.get_or_404(Dataset, dataset_id)
    stored = dataset.uploaded_file
    if stored is None or not upload_path(stored.stored_filename).exists():
        abort(404, description="No original file was kept for this dataset.")
    return send_file(
        upload_path(stored.stored_filename),
        as_attachment=True,
        download_name=stored.original_filename,
    )
