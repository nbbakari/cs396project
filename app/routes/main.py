"""Dashboard, data explorer, comparison and detail pages."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from flask import Blueprint, flash, redirect, render_template, request, url_for
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


@main_bp.route("/explorer")
def explorer():
    """Search, filter, rank and page through annual records."""
    search = parse_search(request.args)
    results = run_search(search)
    choices = filter_choices()
    latest_year = choices["years"][0] if choices["years"] else None

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
