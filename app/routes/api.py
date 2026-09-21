"""JSON endpoints backing the dashboard's charts and data explorer.

Views here return JSON only -- no templates -- so the front end can fetch them
directly. Every response is a JSON document, including errors, so a failed
request never hands the browser an HTML error page to parse.
"""

from __future__ import annotations

import math
from typing import Optional

from flask import Blueprint, current_app, jsonify, request
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app import db
from app.models import AnnualRecord, Facility, Unit

api_bp = Blueprint("api", __name__, url_prefix="/api")

DEFAULT_LIMIT = 10
MAX_LIMIT = 100

DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100

# Columns the explorer table is allowed to sort by, mapped onto the actual
# SQLAlchemy column. Keeping this an explicit allowlist -- rather than doing
# getattr(Model, request.args["sort"]) -- means a request can never sort by an
# arbitrary/unintended column.
SORTABLE_COLUMNS = {
    "facility_name": Facility.facility_name,
    "state_code": Facility.state_code,
    "epa_unit_id": Unit.epa_unit_id,
    "reporting_year": AnnualRecord.reporting_year,
    "gross_load": AnnualRecord.gross_load,
    "heat_input": AnnualRecord.heat_input,
    "co2_mass": AnnualRecord.co2_mass,
    "so2_mass": AnnualRecord.so2_mass,
    "nox_mass": AnnualRecord.nox_mass,
}


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

    Backs the Data Explorer table. Query parameters:
        page:        1-indexed page number (default 1).
        per_page:    rows per page (default 25, capped at 100).
        sort:        one of SORTABLE_COLUMNS (default "reporting_year").
        order:       "asc" or "desc" (default "desc").
        year:        filter to one reporting year.
        state:       filter to one two-letter state code.
        facility_id: filter to one EPA facility ID (used by the facility
                     drill-down page to show just that plant's records).
    """
    page = max(1, _int_arg("page", 1))
    per_page = max(1, min(_int_arg("per_page", DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE))

    sort_key = request.args.get("sort", "reporting_year")
    if sort_key not in SORTABLE_COLUMNS:
        sort_key = "reporting_year"
    sort_column = SORTABLE_COLUMNS[sort_key]

    order = request.args.get("order", "desc").lower()
    order_expr = sort_column.asc() if order == "asc" else sort_column.desc()

    query = (
        select(
            Facility.epa_facility_id,
            Facility.facility_name,
            Facility.state_code,
            Unit.internal_unit_key,
            Unit.epa_unit_id,
            AnnualRecord.reporting_year,
            AnnualRecord.gross_load,
            AnnualRecord.heat_input,
            AnnualRecord.co2_mass,
            AnnualRecord.so2_mass,
            AnnualRecord.nox_mass,
        )
        .join(Unit, Unit.facility_id == Facility.epa_facility_id)
        .join(AnnualRecord, AnnualRecord.unit_id == Unit.internal_unit_key)
    )

    raw_year = (request.args.get("year") or "").strip()
    if raw_year:
        try:
            query = query.where(AnnualRecord.reporting_year == int(raw_year))
        except ValueError:
            return jsonify({"error": f"Invalid year '{raw_year}'."}), 400

    state = (request.args.get("state") or "").strip().upper()
    if state:
        query = query.where(Facility.state_code == state)

    raw_facility_id = (request.args.get("facility_id") or "").strip()
    if raw_facility_id:
        try:
            query = query.where(Facility.epa_facility_id == int(raw_facility_id))
        except ValueError:
            return jsonify({"error": f"Invalid facility_id '{raw_facility_id}'."}), 400

    try:
        total = (
            db.session.execute(select(func.count()).select_from(query.subquery())).scalar()
            or 0
        )
        rows = db.session.execute(
            query.order_by(order_expr, AnnualRecord.id)
            .offset((page - 1) * per_page)
            .limit(per_page)
        ).all()
    except SQLAlchemyError as exc:
        db.session.rollback()
        current_app.logger.exception("emissions-records query failed")
        return jsonify({"error": "Database query failed", "detail": str(exc)}), 500

    total_pages = max(1, math.ceil(total / per_page))

    return jsonify(
        {
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "sort": sort_key,
            "order": "asc" if order == "asc" else "desc",
            "records": [
                {
                    "facility_id": row.epa_facility_id,
                    "facility_name": row.facility_name,
                    "state_code": row.state_code,
                    "unit_key": row.internal_unit_key,
                    "epa_unit_id": row.epa_unit_id,
                    "reporting_year": row.reporting_year,
                    "gross_load": round(float(row.gross_load or 0), 2),
                    "heat_input": round(float(row.heat_input or 0), 2),
                    "co2_mass": round(float(row.co2_mass or 0), 2),
                    "so2_mass": round(float(row.so2_mass or 0), 2),
                    "nox_mass": round(float(row.nox_mass or 0), 2),
                }
                for row in rows
            ],
        }
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