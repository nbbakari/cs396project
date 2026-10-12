"""JSON endpoints backing the dashboard's charts and data explorer.

Views here return JSON only -- no templates -- so the front end can fetch them
directly. Every response is a JSON document, including errors, so a failed
request never hands the browser an HTML error page to parse.
"""

from __future__ import annotations

from typing import Optional

from flask import Blueprint, current_app, jsonify, request, url_for
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app import db
from app.models import AnnualRecord, Facility, Unit
from app.services.describe import MAX_LENGTH, interpret
from app.services.search import parse_search, run_search

api_bp = Blueprint("api", __name__, url_prefix="/api")

DEFAULT_LIMIT = 10
MAX_LIMIT = 100

def _int_arg(name: str, default: int) -> int:
    """Read an integer query-string argument, falling back on any bad input."""
    raw = request.args.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _requested_limit() -> int:
    """Read ?limit= from the query string, falling back to the default."""
    return max(1, min(_int_arg("limit", DEFAULT_LIMIT), MAX_LIMIT))


def _latest_reporting_year() -> Optional[int]:
    """The most recent year with any annual record, or None if none exist."""
    return db.session.execute(
        select(func.max(AnnualRecord.reporting_year))
    ).scalar()


@api_bp.route("/emissions/top-facilities", methods=["GET"])
def top_facilities():
    """Return the facilities with the highest total CO2 mass for one year.

    Query parameters:
        year:  reporting year to report on. Defaults to the most recent year
               present in the database -- totals are never blended across
               years, which would compare a facility's two-year total against
               another's single year.
        limit: how many facilities to return (default 10, capped at 100).
    """
    raw_year = (request.args.get("year") or "").strip()
    year: Optional[int]

    if raw_year:
        try:
            year = int(raw_year)
        except ValueError:
            return (
                jsonify(
                    {
                        "error": f"Invalid year '{raw_year}'.",
                        "detail": "Expected a four-digit year, for example 2023.",
                    }
                ),
                400,
            )
    else:
        try:
            year = _latest_reporting_year()
        except SQLAlchemyError as exc:
            db.session.rollback()
            current_app.logger.exception("latest-year lookup failed")
            return jsonify({"error": "Database query failed", "detail": str(exc)}), 500

        if year is None:
            return jsonify([])

    total_co2 = func.sum(AnnualRecord.co2_mass).label("total_co2")

    statement = (
        select(
            Facility.epa_facility_id,
            Facility.facility_name,
            Facility.state_code,
            total_co2,
        )
        .join(Unit, Unit.facility_id == Facility.epa_facility_id)
        .join(AnnualRecord, AnnualRecord.unit_id == Unit.internal_unit_key)
        .where(AnnualRecord.reporting_year == year)
        .group_by(
            Facility.epa_facility_id, Facility.facility_name, Facility.state_code
        )
        .order_by(total_co2.desc())
        .limit(_requested_limit())
    )

    try:
        rows = db.session.execute(statement).all()
    except SQLAlchemyError as exc:
        db.session.rollback()
        current_app.logger.exception("top-facilities query failed")
        return jsonify({"error": "Database query failed", "detail": str(exc)}), 500

    return jsonify(
        [
            {
                "facility_id": row.epa_facility_id,
                "facility_name": row.facility_name,
                "state_code": row.state_code,
                "year": year,
                "total_co2": round(float(row.total_co2 or 0.0), 2),
            }
            for row in rows
        ]
    )


@api_bp.route("/emissions/records", methods=["GET"])
def emissions_records():
    """Paginated, sortable, filterable annual emissions records.

    Accepts every data-explorer parameter (see app/services/search.py), so a
    page of the explorer and this endpoint return the same rows for the same
    query string. Invalid parameters produce a 400 listing each problem.
    """
    search = parse_search(request.args)
    if search.errors:
        return jsonify({"error": "Invalid search parameters", "detail": search.errors}), 400

    try:
        results = run_search(search)
    except SQLAlchemyError as exc:
        db.session.rollback()
        current_app.logger.exception("emissions-records query failed")
        return jsonify({"error": "Database query failed", "detail": str(exc)}), 500

    columns = search.export_columns
    return jsonify(
        {
            "page": results.page,
            "per_page": results.per_page,
            "total": results.total,
            "total_pages": results.total_pages,
            "level": search.level,
            "sort": search.sort,
            "order": search.order,
            "records": [
                {
                    key: _json_value(row._mapping.get(key))
                    for key, _ in [*columns, ("unit_key", "unit_key")]
                    if key in row._mapping
                }
                for row in results.rows
            ],
        }
    )


@api_bp.route("/emissions/interpret", methods=["GET"])
def interpret_description():
    """Read a plain-English description as data-explorer search parameters.

    Query parameters:
        q: the description, e.g. "coal units in Kentucky with high CO2".

    Returns the parameters, how each phrase was read, the words that were not
    used, and links to the same search in the explorer and in this API.
    """
    description = (request.args.get("q") or "").strip()
    if not description:
        return jsonify({"error": "Missing description", "detail": "Pass it as ?q=..."}), 400
    if len(description) > MAX_LENGTH:
        return jsonify({"error": "Description too long",
                        "detail": f"Keep it under {MAX_LENGTH} characters."}), 400
    latest = _latest_reporting_year()
    interpretation = interpret(description, latest_year=latest)
    body = interpretation.as_dict()
    body["explorer_url"] = url_for("main.explorer", q=interpretation.text, **interpretation.params)
    body["records_url"] = url_for("api.emissions_records", **interpretation.params)
    return jsonify(body)


def _json_value(value):
    if isinstance(value, float):
        return round(value, 2)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


@api_bp.route("/emissions/years", methods=["GET"])
def reporting_years():
    """Return every reporting year that has annual records, newest first.

    Intended for populating a year selector, so the front end offers only
    years that actually have data rather than a hardcoded range.
    """
    statement = (
        select(AnnualRecord.reporting_year)
        .distinct()
        .order_by(AnnualRecord.reporting_year.desc())
    )

    try:
        years = db.session.execute(statement).scalars().all()
    except SQLAlchemyError as exc:
        db.session.rollback()
        current_app.logger.exception("reporting-years query failed")
        return jsonify({"error": "Database query failed", "detail": str(exc)}), 500

    return jsonify([int(year) for year in years])