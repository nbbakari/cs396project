"""AI assistant: answers plain-English questions about the emissions data.

Google's Gemini model reads the question and calls one tool,
:func:`query_epa_database`, which runs real SQL on this computer's database
and returns statistics and ranked rows. Gemini then explains those numbers in
plain English. The model never sees the database directly, is told never to
invent numbers, and quotes year-over-year changes that are calculated here
rather than doing the arithmetic itself.

Setup: the ``google-genai`` package (in requirements.txt) and a free Gemini
API key, pasted on the Settings page or set as GEMINI_API_KEY in .env. Without
them the assistant says it is not set up, and the rest of the app is unaffected.
"""

import logging
import time
from decimal import Decimal
from typing import Optional

from flask import current_app
from sqlalchemy import and_, case, distinct, func, select

from app import db
from app.models import AnnualRecord

try:  # Optional: the app runs without the Gemini client installed.
    from google import genai
    from google.genai import types
except ImportError:
    genai = types = None

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-3.5-flash-lite"
KEY_SIGNUP_URL = "https://aistudio.google.com/apikey"
MAX_PROMPT_LENGTH = 2000
MAX_HISTORY = 12

# Measurements the AI may rank or total by (columns of annual_records).
METRICS = ("co2_mass", "so2_mass", "nox_mass", "gross_load", "heat_input")
LEVELS = ("facility", "state", "unit", "year")

AI_RULES = (
    "You are an assistant for epaData, an explorer of EPA power-plant emissions data. "
    "The data holds one record per unit per reporting year, with CO2, SO2 and NOx mass "
    "(short tons), gross load (MWh) and heat input (MMBtu). Each unit belongs to a facility "
    "in a state and has a primary fuel such as coal or natural gas. "
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
    "change and the percent change, quoting the `_change_from_previous` and "
    "`_pct_change_from_previous` fields; never calculate changes yourself. "
    "5. If status is 'no_records', say nothing matched and suggest a different search, "
    "such as a year that has data. If status is 'error', say the lookup failed and what "
    "was being searched. "
    "6. A null value means CAMPD did not report it: say 'not reported', never treat it "
    "as zero. `not_reported` counts unit-years with no reported value."
)


class AssistantError(Exception):
    """A problem to show the user, with the HTTP status to return."""

    def __init__(self, message: str, status: int = 503):
        super().__init__(message)
        self.status = status


def is_installed() -> bool:
    return genai is not None


# ---------------------------------------------------------------------------
# The database tool
# ---------------------------------------------------------------------------


def _clean(value):
    """Make a database value JSON friendly."""
    if isinstance(value, Decimal):
        value = float(value)
    if isinstance(value, float):
        return round(value, 4)
    return value


def _tables():
    tables = db.metadata.tables
    return tables["facilities"], tables["units"], AnnualRecord.__table__


def _reported(records, name):
    """A metric, or NULL where it was blank in CAMPD (Phase 1 stored those as 0)."""
    return case((records.c.missing_metrics.like(f"%{name}%"), None), else_=records.c[name])


def _paired_sum(value, other):
    """Sum of ``value`` over the rows where ``other`` was also reported."""
    return func.sum(case((and_(value.is_not(None), other.is_not(None)), value)))


def _ratio(numerator, denominator):
    if numerator is None or not denominator:
        return None
    return float(numerator) / float(denominator)


def _add_year_changes(rows: list[dict]) -> None:
    """Change of every metric from the previous year in the results."""
    previous = None
    for row in rows:
        if previous is not None:
            row["compared_with_year"] = previous["reporting_year"]
            for name in METRICS:
                now, before = row.get(name), previous.get(name)
                change = now - before if now is not None and before is not None else None
                row[f"{name}_change_from_previous"] = _clean(change)
                row[f"{name}_pct_change_from_previous"] = (
                    round(100 * change / before, 1) if change is not None and before else None
                )
        previous = row


def available_years() -> list[int]:
    return list(db.session.scalars(
        select(AnnualRecord.reporting_year).distinct().order_by(AnnualRecord.reporting_year)
    ))


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

    value = {name: _reported(records, name) for name in METRICS}

    # Statistics over ALL matching unit-year records (not just the rows shown).
    s = db.session.execute(
        select(
            func.count(records.c.id),
            func.count(distinct(units.c.internal_unit_key)),
            func.count(distinct(facilities.c.epa_facility_id)),
            *(func.sum(value[name]) for name in METRICS),
            *(func.count(value[name]) for name in METRICS),
            func.avg(value["co2_mass"]),
            func.max(value["co2_mass"]),
            func.min(value["co2_mass"]),
            _paired_sum(value["co2_mass"], value["gross_load"]),
            _paired_sum(value["gross_load"], value["co2_mass"]),
            _paired_sum(value["heat_input"], value["gross_load"]),
            _paired_sum(value["gross_load"], value["heat_input"]),
        )
        .select_from(source)
        .where(*conditions)
    ).one()

    if not s[0]:
        return {"status": "no_records", "filters": filters, "years_with_data": available_years()}

    count = int(s[0])
    n = len(METRICS)
    sums, reported = s[3:3 + n], s[3 + n:3 + 2 * n]
    avg, high, low, co2_num, co2_den, heat_num, heat_den = s[3 + 2 * n:]
    stats = {
        "record_count": count,
        "unit_count": int(s[1]),
        "facility_count": int(s[2]),
        **{f"{name}_total": _clean(total) for name, total in zip(METRICS, sums)},
        "co2_mass_average_per_unit_year": _clean(avg),
        "co2_mass_max_unit_year": _clean(high),
        "co2_mass_min_unit_year": _clean(low),
        "co2_intensity_tons_per_mwh": _clean(_ratio(co2_num, co2_den)),
        "heat_rate_mmbtu_per_mwh": _clean(_ratio(heat_num, heat_den)),
        "not_reported": {
            name: count - int(n_reported)
            for name, n_reported in zip(METRICS, reported)
            if count - int(n_reported)
        },
    }

    totals = {name: func.sum(value[name]).label(name) for name in METRICS}
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
            .order_by(direction(totals[rank_by]).nulls_last())
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
            .order_by(direction(totals[rank_by]).nulls_last())
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
        stmt = (
            select(
                facilities.c.facility_name,
                facilities.c.state_code,
                units.c.epa_unit_id,
                units.c.unit_type,
                units.c.primary_fuel,
                records.c.reporting_year,
                *(value[name].label(name) for name in METRICS),
                records.c.operating_time,
            )
            .select_from(source)
            .where(*conditions)
            .order_by(direction(value[rank_by]).nulls_last())
            .limit(limit)
        )

    rows = [
        {k: _clean(v) for k, v in row._mapping.items()}
        for row in db.session.execute(stmt).all()
    ]
    if level == "year":
        _add_year_changes(rows)

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


# ---------------------------------------------------------------------------
# Talking to Gemini
# ---------------------------------------------------------------------------


def system_instruction() -> str:
    years = available_years()
    if not years:
        return AI_RULES + " The database is empty, so explain that data must be loaded first."
    return (
        AI_RULES
        + f" Reporting years with data: {', '.join(str(y) for y in years)}."
        + f" When the user gives no year, use the latest one, {years[-1]}."
    )


def make_client(api_key: str):
    return genai.Client(api_key=api_key)


def build_history(raw_history) -> list:
    """Turn [{'role': 'user'|'model', 'text': '...'}] from the page into Gemini history."""
    history = []
    if not isinstance(raw_history, list):
        return history
    for item in raw_history[-MAX_HISTORY:]:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        text = (item.get("text") or "").strip()
        if role in ("user", "model") and text:
            history.append(types.Content(role=role, parts=[types.Part(text=text)]))
    return history


def _explain_failure(error: Exception, model: str) -> AssistantError:
    detail = str(error)
    if "429" in detail or "RESOURCE_EXHAUSTED" in detail:
        return AssistantError(
            "The AI has reached its free usage limit for now. Try again later, or set "
            "GEMINI_MODEL in .env to a different model.", 429)
    if "API_KEY_INVALID" in detail or "API key not valid" in detail or "PERMISSION_DENIED" in detail:
        return AssistantError(
            "Gemini rejected the API key. Paste a valid key on the Settings page.", 503)
    if "NOT_FOUND" in detail or "404" in detail:
        return AssistantError(
            f"The Gemini model '{model}' is not available to this key. Set GEMINI_MODEL in "
            ".env to another model.", 503)
    return AssistantError(f"AI call failed: {type(error).__name__}: {error}", 503)


def ask(prompt: str, raw_history=None) -> str:
    """Answer one question, using the conversation so far. Raises AssistantError."""
    if not is_installed():
        raise AssistantError(
            "The AI assistant is not installed on this computer. Run "
            "`pip install -r requirements.txt` and restart the app.")
    api_key = current_app.config.get("GEMINI_API_KEY")
    if not api_key:
        raise AssistantError(
            "The AI assistant needs a Gemini API key. Get a free one at "
            f"{KEY_SIGNUP_URL} and paste it on the Settings page.")

    model = current_app.config.get("GEMINI_MODEL") or DEFAULT_MODEL
    # Keep the client in a variable: the SDK closes its connection when the
    # client object is garbage-collected, even while a chat still uses it.
    client = make_client(api_key)
    chat = client.chats.create(model=model, history=build_history(raw_history))
    config = types.GenerateContentConfig(
        tools=[query_epa_database],
        system_instruction=system_instruction(),
        temperature=0.2,
    )

    last_error: Optional[Exception] = None
    for attempt in range(3):
        try:
            response = chat.send_message(prompt, config=config)
        except Exception as error:  # noqa: BLE001 - the SDK raises many error types
            last_error = error
            current_app.logger.error("AI attempt %d failed: %s", attempt + 1, error)
            if "503" in str(error) or "UNAVAILABLE" in str(error):
                time.sleep(2 * (attempt + 1))  # wait 2s, 4s, 6s, then try again
                continue
            break
        return response.text or "No response generated."
    raise _explain_failure(last_error, model)


def verify_key(api_key: str) -> None:
    """Check a key with Google. Raises AssistantError(status=401) if it is rejected,
    or AssistantError(status=503) if it could not be checked."""
    if not is_installed():
        raise AssistantError("The google-genai package is not installed.", 503)
    model = current_app.config.get("GEMINI_MODEL") or DEFAULT_MODEL
    client = make_client(api_key)  # kept alive for the call (see ask)
    try:
        client.models.get(model=model)
    except Exception as error:  # noqa: BLE001
        explained = _explain_failure(error, model)
        if "rejected the API key" in str(explained) or "400" in str(error):
            raise AssistantError("Google rejected that key.", 401) from error
        raise explained from error
