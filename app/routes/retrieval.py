"""EPA CAMPD API retrieval page.

A retrieval fetches the selected unit-years from the CAMPD API on the server,
keeps the response as a CSV file, and stages it for the same review-and-approve
step as an uploaded file. The query parameters travel with the staged file
into the dataset's provenance record.
"""

from __future__ import annotations

from datetime import date

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from sqlalchemy import select

from app import db
from app.models import Dataset, DataProvenance, UploadedFile
from app.services.campd import (
    CONTROL_TECHNOLOGIES,
    FUEL_TYPES,
    STATES,
    UNIT_TYPES,
    CampdError,
    build_query,
    retrieve_annual,
)
from app.services.ingest import stage_bytes
from app.services.search import filter_choices
from app.services.validation import FIRST_REPORTING_YEAR

retrieval_bp = Blueprint("retrieval", __name__)


def _merged(*lists: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for values in lists:
        for value in values:
            seen.setdefault(value, None)
    return sorted(seen, key=str.lower)


def _render_form(form: dict[str, str], status: int = 200):
    choices = filter_choices()
    history = db.session.execute(
        select(Dataset, DataProvenance)
        .join(DataProvenance, DataProvenance.dataset_id == Dataset.id)
        .where(DataProvenance.source_type == "api")
        .order_by(Dataset.retrieval_date.desc())
        .limit(25)
    ).all()
    pending = db.session.scalars(
        select(UploadedFile)
        .where(UploadedFile.status == "staged", UploadedFile.source_type == "api")
        .order_by(UploadedFile.uploaded_at.desc())
    ).all()
    return (
        render_template(
            "retrieval.html",
            form=form,
            states=STATES,
            fuel_types=_merged(FUEL_TYPES, choices["primary_fuels"]),
            unit_types=_merged(UNIT_TYPES, choices["unit_types"]),
            controls=_merged(
                CONTROL_TECHNOLOGIES,
                choices["so2_controls"],
                choices["nox_controls"],
                choices["pm_controls"],
            ),
            first_year=FIRST_REPORTING_YEAR,
            last_year=date.today().year,
            history=history,
            pending=pending,
            api_key_configured=bool(current_app.config.get("CAMPD_API_KEY")),
        ),
        status,
    )


@retrieval_bp.route("/retrieve", methods=["GET"])
def retrieval_form():
    return _render_form({"year": str(date.today().year - 1), "attributes": "on"})


@retrieval_bp.route("/retrieve", methods=["POST"])
def run_retrieval():
    form = {key: (request.form.get(key) or "").strip() for key in request.form}
    errors = []

    try:
        year = int(form.get("year", ""))
        if not FIRST_REPORTING_YEAR <= year <= date.today().year:
            raise ValueError
    except ValueError:
        errors.append(
            f"Choose a reporting year between {FIRST_REPORTING_YEAR} and {date.today().year}."
        )
        year = None

    state = form.get("state", "").upper()
    if state and state not in {code for code, _ in STATES}:
        errors.append(f"'{state}' is not a state code CAMPD recognises.")

    facility_ids = []
    for part in form.get("facility_ids", "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if part.isdigit():
            facility_ids.append(int(part))
        else:
            errors.append(f"Facility ID '{part}' is not a number.")

    if errors:
        for message in errors:
            flash(message, "danger")
        return _render_form(form, 400)

    query = build_query(
        year,
        state=state or None,
        facility_ids=facility_ids or None,
        fuel_type=form.get("fuel_type") or None,
        unit_type=form.get("unit_type") or None,
        control_technology=form.get("control_technology") or None,
    )

    try:
        retrieval = retrieve_annual(query, include_attributes=bool(form.get("attributes")))
    except CampdError as exc:
        current_app.logger.warning("CAMPD retrieval failed: %s", exc)
        flash(f"Retrieval failed: {exc}", "danger")
        return _render_form(form, 502)

    if retrieval.frame.empty:
        flash(
            "The CAMPD API returned no records for those filters. Check the "
            "spelling of fuel, unit type or control, or widen the search.",
            "warning",
        )
        return _render_form(form)

    label_parts = [str(year)] + [
        value for value in (state, form.get("fuel_type"), form.get("unit_type")) if value
    ]
    default_name = "CAMPD API " + " / ".join(label_parts)
    staged = stage_bytes(
        retrieval.frame.to_csv(index=False).encode("utf-8"),
        original_filename=f"campd-annual-emissions-{'-'.join(p.replace(' ', '_') for p in label_parts)}.csv",
        extension="csv",
        source_type="api",
        dataset_name=form.get("dataset_name") or default_name,
        source_url=retrieval.source_url,
        query_params=retrieval.query_params,
    )

    detail = (
        f" and {retrieval.attribute_rows:,} facility-attribute rows"
        if retrieval.attribute_rows
        else ""
    )
    flash(
        f"Retrieved {retrieval.emission_rows:,} unit-year record(s){detail} from CAMPD "
        f"in {retrieval.elapsed_seconds:.1f} s. Review them below, then approve the import.",
        "success",
    )
    for warning in retrieval.warnings:
        flash(warning, "warning")
    return redirect(url_for("upload.review", file_id=staged.id))
