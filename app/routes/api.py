"""API routes for epaData data retrieval and AI natural language search."""

import logging
import os
import time
from decimal import Decimal
from typing import Optional

from flask import Blueprint, current_app, jsonify, request, url_for
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app import db
from app.models import AnnualRecord, Facility, Unit
from app.services.describe import MAX_LENGTH, interpret
from app.services.search import parse_search, run_search

api_bp = Blueprint("api", __name__, url_prefix="/api")
ai_bp = Blueprint("ai", __name__, url_prefix="/api/ai")

logger = logging.getLogger(__name__)

# Change the model here, or set GEMINI_MODEL in .env, if you hit a quota limit.
AI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")

# Measurements the AI may rank or total by (columns of annual_records).
METRICS = ("co2_mass", "so2_mass", "nox_mass", "gross_load", "heat_input")
LEVELS = ("facility", "state", "unit", "year")

AI_SYSTEM_INSTRUCTION = (
    "You are an assistant for epaData, an explorer of EPA power-plant emissions data. "
    "The data holds one record per unit per reporting year, with CO2, SO2 and NOx mass "
    "(tons), gross load (MWh) and heat input (MMBtu). Each unit belongs to a facility in "
    "a state and has a primary fuel such as coal or natural gas. "
    "Rules: "
    "1. Always call `query_epa_database` before answering and never invent numbers. "
    "Use the `stats` and `rows` values exactly as returned; do not recalculate them. "
    "2. Choose `level` to match the question: 'facility' for top plants, 'state' for "
    "which states, 'unit' for individual units, and 'year' to compare or show a trend "
    "across years. To compare years, make ONE call with level='year' and year_min and "
    "year_max (or no year filter), instead of one call per year. "
    "3. Put fuel in primary_fuel (for example 'coal' or 'gas'), the state as a "
    "two-letter code, and the reporting year in `year`. "
    "4. Explain the results in plain English with the takeaway first. Mention totals, "
    "the highest and lowest values, and for comparisons give each value, the absolute "
    "change and the percent change. "
    "5. If status is 'no_records', say nothing matched and suggest a different search. "
    "If status is 'error', say the lookup failed and what was being searched."
)


def _clean(value):
    """Make a database value JSON friendly."""
    if isinstance(value, Decimal):
        return float(value)
    return value


def _tables():
    tables = db.metadata.tables
    return tables["facilities"], tables["units"], AnnualRecord.__table__


def run_emissions_query(
    year: Optional[int] = None,
    year_min: Optional[int] = None,
    year_max: Optional[int] = None,
    state: Optional[str] = None,
    facility_name: Optional[str] = None,
    primary_fuel: Optional[str] = None,
    county: Optional[str] = None,
    unit_type: Optional[str] = None,
    level: str = "facility",
    rank_by: str = "co2_mass",
    order: str = "desc",
    limit: int = 10,
) -> dict:
    """Search annual emissions with real SQL and return stats plus ranked rows."""
    facilities, units, records = _tables()

    level = level if level in LEVELS else "facility"
    rank_by = rank_by if rank_by in METRICS else "co2_mass"
    limit = max(1, min(int(limit or 10), 50))
    descending = (order or "desc").lower() != "asc"

    source = records.join(
        units, records.c.unit_id == units.c.internal_unit_key
    ).join(facilities, units.c.facility_id == facilities.c.epa_facility_id)

    conditions = []
    filters = {}
    if year:
        conditions.append(records.c.reporting_year == int(year))
        filters["year"] = int(year)
    if year_min:
        conditions.append(records.c.reporting_year >= int(year_min))
        filters["year_min"] = int(year_min)
    if year_max:
        conditions.append(records.c.reporting_year <= int(year_max))
        filters["year_max"] = int(year_max)
    if state:
        codes = [s.strip().upper() for s in state.split(",") if s.strip()]
        if codes:
            conditions.append(func.upper(facilities.c.state_code).in_(codes))
            filters["state"] = ",".join(codes)
    if facility_name:
        conditions.append(facilities.c.facility_name.ilike(f"%{facility_name.strip()}%"))
        filters["facility_name"] = facility_name
    if primary_fuel:
        conditions.append(units.c.primary_fuel.ilike(f"%{primary_fuel.strip()}%"))
        filters["primary_fuel"] = primary_fuel
    if county:
        conditions.append(facilities.c.county.ilike(f"%{county.strip()}%"))
        filters["county"] = county
    if unit_type:
        conditions.append(units.c.unit_type.ilike(f"%{unit_type.strip()}%"))
        filters["unit_type"] = unit_type

    # Statistics over ALL matching unit-year records (not just the rows shown).
    s = db.session.execute(
        select(
            func.count(records.c.id),
            func.count(distinct(units.c.internal_unit_key)),
            func.count(distinct(facilities.c.epa_facility_id)),
            func.sum(records.c.co2_mass),
            func.sum(records.c.so2_mass),
            func.sum(records.c.nox_mass),
            func.sum(records.c.gross_load),
            func.sum(records.c.heat_input),
            func.avg(records.c.co2_mass),
            func.max(records.c.co2_mass),
            func.min(records.c.co2_mass),
        )
        .select_from(source)
        .where(*conditions)
    ).one()

    if not s[0]:
        return {"status": "no_records", "filters": filters}

    stats = {
        "record_count": int(s[0]),
        "unit_count": int(s[1]),
        "facility_count": int(s[2]),
        "co2_mass_total": float(s[3] or 0),
        "so2_mass_total": float(s[4] or 0),
        "nox_mass_total": float(s[5] or 0),
        "gross_load_total": float(s[6] or 0),
        "heat_input_total": float(s[7] or 0),
        "co2_mass_average_per_unit_year": float(s[8] or 0),
        "co2_mass_max_unit_year": float(s[9] or 0),
        "co2_mass_min_unit_year": float(s[10] or 0),
    }

    totals = {
        name: func.coalesce(func.sum(records.c[name]), 0).label(name)
        for name in METRICS
    }
    direction = (lambda col: col.desc()) if descending else (lambda col: col.asc())

    if level == "facility":
        stmt = (
            select(
                facilities.c.epa_facility_id,
                facilities.c.facility_name,
                facilities.c.state_code,
                *totals.values(),
            )
            .select_from(source)
            .where(*conditions)
            .group_by(
                facilities.c.epa_facility_id,
                facilities.c.facility_name,
                facilities.c.state_code,
            )
            .order_by(direction(totals[rank_by]))
            .limit(limit)
        )
    elif level == "state":
        stmt = (
            select(
                facilities.c.state_code,
                func.count(distinct(facilities.c.epa_facility_id)).label("facility_count"),
                *totals.values(),
            )
            .select_from(source)
            .where(*conditions)
            .group_by(facilities.c.state_code)
            .order_by(direction(totals[rank_by]))
            .limit(limit)
        )
    elif level == "year":
        stmt = (
            select(
                records.c.reporting_year,
                func.count(distinct(facilities.c.epa_facility_id)).label("facility_count"),
                *totals.values(),
            )
            .select_from(source)
            .where(*conditions)
            .group_by(records.c.reporting_year)
            .order_by(records.c.reporting_year.asc())
        )
    else:  # unit
        metric_col = records.c[rank_by]
        stmt = (
            select(
                facilities.c.facility_name,
                facilities.c.state_code,
                units.c.epa_unit_id,
                units.c.unit_type,
                units.c.primary_fuel,
                records.c.reporting_year,
                records.c.co2_mass,
                records.c.so2_mass,
                records.c.nox_mass,
                records.c.gross_load,
                records.c.heat_input,
                records.c.operating_time,
            )
            .select_from(source)
            .where(*conditions)
            .order_by(direction(metric_col).nulls_last())
            .limit(limit)
        )

    rows = [
        {k: _clean(v) for k, v in row._mapping.items()}
        for row in db.session.execute(stmt).all()
    ]

    return {
        "status": "success",
        "filters": filters,
        "level": level,
        "ranked_by": rank_by,
        "stats": stats,
        "rows": rows,
    }


def query_epa_database(
    year: Optional[int] = None,
    year_min: Optional[int] = None,
    year_max: Optional[int] = None,
    state: Optional[str] = None,
    facility_name: Optional[str] = None,
    primary_fuel: Optional[str] = None,
    county: Optional[str] = None,
    unit_type: Optional[str] = None,
    level: str = "facility",
    rank_by: str = "co2_mass",
    order: str = "desc",
    limit: int = 10,
) -> dict:
    """Search EPA annual emissions records and return statistics and ranked rows.

    Args:
        year: A single reporting year, e.g. 2022.
        year_min: First reporting year of a range (inclusive).
        year_max: Last reporting year of a range (inclusive).
        state: Two-letter state code, or several separated by commas, e.g. 'KY' or 'KY,WV'.
        facility_name: Part of a facility (plant) name.
        primary_fuel: Primary fuel to match, e.g. 'coal' or 'gas'.
        county: Part of a county name.
        unit_type: Part of a unit type name.
        level: How to group the rows: 'facility', 'state', 'unit' or 'year'. Use 'year' to compare years.
        rank_by: Measurement to rank by: co2_mass, so2_mass, nox_mass, gross_load or heat_input.
        order: 'desc' for highest first, 'asc' for lowest first.
        limit: How many rows to return (1 to 50).
    """
    try:
        return run_emissions_query(
            year=year, year_min=year_min, year_max=year_max, state=state,
            facility_name=facility_name, primary_fuel=primary_fuel, county=county,
            unit_type=unit_type, level=level, rank_by=rank_by, order=order, limit=limit,
        )
    except Exception as exc:  # report the problem to the model instead of crashing
        logger.exception("query_epa_database failed")
        db.session.rollback()
        return {"status": "error", "message": str(exc)}


def get_gemini_client() -> genai.Client:
    """Initialize and return the Google GenAI client."""
    return genai.Client()


def _build_history(raw_history) -> list:
    """Turn [{'role': 'user'|'model', 'text': '...'}] from the page into Gemini history."""
    history = []
    if not isinstance(raw_history, list):
        return history
    for item in raw_history[-12:]:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        text = (item.get("text") or "").strip()
        if role in ("user", "model") and text:
            history.append(types.Content(role=role, parts=[types.Part(text=text)]))
    return history


@ai_bp.route("/query", methods=["POST"])
def ai_query():
    """Natural-language assistant: Gemini calls the database tool and explains the results."""
    data = request.get_json(silent=True) or {}
    user_message = (data.get("prompt") or "").strip()

    if not user_message:
        return jsonify({"error": "No prompt provided."}), 400

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
    """Return available years for emissions data."""
    try:
        years = db.session.scalars(
            select(AnnualRecord.reporting_year)
            .distinct()
            .order_by(AnnualRecord.reporting_year.desc())
        ).all()
        return jsonify({"years": years})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@api_bp.route("/emissions/top-facilities", methods=["GET"])
def top_facilities():
    """Return top emitting facilities (by total CO2) for a given year."""
    try:
        year = request.args.get("year", type=int)
        limit = request.args.get("limit", type=int, default=10)

        if year is None:  # default to the most recent year in the data
            year = db.session.scalar(select(func.max(AnnualRecord.reporting_year)))

        result = run_emissions_query(year=year, level="facility", rank_by="co2_mass", limit=limit)
        data = [
            {
                "facility_name": row["facility_name"],
                "state": row["state_code"],
                "co2_mass": row["co2_mass"],
            }
            for row in result.get("rows", [])
        ]
        return jsonify({"data": data})
    except Exception as e:
        return jsonify({"error": str(e)}), 500