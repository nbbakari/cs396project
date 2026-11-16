"""Dashboard, data explorer, comparison and detail pages."""

from __future__ import annotations

import math

from collections import defaultdict
from typing import Any

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for
from sqlalchemy import delete, func, select

from app import db
from app.models import (
    AnnualRecord,
    Dataset,
    DataProvenance,
    DataQualityIssue,
    Facility,
    Unit,
)
from app.services.describe import interpret
from app.services.ingest import query_params_of, remove_stored_file, upload_path
from app.services.search import (
    GROUPS,
    METRIC_LABELS,
    METRICS,
    PER_PAGE_CHOICES,
    RANGE_FILTERS,
    filter_choices,
    parse_search,
    run_search,
)
from app.services.traci import SOURCE_URL as TRACI_SOURCE_URL
from app.services.traci import factors_for

main_bp = Blueprint("main", __name__)

# Eight is the categorical palette size; a ninth series would need a reused color.
MAX_COMPARE = 8
ISSUES_PER_PAGE = 50


def _int_list(name: str) -> list[int]:
    values: list[int] = []
    for raw in request.args.getlist(name):
        for part in raw.split(","):
            part = part.strip()
            if part.isdigit() and int(part) not in values:
                values.append(int(part))
    return values


def _coverage_stats() -> dict[str, Any]:
    """Totals for the coverage page: facilities, units, records, states, years."""
    year_min, year_max = db.session.execute(
        select(func.min(AnnualRecord.reporting_year), func.max(AnnualRecord.reporting_year))
    ).one()
    return {
        "facility_count": db.session.scalar(select(func.count(Facility.epa_facility_id))) or 0,
        "unit_count": db.session.scalar(select(func.count(Unit.internal_unit_key))) or 0,
        "record_count": db.session.scalar(select(func.count(AnnualRecord.id))) or 0,
        "state_count": db.session.scalar(
            select(func.count(func.distinct(Facility.state_code))).where(Facility.state_code != "")
        ) or 0,
        "year_min": year_min,
        "year_max": year_max,
    }


@main_bp.route("/data-sources")
def data_sources():
    """Where the data comes from: CAMPD, the TRACI factors, and every imported dataset."""
    datasets = db.session.scalars(
        select(Dataset).order_by(Dataset.retrieval_date.desc())
    ).all()
    source_counts = dict(
        db.session.execute(
            select(DataProvenance.source_type, func.count(DataProvenance.id)).group_by(
                DataProvenance.source_type
            )
        ).all()
    )
    factors = factors_for()
    return render_template(
        "data_sources.html",
        datasets=datasets,
        source_counts=source_counts,
        traci_version=factors[0].version if factors else None,
        traci_count=len(factors),
        traci_url=TRACI_SOURCE_URL,
    )


@main_bp.route("/coverage")
def coverage():
    """Coverage dashboard: totals plus records by year and facilities by state."""
    by_year = [
        {"year": year, "count": count}
        for year, count in db.session.execute(
            select(AnnualRecord.reporting_year, func.count(AnnualRecord.id))
            .group_by(AnnualRecord.reporting_year)
            .order_by(AnnualRecord.reporting_year)
        ).all()
    ]
    by_state = [
        {"state": state, "count": count}
        for state, count in db.session.execute(
            select(Facility.state_code, func.count(Facility.epa_facility_id))
            .where(Facility.state_code != "")
            .group_by(Facility.state_code)
            .order_by(func.count(Facility.epa_facility_id).desc())
            .limit(10)
        ).all()
    ]
    return render_template(
        "coverage.html", stats=_coverage_stats(), by_year=by_year, by_state=by_state
    )


@main_bp.route("/")
def index():
    """Dashboard: introduction, coverage, data sources and recent imports."""
    facility_count = db.session.scalar(select(func.count(Facility.epa_facility_id))) or 0
    unit_count = db.session.scalar(select(func.count(Unit.internal_unit_key))) or 0
    record_count = db.session.scalar(select(func.count(AnnualRecord.id))) or 0
    state_count = (
        db.session.scalar(
            select(func.count(func.distinct(Facility.state_code))).where(
                Facility.state_code != ""
            )
        )
        or 0
    )
    year_min, year_max = db.session.execute(
        select(func.min(AnnualRecord.reporting_year), func.max(AnnualRecord.reporting_year))
    ).one()

    per_year = db.session.execute(
        select(
            AnnualRecord.reporting_year,
            func.count(AnnualRecord.id),
            func.count(func.distinct(Unit.facility_id)),
            func.sum(AnnualRecord.co2_mass),
        )
        .join(Unit, Unit.internal_unit_key == AnnualRecord.unit_id)
        .group_by(AnnualRecord.reporting_year)
        .order_by(AnnualRecord.reporting_year.desc())
    ).all()

    source_counts = dict(
        db.session.execute(
            select(DataProvenance.source_type, func.count(DataProvenance.id)).group_by(
                DataProvenance.source_type
            )
        ).all()
    )
    issue_count = db.session.scalar(
        select(func.count(DataQualityIssue.id)).where(DataQualityIssue.severity == "rejected")
    ) or 0

    coverage = {
        "facility_count": facility_count,
        "unit_count": unit_count,
        "record_count": record_count,
        "state_count": state_count,
        "year_min": year_min,
        "year_max": year_max,
        "rejected_rows": issue_count,
    }
    datasets = db.session.scalars(
        select(Dataset).order_by(Dataset.retrieval_date.desc()).limit(10)
    ).all()
    dataset_total = db.session.scalar(select(func.count(Dataset.id))) or 0

    return render_template(
        "index.html",
        coverage=coverage,
        per_year=per_year,
        datasets=datasets,
        dataset_total=dataset_total,
        source_counts=source_counts,
    )


def _presets(year: Any) -> list[tuple[str, str, dict[str, Any]]]:
    """Example searches from the project specification, as (group, label, params)."""
    if year is None:
        return []
    y = str(year)
    return [
        ("Multi-criteria", f"Coal units in KY, {y}, CO₂ > 500,000 tons",
         {"state": "KY", "year": y, "primary_fuel": "coal", "co2_min": "500000",
          "sort": "co2_mass"}),
        ("Multi-criteria", f"Units running > 5,000 h in {y}",
         {"year": y, "op_time_min": "5000", "sort": "operating_time"}),
        ("Multi-criteria", f"Gross load 100,000–1,000,000 MWh ({y})",
         {"year": y, "gross_load_min": "100000", "gross_load_max": "1000000",
          "sort": "gross_load"}),
        ("Multi-criteria", f"Heat input > 5,000,000 MMBtu ({y})",
         {"year": y, "heat_input_min": "5000000", "sort": "heat_input"}),
        ("Multi-criteria", f"SO₂ < 500 tons, coal units ({y})",
         {"year": y, "primary_fuel": "coal", "so2_max": "500", "sort": "so2_mass"}),
        ("Top / bottom N", f"Top 10 facilities by CO₂ ({y})",
         {"level": "facility", "year": y, "sort": "co2_mass", "limit": "10"}),
        ("Top / bottom N", f"Top 20 units by gross generation ({y})",
         {"year": y, "sort": "gross_load", "limit": "20"}),
        ("Top / bottom N", f"Top 10 units by heat input ({y})",
         {"year": y, "sort": "heat_input", "limit": "10"}),
        ("Top / bottom N", f"Lowest SO₂, units running > 5,000 h ({y})",
         {"year": y, "op_time_min": "5000", "sort": "so2_mass", "order": "asc",
          "limit": "10"}),
        ("Top / bottom N", f"Lowest NOx, units running > 5,000 h ({y})",
         {"year": y, "op_time_min": "5000", "sort": "nox_mass", "order": "asc",
          "limit": "10"}),
        ("Group and rank", f"Top CO₂ facility in each state ({y})",
         {"level": "facility", "year": y, "group_by": "state", "per_group": "1",
          "sort": "co2_mass"}),
        ("Group and rank", f"Top 10 coal units in Kentucky ({y})",
         {"state": "KY", "primary_fuel": "coal", "year": y, "sort": "co2_mass",
          "limit": "10"}),
        ("Group and rank", f"Highest-generation natural-gas units ({y})",
         {"primary_fuel": "natural gas", "year": y, "sort": "gross_load",
          "limit": "10"}),
        ("Group and rank", f"Top 3 units by CO₂ for each fuel ({y})",
         {"year": y, "group_by": "primary_fuel", "per_group": "3",
          "sort": "co2_mass"}),
        ("Historical", "James H Miller Jr unit 1, 2015–2025",
         {"facility_id": "6002", "unit_id": "1", "year_min": "2015",
          "year_max": "2025", "sort": "reporting_year", "order": "asc"}),
    ]


def _describe_examples(year: Any) -> list[str]:
    """Plain-English searches offered under the description box."""
    y = str(year or 2024)
    return [
        "Find coal units in Kentucky with high CO2 emissions",
        f"Top 10 facilities by CO2 in {y}",
        f"Highest NOx emitter in each state in {y}",
        "Coal units in Ohio without SCR",
        "Gas units with SO2 below 500 tons since 2020",
        "Plants owned by Duke Energy in North Carolina",
    ]


@main_bp.route("/explorer")
def explorer():
    """Search, filter, rank and page through annual records.

    ``q`` holds a plain-English description. On its own it is interpreted and
    redirected to the equivalent filter URL, which keeps ``q`` so the page can
    show how the description was read.
    """
    choices = filter_choices()
    latest_year = choices["years"][0] if choices["years"] else None

    interpretation = None
    interpretation_edited = False
    description = (request.args.get("q") or "").strip()
    if description:
        interpretation = interpret(description, latest_year=latest_year)
        if interpretation.params and set(request.args) == {"q"}:
            return redirect(url_for("main.explorer", q=interpretation.text, **interpretation.params))
        # Sorting or paging keeps q; flag a search changed in other ways.
        ignore = {"q", "page", "per_page", "sort", "order"}
        applied = {k: v for k, v in request.args.items() if k not in ignore}
        expected = {k: v for k, v in interpretation.params.items() if k not in ignore}
        interpretation_edited = applied != expected

    search = parse_search(request.args)
    results = run_search(search)

    # Open the advanced panel whenever the URL uses something in it, so a
    # shared link shows the user every filter that shaped the result.
    advanced_keys = {f"{p}_{s}" for p in RANGE_FILTERS for s in ("min", "max")}
    advanced_keys |= {"level", "limit", "group_by", "per_group", "year_min", "year_max",
                      "secondary_fuel", "owner", "so2_control", "nox_control",
                      "pm_control", "hg_control", "program", "county", "unit", "dataset"}
    advanced_open = any(request.args.get(k) for k in advanced_keys)

    return render_template(
        "explorer.html",
        search=search,
        results=results,
        choices=choices,
        presets=_presets(latest_year),
        metric_labels=METRIC_LABELS,
        per_page_choices=PER_PAGE_CHOICES,
        groups=GROUPS,
        range_filters=RANGE_FILTERS,
        advanced_open=advanced_open,
        latest_year=latest_year,
        interpretation=interpretation,
        interpretation_edited=interpretation_edited,
        describe_examples=_describe_examples(latest_year),
    )


@main_bp.route("/compare")
def compare():
    """Compare several units or facilities year by year on one metric."""
    metric = request.args.get("metric", "co2_mass")
    if metric not in METRICS:
        metric = "co2_mass"
    unit_keys = _int_list("unit")[:MAX_COMPARE]
    facility_ids = _int_list("facility_id")[:MAX_COMPARE]
    if len(_int_list("unit")) + len(_int_list("facility_id")) > MAX_COMPARE:
        flash(f"Comparisons are limited to {MAX_COMPARE} units or facilities.", "warning")

    column = getattr(AnnualRecord, metric)
    entities: list[dict[str, Any]] = []
    values: dict[str, dict[int, float]] = defaultdict(dict)

    if unit_keys:
        units = db.session.scalars(
            select(Unit).where(Unit.internal_unit_key.in_(unit_keys))
        ).all()
        for unit in sorted(units, key=lambda u: unit_keys.index(u.internal_unit_key)):
            key = f"u{unit.internal_unit_key}"
            entities.append(
                {
                    "key": key,
                    "kind": "Unit",
                    "label": f"{unit.facility.facility_name} ({unit.facility.state_code}) unit {unit.epa_unit_id}",
                    "url": url_for("main.unit_detail", unit_key=unit.internal_unit_key),
                    "detail": f"{unit.unit_type} · {unit.primary_fuel}",
                }
            )
        for unit_key, year, value in db.session.execute(
            select(AnnualRecord.unit_id, AnnualRecord.reporting_year, column).where(
                AnnualRecord.unit_id.in_(unit_keys)
            )
        ):
            values[f"u{unit_key}"][year] = value

    if facility_ids:
        facilities = db.session.scalars(
            select(Facility).where(Facility.epa_facility_id.in_(facility_ids))
        ).all()
        for facility in sorted(facilities, key=lambda f: facility_ids.index(f.epa_facility_id)):
            entities.append(
                {
                    "key": f"f{facility.epa_facility_id}",
                    "kind": "Facility",
                    "label": f"{facility.facility_name} ({facility.state_code})",
                    "url": url_for("main.facility_detail", facility_id=facility.epa_facility_id),
                    "detail": f"Facility ID {facility.epa_facility_id}, all units",
                }
            )
        # Operating hours do not add across units; average them instead.
        aggregate = func.avg(column) if metric == "operating_time" else func.sum(column)
        for facility_id, year, value in db.session.execute(
            select(Unit.facility_id, AnnualRecord.reporting_year, aggregate)
            .join(AnnualRecord, AnnualRecord.unit_id == Unit.internal_unit_key)
            .where(Unit.facility_id.in_(facility_ids))
            .group_by(Unit.facility_id, AnnualRecord.reporting_year)
        ):
            values[f"f{facility_id}"][year] = value

    years = sorted({year for series in values.values() for year in series})
    table = [(year, [values[e["key"]].get(year) for e in entities]) for year in years]
    chart = {
        "labels": years,
        "datasets": [
            {"label": e["label"], "data": [values[e["key"]].get(y) for y in years]}
            for e in entities
        ],
    }
    return render_template(
        "compare.html",
        metric=metric,
        metric_labels=METRIC_LABELS,
        entities=entities,
        table=table,
        chart=chart,
        unit_keys=unit_keys,
        facility_ids=facility_ids,
    )


@main_bp.route("/facility/<int:facility_id>")
def facility_detail(facility_id: int):
    """One facility: location, its units, and year-by-year totals."""
    facility = db.get_or_404(Facility, facility_id)
    units = db.session.execute(
        select(
            Unit,
            func.count(AnnualRecord.id),
            func.min(AnnualRecord.reporting_year),
            func.max(AnnualRecord.reporting_year),
        )
        .outerjoin(AnnualRecord, AnnualRecord.unit_id == Unit.internal_unit_key)
        .where(Unit.facility_id == facility_id)
        .group_by(Unit.internal_unit_key)
        .order_by(Unit.epa_unit_id)
    ).all()
    totals = db.session.execute(
        select(
            AnnualRecord.reporting_year,
            func.count(AnnualRecord.id),
            func.sum(AnnualRecord.gross_load),
            func.sum(AnnualRecord.heat_input),
            func.sum(AnnualRecord.co2_mass),
            func.sum(AnnualRecord.so2_mass),
            func.sum(AnnualRecord.nox_mass),
        )
        .join(Unit, Unit.internal_unit_key == AnnualRecord.unit_id)
        .where(Unit.facility_id == facility_id)
        .group_by(AnnualRecord.reporting_year)
        .order_by(AnnualRecord.reporting_year)
    ).all()
    chart = {
        "labels": [row[0] for row in totals],
        "series": {
            "co2_mass": [row[4] for row in totals],
            "so2_mass": [row[5] for row in totals],
            "nox_mass": [row[6] for row in totals],
            "gross_load": [row[2] for row in totals],
            "heat_input": [row[3] for row in totals],
        },
    }
    # Each unit lists its owners and operators "|"-separated; show every
    # distinct entry once, in the order the units list them.
    owners = list(dict.fromkeys(
        part.strip()
        for unit, *_ in units
        for part in (unit.owner_operator or "").split("|")
        if part.strip()
    ))
    return render_template(
        "facility_detail.html",
        facility=facility,
        units=units,
        owners=owners,
        totals=list(reversed(totals)),
        chart=chart,
        metric_labels=METRIC_LABELS,
        unit_keys=[unit.internal_unit_key for unit, *_ in units],
    )


@main_bp.route("/unit/<int:unit_key>")
def unit_detail(unit_key: int):
    """One unit: identification, fuel and controls, and its full history."""
    unit = db.get_or_404(Unit, unit_key)
    records = db.session.execute(
        select(AnnualRecord, Dataset)
        .join(Dataset, Dataset.id == AnnualRecord.dataset_id)
        .where(AnnualRecord.unit_id == unit_key)
        .order_by(AnnualRecord.reporting_year.desc())
    ).all()
    ascending = list(reversed(records))
    chart = {
        "labels": [record.reporting_year for record, _ in ascending],
        "series": {
            metric: [getattr(record, metric) for record, _ in ascending] for metric in METRICS
        },
    }
    return render_template(
        "unit_detail.html",
        unit=unit,
        facility=unit.facility,
        records=records,
        chart=chart,
        metric_labels=METRIC_LABELS,
    )


@main_bp.route("/dataset/<int:dataset_id>")
def dataset_detail(dataset_id: int):
    """One import: provenance, record counts and its data-quality report."""
    dataset = db.get_or_404(Dataset, dataset_id)
    severity = request.args.get("severity")
    if severity not in ("rejected", "warning"):
        severity = None
    page = max(1, request.args.get("page", 1, type=int))

    counts = db.session.execute(
        select(DataQualityIssue.severity, DataQualityIssue.issue_type, func.count())
        .where(DataQualityIssue.dataset_id == dataset_id)
        .group_by(DataQualityIssue.severity, DataQualityIssue.issue_type)
        .order_by(DataQualityIssue.severity, func.count().desc())
    ).all()

    issue_query = select(DataQualityIssue).where(DataQualityIssue.dataset_id == dataset_id)
    if severity:
        issue_query = issue_query.where(DataQualityIssue.severity == severity)
    issue_total = db.session.scalar(
        select(func.count()).select_from(issue_query.subquery())
    ) or 0
    issues = db.session.scalars(
        issue_query.order_by(DataQualityIssue.source_row, DataQualityIssue.id)
        .offset((page - 1) * ISSUES_PER_PAGE)
        .limit(ISSUES_PER_PAGE)
    ).all()

    per_year = db.session.execute(
        select(AnnualRecord.reporting_year, func.count(AnnualRecord.id))
        .where(AnnualRecord.dataset_id == dataset_id)
        .group_by(AnnualRecord.reporting_year)
        .order_by(AnnualRecord.reporting_year)
    ).all()

    stored = dataset.uploaded_file
    return render_template(
        "dataset_detail.html",
        dataset=dataset,
        provenance=dataset.provenance,
        query_params=query_params_of(dataset.provenance) if dataset.provenance else {},
        stored=stored,
        original_available=bool(stored and upload_path(stored.stored_filename).exists()),
        counts=counts,
        issues=issues,
        issue_total=issue_total,
        severity=severity,
        page=page,
        issue_pages=max(1, -(-issue_total // ISSUES_PER_PAGE)),
        per_year=per_year,
    )


@main_bp.route("/dataset/<int:dataset_id>/delete", methods=["POST"])
def delete_dataset(dataset_id: int):
    """Delete an imported dataset, its records, its report and its stored file.

    Facilities and units are shared between datasets, so only the ones left
    with no records at all are removed afterwards.
    """
    dataset = db.get_or_404(Dataset, dataset_id)
    name = dataset.dataset_name
    count = dataset.accepted_records
    if dataset.uploaded_file is not None:
        remove_stored_file(dataset.uploaded_file)
    db.session.delete(dataset)
    db.session.flush()

    db.session.execute(
        delete(Unit).where(
            ~select(AnnualRecord.id)
            .where(AnnualRecord.unit_id == Unit.internal_unit_key)
            .exists()
        ),
        execution_options={"synchronize_session": False},
    )
    db.session.execute(
        delete(Facility).where(
            ~select(Unit.internal_unit_key)
            .where(Unit.facility_id == Facility.epa_facility_id)
            .exists()
        ),
        execution_options={"synchronize_session": False},
    )
    db.session.commit()
    flash(f"Deleted dataset '{name}' and its {count:,} record(s).", "info")
    return redirect(url_for("main.index"))
# ---------------------------------------------------------------------------
# Visualization dashboard (Plotly): one page plus JSON endpoints it reads
# ---------------------------------------------------------------------------
VIZ_METRICS: dict[str, tuple[str, str]] = {
    "co2_mass": ("CO₂ emissions", "short tons"),
    "so2_mass": ("SO₂ emissions", "short tons"),
    "nox_mass": ("NOₓ emissions", "short tons"),
    "gross_load": ("Gross load", "MWh"),
    "heat_input": ("Heat input", "MMBtu"),
    "operating_time": ("Operating time", "hours"),
    "co2_intensity": ("CO₂ intensity", "short tons CO₂ per MWh"),
}
CORR_METRICS = ["operating_time", "gross_load", "heat_input", "co2_mass", "so2_mass", "nox_mass"]

# TRACI-style impact categories. Mass columns are short tons; "scale" converts to tonnes.
# CHECK these factors against the EPA TRACI 2.1 table before putting numbers in the report.
TRACI_CATEGORIES = {
    "gwp": {
        "label": "Global warming potential (100-year)",
        "unit": "tonnes CO₂-eq",
        "factors": {"co2_mass": 1.0},
        "scale": 0.90718474,
    },
    "acidification": {
        "label": "Acidification potential",
        "unit": "tonnes SO₂-eq",
        "factors": {"so2_mass": 1.0, "nox_mass": 0.7},
        "scale": 0.90718474,
    },
}


def _viz_metric(name: str | None, default: str = "co2_mass") -> str:
    return name if name in VIZ_METRICS else default


def _viz_meta(metric: str) -> dict[str, str]:
    label, unit = VIZ_METRICS[metric]
    return {"metric": metric, "label": label, "unit": unit}


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _thin(values: list[float], cap: int = 5000) -> list[float]:
    if len(values) <= cap:
        return values
    return values[:: -(-len(values) // cap)]


def _viz_row_expr(metric: str):
    """Per-record value (no aggregation)."""
    if metric == "co2_intensity":
        return (AnnualRecord.co2_mass * 1.0) / func.nullif(AnnualRecord.gross_load, 0)
    return getattr(AnnualRecord, metric)


def _viz_agg_expr(metric: str):
    """Aggregated value: sums, except hours (average) and intensity (ratio of sums)."""
    if metric == "co2_intensity":
        return (func.sum(AnnualRecord.co2_mass) * 1.0) / func.nullif(
            func.sum(AnnualRecord.gross_load), 0
        )
    column = getattr(AnnualRecord, metric)
    return func.avg(column) if metric == "operating_time" else func.sum(column)


def _viz_base(*columns):
    """SELECT ... FROM records JOIN units JOIN facilities, with the shared page filters."""
    stmt = (
        select(*columns)
        .select_from(AnnualRecord)
        .join(Unit, Unit.internal_unit_key == AnnualRecord.unit_id)
        .join(Facility, Facility.epa_facility_id == Unit.facility_id)
    )
    state = request.args.get("state", "").strip().upper()
    fuel = request.args.get("fuel", "").strip()
    year_min = request.args.get("year_min", type=int)
    year_max = request.args.get("year_max", type=int)
    if state:
        stmt = stmt.where(Facility.state_code == state)
    if fuel:
        stmt = stmt.where(Unit.primary_fuel == fuel)
    if year_min is not None:
        stmt = stmt.where(AnnualRecord.reporting_year >= year_min)
    if year_max is not None:
        stmt = stmt.where(AnnualRecord.reporting_year <= year_max)
    return stmt


def _pearson(xs, ys) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / math.sqrt(sxx * syy)


@main_bp.route("/visualizations")
def visualizations():
    """Interactive Plotly dashboard."""
    states = [
        s for (s,) in db.session.execute(
            select(Facility.state_code).where(Facility.state_code != "").distinct()
            .order_by(Facility.state_code)
        )
    ]
    fuels = [
        f for (f,) in db.session.execute(
            select(Unit.primary_fuel)
            .where(Unit.primary_fuel.is_not(None), Unit.primary_fuel != "")
            .distinct().order_by(Unit.primary_fuel)
        )
    ]
    years = [
        y for (y,) in db.session.execute(
            select(AnnualRecord.reporting_year).distinct().order_by(AnnualRecord.reporting_year)
        )
    ]
    return render_template(
        "visualizations.html",
        states=states, fuels=fuels, years=years, years_desc=list(reversed(years)),
        metrics=VIZ_METRICS,
    )


@main_bp.route("/api/viz/timeseries")
def viz_timeseries():
    """Line chart data: a metric by year, optionally split by state or fuel."""
    metric = _viz_metric(request.args.get("metric"))
    group = request.args.get("group", "none")
    value = _viz_agg_expr(metric)
    group_col = {"state": Facility.state_code, "fuel": Unit.primary_fuel}.get(group)

    if group_col is None:
        rows = db.session.execute(
            _viz_base(AnnualRecord.reporting_year, value)
            .group_by(AnnualRecord.reporting_year)
            .order_by(AnnualRecord.reporting_year)
        ).all()
        series = [{
            "name": "All selected units",
            "x": [r[0] for r in rows],
            "y": [_num(r[1]) for r in rows],
        }]
    else:
        rows = db.session.execute(
            _viz_base(group_col, AnnualRecord.reporting_year, value)
            .group_by(group_col, AnnualRecord.reporting_year)
            .order_by(AnnualRecord.reporting_year)
        ).all()
        grouped: dict[str, dict[str, list]] = defaultdict(lambda: {"x": [], "y": []})
        for name, year, v in rows:
            key = name or "Unknown"
            grouped[key]["x"].append(year)
            grouped[key]["y"].append(_num(v))
        ranked = sorted(grouped, key=lambda n: sum(v or 0 for v in grouped[n]["y"]), reverse=True)[:8]
        series = [{"name": n, **grouped[n]} for n in ranked]

    return jsonify({**_viz_meta(metric), "series": series})


@main_bp.route("/api/viz/bar")
def viz_bar():
    """Bar chart data: top N states, fuels or facilities for a metric."""
    metric = _viz_metric(request.args.get("metric"))
    by = request.args.get("by", "state")
    limit = min(max(request.args.get("limit", 10, type=int), 1), 25)
    year = request.args.get("year", type=int)
    value = _viz_agg_expr(metric)

    if by == "facility":
        stmt = _viz_base(Facility.facility_name, Facility.state_code, value).group_by(
            Facility.epa_facility_id, Facility.facility_name, Facility.state_code
        )
    else:
        by = "fuel" if by == "fuel" else "state"
        col = Unit.primary_fuel if by == "fuel" else Facility.state_code
        stmt = _viz_base(col, value).group_by(col)
    if year is not None:
        stmt = stmt.where(AnnualRecord.reporting_year == year)

    rows = db.session.execute(stmt.order_by(value.desc().nulls_last()).limit(limit)).all()
    if by == "facility":
        labels = [f"{r[0]} ({r[1]})" for r in rows]
        values = [_num(r[2]) for r in rows]
    else:
        labels = [r[0] or "Unknown" for r in rows]
        values = [_num(r[1]) for r in rows]
    return jsonify({**_viz_meta(metric), "by": by, "year": year, "labels": labels, "values": values})


@main_bp.route("/api/viz/distribution")
def viz_distribution():
    """Histogram / box plot data: unit-year values for one metric (zeros excluded)."""
    metric = _viz_metric(request.args.get("metric"))
    year = request.args.get("year", type=int)
    value = _viz_row_expr(metric)
    stmt = _viz_base(Unit.primary_fuel, value).where(value.is_not(None))
    if year is not None:
        stmt = stmt.where(AnnualRecord.reporting_year == year)

    by_fuel: dict[str, list[float]] = defaultdict(list)
    for fuel, v in db.session.execute(stmt):
        number = _num(v)
        if number is not None and number > 0:
            by_fuel[fuel or "Unknown"].append(number)
    everything = [v for vals in by_fuel.values() for v in vals]
    top = sorted(by_fuel, key=lambda f: len(by_fuel[f]), reverse=True)[:6]
    return jsonify({
        **_viz_meta(metric), "year": year, "count": len(everything),
        "values": _thin(everything),
        "groups": {f: _thin(by_fuel[f]) for f in top},
    })


@main_bp.route("/api/viz/scatter")
def viz_scatter():
    """Scatter plot data: one point per unit-year for two metrics."""
    x_metric = _viz_metric(request.args.get("x"), "heat_input")
    y_metric = _viz_metric(request.args.get("y"), "co2_mass")
    year = request.args.get("year", type=int)
    xe, ye = _viz_row_expr(x_metric), _viz_row_expr(y_metric)
    stmt = _viz_base(
        Facility.facility_name, Unit.epa_unit_id, Unit.primary_fuel,
        AnnualRecord.reporting_year, xe, ye,
    ).where(xe.is_not(None), ye.is_not(None))
    if year is not None:
        stmt = stmt.where(AnnualRecord.reporting_year == year)

    points: dict[str, list] = {"name": [], "fuel": [], "x": [], "y": []}
    for name, unit_id, fuel, yr, xv, yv in db.session.execute(stmt.limit(4000)):
        xv, yv = _num(xv), _num(yv)
        if xv is None or yv is None:
            continue
        points["name"].append(f"{name} unit {unit_id} ({yr})")
        points["fuel"].append(fuel or "Unknown")
        points["x"].append(xv)
        points["y"].append(yv)
    return jsonify({"x_meta": _viz_meta(x_metric), "y_meta": _viz_meta(y_metric),
                    "year": year, "points": points})


@main_bp.route("/api/viz/correlation")
def viz_correlation():
    """Pearson correlation matrix across the numeric record metrics."""
    year = request.args.get("year", type=int)
    stmt = _viz_base(*[getattr(AnnualRecord, m) for m in CORR_METRICS])
    if year is not None:
        stmt = stmt.where(AnnualRecord.reporting_year == year)
    rows = [[_num(v) for v in row] for row in db.session.execute(stmt)]
    rows = [r for r in rows if all(v is not None for v in r)]
    columns = [list(c) for c in zip(*rows)] if rows else [[] for _ in CORR_METRICS]
    matrix = [[_pearson(a, b) for b in columns] for a in columns]
    labels = [f"{VIZ_METRICS[m][0]}" for m in CORR_METRICS]
    return jsonify({"labels": labels, "matrix": matrix, "count": len(rows), "year": year})


@main_bp.route("/api/viz/traci")
def viz_traci():
    """TRACI-style impact estimates by year, from CO2/SO2/NOx masses."""
    stmt = (
        _viz_base(
            AnnualRecord.reporting_year,
            func.sum(AnnualRecord.co2_mass),
            func.sum(AnnualRecord.so2_mass),
            func.sum(AnnualRecord.nox_mass),
        )
        .group_by(AnnualRecord.reporting_year)
        .order_by(AnnualRecord.reporting_year)
    )
    rows = db.session.execute(stmt).all()
    index = {"co2_mass": 1, "so2_mass": 2, "nox_mass": 3}
    categories = []
    for key, cfg in TRACI_CATEGORIES.items():
        values = [
            sum((_num(r[index[m]]) or 0) * f for m, f in cfg["factors"].items()) * cfg["scale"]
            for r in rows
        ]
        categories.append({"key": key, "label": cfg["label"], "unit": cfg["unit"], "values": values})
    return jsonify({
        "years": [r[0] for r in rows],
        "categories": categories,
        "note": "Estimates computed from reported CO₂, SO₂ and NOₓ mass using the characterization "
                "factors in TRACI_CATEGORIES (the NOₓ acidification factor is approximate). "
                "Verify against the EPA TRACI 2.1 table.",
    })
