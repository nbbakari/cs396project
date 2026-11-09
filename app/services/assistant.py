"""AI assistant: answers plain-English questions about the data and the
website, and takes the user to any page.

Google's Gemini model reads the question, together with a snapshot of the
page the user is looking at (address, filters, numbers and table rows), and
can call four tools:

* :func:`query_epa_database` -- totals, rankings and year comparisons (real SQL)
* :func:`summarize_records`  -- mean, median, min and max of every measurement
  and derived indicator for any selection, with missing-value counts
* :func:`get_app_status`     -- what data is loaded, imports awaiting review,
  which keys and services are set up, and the TRACI factors
* :func:`navigate`           -- open a page of the site (with filters) or offer
  a download link

The model never sees the database directly, is told never to invent numbers,
and quotes year-over-year changes that are calculated here. It cannot change
data: for a retrieval, an import, a deletion or a key it opens the right page,
filled in, and the user presses the button.

Setup: the ``google-genai`` package (in requirements.txt) and a free Gemini
API key, pasted on the Settings page or set as GEMINI_API_KEY in .env. Without
them the assistant says it is not set up, and the rest of the app is unaffected.
"""

import json
import logging
import time
from decimal import Decimal
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode

from flask import current_app, g
from sqlalchemy import and_, case, distinct, func, select
from werkzeug.datastructures import MultiDict
from werkzeug.exceptions import HTTPException

from app import db
from app.models import AnnualRecord, Dataset, Facility, UploadedFile, Unit
from app.services.analysis import dataset_counts, parse_analysis
from app.services.search import parse_search
from app.glossary import glossary_text
from app.services.impacts import (
    DISCLAIMER,
    IMPACT_GROUPS,
    grouped_impacts,
    impact_summary,
    sorted_groups,
)
from app.services.statistics import (
    DIMENSIONS,
    all_variables,
    compare_groups,
    describe_values,
    level_frame,
    load_selection,
    shown_groups,
)
from app.services.traci import CATEGORY_LABELS, factors_for

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
MAX_PAGE_CONTEXT = 15000  # characters of the on-screen snapshot sent with a question

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
    "as zero. `not_reported` counts unit-years with no reported value. "
    "7. Each question may come with SCREEN: a snapshot of the page the user is looking at "
    "(address, heading, filters, form values, number cards and the first table rows). "
    "When the user says 'this', 'here', 'my screen', 'these results' or asks about their "
    "filters or configuration, answer from SCREEN first; use the tools for anything it does "
    "not show. The snapshot holds only the first rows of a table; say so when it matters. "
    "8. To take the user somewhere, or to change what the current page shows (filters, "
    "sorting, level, page number), call `navigate` with the path and the full query "
    "parameters, starting from the current ones in SCREEN. Call it once, with open_now=true "
    "when the user asked to go, open, show or change something; then tell them in one "
    "sentence what will open. For files use the download paths, which give a link. "
    "9. For mean, median, standard deviation, quartiles, IQR, minimum or maximum values, "
    "missing-value counts, or any derived indicator (CO2/SO2/NOx intensity, heat rate, "
    "operating utilization), use `summarize_records`. To compare or rank facilities, states, "
    "fuels, unit types or years with absolute and percent differences, use `compare_and_rank`. "
    "For potential environmental impacts (climate change, acid rain, fine particles, smog) use "
    "`assess_traci_impacts`, and say they are potential impacts, not measured harm. For what "
    "data is loaded, imports waiting for review, keys or settings, or the TRACI factors, use "
    "`get_app_status`. "
    "10. You cannot change data or settings. For retrieving data from the EPA, uploading or "
    "approving a file, deleting a dataset, saving an API key or loading shared data, open "
    "the right page (pre-filled where possible) and tell the user exactly which button to "
    "press. Never ask for, repeat or guess API keys or passwords. "
    "11. Many users are not scientists. Each time you first use a technical term in an "
    "answer, add its everyday meaning in parentheses, for example 'acidification (acid "
    "rain)', 'heat rate (fuel burned per MWh; lower is more efficient)' or 'NOx (nitrogen "
    "oxides)'. Use the glossary below, and keep explanations short."
)

SITE_GUIDE = (
    "Pages of the epaData website (path: what it is; query parameters): "
    "/ : dashboard with coverage and recent imports. "
    "/explorer : Data Explorer, a table of annual records. Filters: state (two-letter codes, "
    "comma-separated), year (comma-separated), year_min, year_max, facility_id, "
    "facility_name, unit_id, county, primary_fuel, secondary_fuel, unit_type, owner, "
    "so2_control, nox_control, pm_control, hg_control, program (text filters match any "
    "part; controls also take none, any, or 'not <text>'), ranges op_time_min/_max, "
    "gross_load_min/_max, heat_input_min/_max, co2_min/_max, so2_min/_max, nox_min/_max; "
    "level (unit or facility), sort (reporting_year, co2_mass, so2_mass, nox_mass, "
    "gross_load, heat_input, operating_time, facility_name, state_code), order (asc or "
    "desc), limit (top N), group_by (state, year, primary_fuel, unit_type, facility) with "
    "per_group, page, per_page (10, 25, 50, 100), q (a plain-English description). "
    "/analysis : Analytical Dataset Builder: the same filters plus level, yoy (variables "
    "for year-over-year change, e.g. co2_mass or co2_intensity), sort (any column, "
    "including co2_intensity, so2_intensity, nox_intensity, heat_rate, "
    "operating_utilization), order, page. Shows counts, missing values, variable "
    "definitions, an indicator preview and the TRACI factors. "
    "/analysis/statistics : descriptive statistics (count, missing, mean, std dev, min, Q1, "
    "median, Q3, max, IQR, percentiles, 95% CI, skewness, outliers) of every variable, "
    "including TRACI impacts; builder filters plus level, group_by (year, state, "
    "primary_fuel, unit_type) and variable (shown per group). "
    "/analysis/compare : compare and rank groups; builder filters plus by (state, "
    "primary_fuel, facility, unit_type, year), metric (any variable or TRACI impact such as "
    "traci_acidification or traci_global_climate_per_mwh), baseline (a group to compare "
    "with), order (desc or asc), limit (10, 25, 50, 100, all). "
    "/analysis/impacts : TRACI impact assessment (kg CO2 eq, kg SO2 eq, kg PM2.5 eq, kg O3 eq "
    "and per MWh) with the calculation shown; builder filters plus group_by (state, "
    "primary_fuel, unit_type, year, facility), metric (impact to rank by), version, limit. "
    "/data-sources : where the data comes from (CAMPD, TRACI) and the loaded datasets. "
    "/coverage : coverage dashboard with records by year and facilities by state. "
    "/compare : year-by-year comparison chart; facility_id (up to 8, comma-separated) or "
    "unit (internal unit keys, which query_epa_database returns as unit_key), metric "
    "(co2_mass, so2_mass, nox_mass, gross_load, heat_input, operating_time). "
    "/facility/<facility_id> , /unit/<unit_key> , /dataset/<dataset_id> : detail pages. "
    "/retrieve : retrieval from the EPA CAMPD API; can be pre-filled with year, state, "
    "facility_ids, fuel_type, unit_type, control_technology, dataset_name; the user presses "
    "Retrieve. /upload : upload a CSV or Excel file (the user chooses the file). "
    "/review/<file_id> : review an import waiting for approval. /downloads : downloads. "
    "/settings : CAMPD and Gemini API keys, shared data. "
    "Downloads (links, not pages): /download/records with explorer filters and "
    "format=csv or xlsx; /analysis/download with builder filters and format=csv or xlsx; "
    "/analysis/statistics/download, /analysis/compare/download and /analysis/impacts/download "
    "with the same parameters as their pages and format=csv or xlsx; "
    "/download/issues?dataset=<id>; /download/provenance."
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
                facilities.c.epa_facility_id.label("facility_id"),
                facilities.c.facility_name,
                facilities.c.state_code,
                units.c.internal_unit_key.label("unit_key"),
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
# More tools: statistics, the app's own state, and navigation
# ---------------------------------------------------------------------------


def _round(value: Any) -> Any:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    if number != number:  # NaN
        return None
    return float(f"{number:.6g}")


def summarize_records(query: str = "", level: str = "unit") -> dict:
    """Statistics for any selection of records: how many records, facilities, units and years; and for every measurement, derived indicator (CO2/SO2/NOx intensity, heat rate, operating utilization) and TRACI potential impact the count, missing count, mean, median, standard deviation, minimum, first and third quartiles, interquartile range and maximum.

    Args:
        query: Record filters as URL query parameters, the same as the Data Explorer, e.g. 'state=OH&primary_fuel=coal&year=2020'. Empty for all records.
        level: 'unit' for unit-years, or 'facility' for facility-year totals.
    """
    try:
        args = MultiDict([(k, v) for k, v in parse_qsl(query or "") if k not in ("level", "yoy")])
        args["level"] = level if level in ("unit", "facility") else "unit"
        analysis = parse_analysis(args)
        if analysis.errors:
            return {"status": "error", "message": " ".join(analysis.errors)}
        units, cats = load_selection(analysis)
        frame = level_frame(units, analysis.level, cats)
        statistics = {}
        for name, var in all_variables(analysis.level, cats).items():
            stats = describe_values(frame[name] if name in frame else [])
            statistics[name] = {
                "unit": var.unit,
                **{key: stats[key] if key in ("count", "missing") else _round(stats[key])
                   for key in SUMMARY_STATISTICS if stats[key] is not None},
            }
        return {
            "status": "success" if len(frame) else "no_records",
            "level": analysis.level,
            "selection": dataset_counts(units),
            "rows": int(len(frame)),
            "statistics": statistics,
        }
    except Exception as exc:  # report the problem to the model instead of crashing
        logger.exception("summarize_records failed")
        db.session.rollback()
        return {"status": "error", "message": str(exc)}


SUMMARY_STATISTICS = ("count", "missing", "mean", "median", "std", "min", "q1", "q3", "iqr", "max")


def _selection_from(query: str):
    args = MultiDict([(k, v) for k, v in parse_qsl(query or "")])
    analysis = parse_analysis(args)
    if analysis.errors:
        return None, None, None, {"status": "error", "message": " ".join(analysis.errors)}
    units, cats = load_selection(analysis)
    return analysis, units, cats, None


def compare_and_rank(query: str = "", by: str = "state", metric: str = "co2_intensity",
                     baseline: str = "", order: str = "desc", limit: int = 10) -> dict:
    """Compare and rank groups of records on one variable: each group's value, rank (1 = highest), percentile, z-score, and absolute and percent difference from a reference, plus a Kruskal-Wallis test of whether the groups really differ.

    Args:
        query: Record filters as URL query parameters, e.g. 'year=2020&primary_fuel=coal'. Empty for all records.
        by: What to compare: 'state', 'primary_fuel', 'facility', 'unit_type' or 'year'.
        metric: The variable, e.g. 'co2_mass', 'co2_intensity', 'heat_rate', 'so2_intensity', 'traci_acidification', 'traci_global_climate_per_mwh'.
        baseline: A group to compare every other group with, e.g. 'OH' or 'Coal'. Empty compares with the average group (for totals) or all records combined (for rates).
        order: 'desc' for highest first, 'asc' for lowest first.
        limit: How many groups to return, 1 to 25.
    """
    try:
        analysis, units, cats, error = _selection_from(query)
        if error:
            return error
        variables = all_variables("unit", cats)
        if by not in DIMENSIONS:
            return {"status": "error", "message": f"by must be one of {', '.join(DIMENSIONS)}."}
        if metric not in variables:
            return {"status": "error", "message": f"Unknown metric '{metric}'. Use one of "
                                                  f"{', '.join(variables)}."}
        if not len(units):
            return {"status": "no_records", "selection": dataset_counts(units)}
        result = compare_groups(units, variables[metric], by, baseline or None)
        groups = shown_groups(result["groups"], "asc" if order == "asc" else "desc",
                              max(1, min(int(limit or 10), 25)))
        keep = ("rank", "label", "value", "difference", "pct_difference", "share_pct",
                "percentile", "values_present")
        return {
            "status": "success",
            "selection": dataset_counts(units),
            "compared": f"{result['group_count']} {result['dimension_plural']}",
            "metric": {"name": metric, "label": result["metric"]["label"],
                       "unit": result["metric"]["unit"]},
            "group_value": "each group's value is the " + result["aggregation_text"],
            "reference": {"label": result["reference"]["label"],
                          "value": _round(result["reference"]["value"])},
            "groups": [{k: g[k] if k in ("rank", "label", "values_present") else _round(g[k])
                        for k in keep} for g in groups],
            "test": result["test"]["interpretation"],
            "warnings": result["errors"],
            "page": "/analysis/compare",
        }
    except Exception as exc:
        logger.exception("compare_and_rank failed")
        db.session.rollback()
        return {"status": "error", "message": str(exc)}


def assess_traci_impacts(query: str = "", group_by: str = "", limit: int = 10) -> dict:
    """Potential environmental and health impacts of the selected emissions with EPA TRACI: for each impact category (climate change in kg CO2 eq, acid rain in kg SO2 eq, fine particles in kg PM2.5 eq, smog in kg O3 eq) the total, the impact per MWh, each pollutant's share, and optionally totals for each group.

    Args:
        query: Record filters as URL query parameters, e.g. 'year=2020&state=OH'. Empty for all records.
        group_by: Optional grouping: 'state', 'primary_fuel', 'unit_type', 'year' or 'facility'.
        limit: How many groups to return, 1 to 25, largest climate impact first.
    """
    try:
        analysis, units, cats, error = _selection_from(query)
        if error:
            return error
        if not cats:
            return {"status": "error", "message": "No TRACI factors are stored."}
        if not len(units):
            return {"status": "no_records", "selection": dataset_counts(units)}
        categories = [
            {"category": c["label"], "unit": c["unit"], "total": _round(c["total"]),
             "per_mwh": _round(c["intensity"]), "formula": c["formula"],
             "unit_years_used": c["records_used"], "unit_years_left_out": c["records_missing"],
             "pollutant_shares_pct": {p["pollutant_name"]: _round(p["share_pct"])
                                      for p in c["pollutants"]}}
            for c in impact_summary(units, cats)
        ]
        body = {
            "status": "success",
            "traci_version": cats[0].version,
            "selection": dataset_counts(units),
            "categories": categories,
            "note": DISCLAIMER + " A unit-year is left out of a category when one of its "
                    "pollutants was not reported.",
            "page": "/analysis/impacts",
        }
        if group_by:
            if group_by not in IMPACT_GROUPS:
                return {"status": "error",
                        "message": f"group_by must be one of {', '.join(IMPACT_GROUPS)}."}
            rows = sorted_groups(grouped_impacts(units, cats, group_by), "traci_global_climate",
                                 max(1, min(int(limit or 10), 25)))
            body["groups"] = [
                {"group": r["label"],
                 **{cat.short_label: _round(r["impacts"][cat.name]["total"]) for cat in cats}}
                for r in rows
            ]
        return body
    except Exception as exc:
        logger.exception("assess_traci_impacts failed")
        db.session.rollback()
        return {"status": "error", "message": str(exc)}


def get_app_status() -> dict:
    """Describe this copy of the epaData website: the data loaded (counts and years), the most recent imports, imports waiting for review, which API keys and services are set up (yes or no only), and the stored EPA TRACI characterization factors."""
    try:
        years = available_years()
        recent = db.session.scalars(
            select(Dataset).order_by(Dataset.retrieval_date.desc()).limit(5)
        ).all()
        staged = db.session.scalars(
            select(UploadedFile).where(UploadedFile.status == "staged")
            .order_by(UploadedFile.uploaded_at.desc())
        ).all()
        factors = factors_for()
        config = current_app.config
        return {
            "status": "success",
            "data": {
                "datasets": db.session.scalar(select(func.count(Dataset.id))) or 0,
                "annual_records": db.session.scalar(select(func.count(AnnualRecord.id))) or 0,
                "facilities": db.session.scalar(select(func.count(Facility.epa_facility_id))) or 0,
                "units": db.session.scalar(select(func.count(Unit.internal_unit_key))) or 0,
                "reporting_years": years,
            },
            "recent_imports": [
                {"dataset_id": d.id, "name": d.dataset_name, "source": d.data_source,
                 "reporting_year": d.reporting_year, "accepted_records": d.accepted_records,
                 "page": f"/dataset/{d.id}"}
                for d in recent
            ],
            "waiting_for_review": [
                {"file_id": f.id, "name": f.dataset_name or f.original_filename,
                 "source": f.source_type, "page": f"/review/{f.id}"}
                for f in staged
            ],
            "setup": {
                "campd_api_key_saved": bool(config.get("CAMPD_API_KEY")),
                "gemini_api_key_saved": bool(config.get("GEMINI_API_KEY")),
                "gemini_model": config.get("GEMINI_MODEL") or DEFAULT_MODEL,
                "shared_database_configured": bool(config.get("SHARED_DATABASE_URL")),
            },
            "traci": {
                "version": factors[0].version if factors else None,
                "factors": [
                    {"campd_metric": f.campd_metric, "substance": f.pollutant_name,
                     "impact_category": CATEGORY_LABELS.get(f.impact_category, f.impact_category),
                     "factor": f.factor_value, "unit": f.factor_unit}
                    for f in factors
                ],
            },
        }
    except Exception as exc:
        logger.exception("get_app_status failed")
        db.session.rollback()
        return {"status": "error", "message": str(exc)}


# Pages the assistant may open, and file downloads it may offer as links.
PAGES = {
    "main.index": "Dashboard",
    "main.explorer": "Data Explorer",
    "main.compare": "Compare",
    "main.facility_detail": "Facility page",
    "main.unit_detail": "Unit page",
    "main.dataset_detail": "Dataset page",
    "analysis.builder": "Analytical Dataset Builder",
    "analysis.statistics": "Descriptive statistics",
    "analysis.compare": "Compare & rank",
    "analysis.impacts": "TRACI impact assessment",
    "main.data_sources": "Data Sources",
    "main.coverage": "Coverage Dashboard",
    "retrieval.retrieval_form": "Data Retrieval",
    "upload.upload_form": "File Intake & Validation",
    "upload.review": "Review import",
    "download.downloads_page": "Export & Downloads",
    "settings.settings_page": "Settings",
}
DOWNLOADS = {
    "download.records": "Download search results",
    "download.issues": "Download data-quality report",
    "download.provenance": "Download provenance",
    "analysis.download": "Download analytical dataset",
    "analysis.configuration": "Download analysis configuration",
    "analysis.statistics_download": "Download statistics",
    "analysis.compare_download": "Download comparison",
    "analysis.impacts_download": "Download TRACI impacts",
}


def _actions() -> list:
    return g.setdefault("assistant_actions", [])


def _page_problems(endpoint: str, view_args: dict, args: MultiDict) -> tuple[list[str], str]:
    """Problems with a page address, and a label naming what it opens."""
    label = PAGES.get(endpoint) or DOWNLOADS[endpoint]
    if endpoint in ("main.explorer", "download.records"):
        return parse_search(args).errors, label
    if endpoint.startswith("analysis."):
        return parse_analysis(args).errors, label
    if endpoint == "main.facility_detail":
        facility = db.session.get(Facility, view_args["facility_id"])
        if facility is None:
            return [f"There is no facility {view_args['facility_id']} in the database."], label
        return [], f"{facility.facility_name} ({facility.state_code})"
    if endpoint == "main.unit_detail":
        unit = db.session.get(Unit, view_args["unit_key"])
        if unit is None:
            return [f"There is no unit with key {view_args['unit_key']}."], label
        return [], f"{unit.facility.facility_name} unit {unit.epa_unit_id}"
    if endpoint == "main.dataset_detail":
        dataset = db.session.get(Dataset, view_args["dataset_id"])
        if dataset is None:
            return [f"There is no dataset {view_args['dataset_id']}."], label
        return [], f"Dataset: {dataset.dataset_name}"
    if endpoint == "upload.review":
        staged = db.session.get(UploadedFile, view_args["file_id"])
        if staged is None or staged.status != "staged":
            return [f"File {view_args['file_id']} is not waiting for review."], label
    return [], label


def navigate(path: str, query: str = "", open_now: bool = True) -> dict:
    """Open a page of the epaData website for the user, with filters, or give them a download link.

    Args:
        path: The page's path on this site, e.g. '/explorer', '/analysis', '/facility/6002', '/retrieve', '/settings'.
        query: The page's URL query parameters, e.g. 'state=OH&primary_fuel=coal&year_min=2019&sort=co2_mass'.
        open_now: True to take the user to the page right after your answer; False to only show them a link.
    """
    try:
        path = (path or "").strip()
        if "?" in path:
            path, extra = path.split("?", 1)
            query = "&".join(part for part in (extra, query or "") if part)
        if not path.startswith("/") or path.startswith("//") or "://" in path or "\\" in path:
            return {"status": "error", "message": "Use a path on this site that starts with '/'."}
        try:
            endpoint, view_args = current_app.url_map.bind("localhost").match(path, method="GET")
        except HTTPException:
            return {"status": "error", "message": f"There is no page at {path}."}
        if endpoint not in PAGES and endpoint not in DOWNLOADS:
            return {"status": "error",
                    "message": f"{path} is not a page users open. Use one of the pages listed."}

        pairs = [(k, v) for k, v in parse_qsl((query or "")[:2000]) if k and v]
        problems, label = _page_problems(endpoint, view_args, MultiDict(pairs))
        if problems:
            return {"status": "error", "message": " ".join(problems)}

        url = path + (f"?{urlencode(pairs)}" if pairs else "")
        is_download = endpoint in DOWNLOADS
        _actions().append({
            "type": "download" if is_download else "navigate",
            "url": url,
            "label": label,
            "open_now": bool(open_now) and not is_download,
        })
        return {
            "status": "success",
            "url": url,
            "opens": label,
            "note": "A download link is shown to the user." if is_download else (
                "The page opens right after your answer." if open_now
                else "A link to the page is shown to the user."),
        }
    except Exception as exc:
        logger.exception("navigate failed")
        db.session.rollback()
        return {"status": "error", "message": str(exc)}


# ---------------------------------------------------------------------------
# Talking to Gemini
# ---------------------------------------------------------------------------


TOOLS = (query_epa_database, summarize_records, compare_and_rank, assess_traci_impacts,
         get_app_status, navigate)


def system_instruction() -> str:
    years = available_years()
    if not years:
        data = " The database is empty, so explain that data must be loaded first."
    else:
        data = (f" Reporting years with data: {', '.join(str(y) for y in years)}."
                f" When the user gives no year, use the latest one, {years[-1]}.")
    return AI_RULES + data + " " + SITE_GUIDE + " Glossary: " + glossary_text() + "."


def screen_text(page: Any) -> Optional[str]:
    """The on-screen snapshot from the browser, as text for the model."""
    if not isinstance(page, dict) or not page:
        return None
    text = json.dumps(page, ensure_ascii=False, default=str)
    if len(text) > MAX_PAGE_CONTEXT:
        text = text[:MAX_PAGE_CONTEXT] + " ...(cut short)"
    return "SCREEN (the page the user is looking at right now):\n" + text


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


def ask(prompt: str, raw_history=None, page=None) -> tuple[str, list[dict]]:
    """Answer one question, using the conversation so far and what is on the
    user's screen. Returns the answer and the pages or downloads to offer.
    Raises AssistantError."""
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
        tools=list(TOOLS),
        system_instruction=system_instruction(),
        temperature=0.2,
    )
    screen = screen_text(page)
    message = [screen, prompt] if screen else prompt

    last_error: Optional[Exception] = None
    for attempt in range(3):
        g.assistant_actions = []  # pages or downloads the tools offer during this answer
        try:
            response = chat.send_message(message, config=config)
        except Exception as error:  # noqa: BLE001 - the SDK raises many error types
            last_error = error
            current_app.logger.error("AI attempt %d failed: %s", attempt + 1, error)
            if "503" in str(error) or "UNAVAILABLE" in str(error):
                time.sleep(2 * (attempt + 1))  # wait 2s, 4s, 6s, then try again
                continue
            break
        return response.text or "No response generated.", list(g.assistant_actions)
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
