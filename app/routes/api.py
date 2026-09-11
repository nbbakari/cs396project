"""JSON endpoints backing the dashboard's charts.

Views here return JSON only -- no templates -- so the front end can fetch them
directly. Every response is a JSON document, including errors, so a failed
request never hands the browser an HTML error page to parse.
"""

from __future__ import annotations

from typing import Optional

from flask import Blueprint, current_app, jsonify, request
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app import db
from app.models import AnnualRecord, Facility, Unit

api_bp = Blueprint("api", __name__, url_prefix="/api")

DEFAULT_LIMIT = 10
MAX_LIMIT = 100


def _requested_limit() -> int:
    """Read ?limit= from the query string, falling back to the default."""
    raw = request.args.get("limit")
    if raw is None:
        return DEFAULT_LIMIT
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(value, MAX_LIMIT))


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
    # A blank ?year= (a frontend interpolating an unset dropdown) is treated as
    # absent rather than as a parse error.
    raw_year = (request.args.get("year") or "").strip()
    year: Optional[int]

    if raw_year:
        try:
            year = int(raw_year)
        except ValueError:
            # A mistyped year must not silently return a different year's data.
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
            # Nothing ingested yet: an empty chart, not an error.
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
        # Grouped on the EPA facility ID rather than the name alone: plant
        # names repeat across states, and grouping by name would silently
        # merge two unrelated facilities into a single bar.
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
                # Repeated on every row so the response stays a flat array
                # while still telling the caller which year it is looking at.
                "year": year,
                # Rounded to match the precision CAMPD reports short tons in,
                # and to keep binary-float artefacts out of the JSON.
                "total_co2": round(float(row.total_co2 or 0.0), 2),
            }
            for row in rows
        ]
    )


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
