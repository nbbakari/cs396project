"""Phase 2 pages: the analytical dataset builder and its downloads.

The builder chooses the records for an analysis with the data explorer's URL
parameters, so every selection is a link that can be bookmarked, shared or
saved as a configuration file and loaded again.
"""

from __future__ import annotations

import io
import json
import math
from datetime import datetime
from typing import Any

from flask import Blueprint, Response, render_template, request
from openpyxl import Workbook

from app.services.analysis import (
    DERIVED,
    MEASURED,
    analysis_frame,
    categorical_summary,
    dataset_counts,
    in_selection,
    metadata,
    missing_summary,
    numeric_variables,
    parse_analysis,
    selected_units,
)
from app.services.exports import FORMATS, requested_format, tabular_response, workbook_response
from app.services.impacts import (
    DISCLAIMER,
    IMPACT_GROUPS,
    SHORT_TON_KG,
    grouped_impacts,
    impact_summary,
    parse_impact_options,
    sorted_groups,
    worked_example,
)
from app.services.impacts import LIMITS as IMPACT_LIMITS
from app.services.search import filter_choices
from app.services.statistics import (
    DIMENSIONS,
    LIMITS,
    MAIN_STATISTICS,
    STATISTICS,
    all_variables,
    compare_groups,
    describe,
    describe_grouped,
    level_frame,
    load_selection,
    parse_compare_options,
    parse_statistics_options,
    shown_groups,
)
from app.services.traci import CATEGORY_LABELS, factors_for, versions
from app.templating import url_for_query

analysis_bp = Blueprint("analysis", __name__, url_prefix="/analysis")

PREVIEW_PER_PAGE = 25

# Shown open when the URL uses one of them, so a shared link shows every filter.
CONTROL_FILTERS = ("so2_control", "nox_control", "pm_control", "hg_control", "program")


def _metadata(analysis, counts, endpoint: str = "analysis.builder") -> dict[str, Any]:
    """Export metadata, pointing at the page that shows this selection."""
    parameters = {
        key: values
        for key in request.args
        if key not in ("format", "page") and (values := [v for v in request.args.getlist(key) if v])
    }
    url = url_for_query(endpoint, format=None, page=None)
    return metadata(analysis, url, parameters, counts)


def _page_context(analysis) -> dict[str, Any]:
    """What every Phase 2 page's filter form needs."""
    return {
        "analysis": analysis,
        "choices": filter_choices(),
        "controls_open": any(request.args.get(k) for k in CONTROL_FILTERS),
    }


def _about(analysis, counts, endpoint: str, extra: list[tuple[str, Any]]) -> list[list[Any]]:
    """The "about" sheet of a download: where it came from and how it was made."""
    about = _metadata(analysis, counts, endpoint)
    rows = [[key, about[key]] for key in ("application", "generated_at", "url", "filters")]
    rows.append(["selection", json.dumps(about["selection"])])
    rows += [[key, value] for key, value in extra]
    rows.append(["missing_values", about["missing_values"]])
    return rows


def _definitions(variables) -> list[list[Any]]:
    rows = [[], ["variable", "label", "unit", "kind", "definition", "everyday meaning"]]
    rows += [[v.name, v.label, v.unit, v.kind, v.definition, v.meaning] for v in variables.values()]
    return rows


def _xlsx_or_csv() -> str:
    return requested_format(request.args.get("format"))


@analysis_bp.route("")
def builder():
    """Choose the records for an analysis and see what they contain."""
    analysis = parse_analysis(request.args)
    units = selected_units(analysis)
    frame = analysis_frame(analysis, units)
    selected = in_selection(units, analysis)

    total = len(frame)
    total_pages = max(1, math.ceil(total / PREVIEW_PER_PAGE))
    try:
        page = int(request.args.get("page", 1))
    except ValueError:
        page = 1
    page = max(1, min(page, total_pages))
    preview = frame.iloc[(page - 1) * PREVIEW_PER_PAGE : page * PREVIEW_PER_PAGE]

    numeric = numeric_variables(analysis.level)
    missing = {row["variable"]: row for row in missing_summary(frame, analysis.level)}
    factors = factors_for()
    return render_template(
        "analysis.html",
        analysis=analysis,
        counts=dataset_counts(selected),
        rows=preview.astype(object).where(preview.notna(), None).to_dict(orient="records"),
        total=total,
        page=page,
        total_pages=total_pages,
        first_index=(page - 1) * PREVIEW_PER_PAGE + 1 if total else 0,
        last_index=min(total, page * PREVIEW_PER_PAGE),
        measured=[(v, missing[v.name]) for v in numeric.values() if v.name in MEASURED],
        derived=[(v, missing[v.name]) for v in numeric.values() if v.name in DERIVED],
        categorical=categorical_summary(
            frame if analysis.level == "facility" else selected, analysis.level
        ),
        numeric=numeric,
        choices=filter_choices(),
        controls_open=any(request.args.get(k) for k in CONTROL_FILTERS),
        factors=factors,
        category_labels=CATEGORY_LABELS,
    )


@analysis_bp.route("/download")
def download():
    """The whole analytical dataset as CSV, or as Excel with an "About" sheet
    holding the filters, counts and indicator definitions."""
    analysis = parse_analysis(request.args)
    units = selected_units(analysis)
    frame = analysis_frame(analysis, units)
    columns = analysis.columns()
    rows = frame.astype(object).where(frame.notna(), None).itertuples(index=False, name=None)
    stem = f"analysis-{analysis.level}"
    if requested_format(request.args.get("format")) == "csv":
        return tabular_response(columns, rows, stem, "csv")

    about = _metadata(analysis, dataset_counts(in_selection(units, analysis)))
    workbook = Workbook(write_only=True)
    data = workbook.create_sheet("data")
    data.append(columns)
    for row in rows:
        data.append([v.isoformat() if hasattr(v, "isoformat") else v for v in row])
    sheet = workbook.create_sheet("about")
    for key in ("application", "generated_at", "url", "level", "filters", "missing_values",
                "facility_totals"):
        if key in about:
            sheet.append([key, about[key]])
    sheet.append(["year_over_year", ", ".join(about["year_over_year"]) or "none"])
    sheet.append(["selection", json.dumps(about["selection"])])
    sheet.append([])
    sheet.append(["variable", "label", "unit", "definition", "everyday meaning"])
    for name, definition in about["definitions"].items():
        sheet.append([name, definition["label"], definition["unit"], definition["definition"],
                      definition.get("meaning", "")])
    output = io.BytesIO()
    workbook.save(output)
    return Response(
        output.getvalue(),
        mimetype=FORMATS["xlsx"][1],
        headers={
            "Content-Disposition": f'attachment; filename="{stem}-{datetime.now():%Y%m%d-%H%M}.xlsx"'
        },
    )


@analysis_bp.route("/config.json")
def configuration():
    """This selection as a configuration file that the builder can load again."""
    analysis = parse_analysis(request.args)
    counts = dataset_counts(in_selection(selected_units(analysis), analysis))
    body = json.dumps(_metadata(analysis, counts), indent=2)
    return Response(
        body,
        mimetype="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="analysis-{datetime.now():%Y%m%d-%H%M}.json"'
        },
    )


# ---------------------------------------------------------------------------
# Week 2: descriptive statistics
# ---------------------------------------------------------------------------


def _statistics():
    analysis = parse_analysis(request.args)
    units, cats = load_selection(analysis)
    variables = all_variables(analysis.level, cats)
    options = parse_statistics_options(request.args, analysis.level, variables)
    frame = level_frame(units, analysis.level, cats)
    return analysis, units, cats, variables, options, frame


@analysis_bp.route("/statistics")
def statistics():
    """Count, missing, mean, standard deviation, quartiles, IQR and more for
    every numeric variable, optionally within each group."""
    analysis, units, cats, variables, options, frame = _statistics()
    variable = options["variable"]
    grouped = (
        describe_grouped(frame, {variable: variables[variable]}, options["group"])
        if options["group"] else []
    )
    return render_template(
        "statistics.html",
        **_page_context(analysis),
        errors=analysis.errors + options["errors"],
        counts=dataset_counts(units),
        total=len(frame),
        rows=describe(frame, variables),
        grouped=grouped,
        options=options,
        variables=variables,
        statistics=STATISTICS,
        main_statistics=MAIN_STATISTICS,
        traci_version=cats[0].version if cats else None,
    )


@analysis_bp.route("/statistics/download")
def statistics_download():
    """The statistics as CSV, or Excel with an "about" sheet."""
    analysis, units, cats, variables, options, frame = _statistics()
    chosen = options["variables"]
    group = options["group"]
    keys = list(STATISTICS)
    if group:
        header = [group, "records", "variable", "label", "unit", *keys]
        rows = [
            [g["group"], g["records"], name, var.label, var.unit,
             *[g["statistics"][name][k] for k in keys]]
            for g in describe_grouped(frame, chosen, group)
            for name, var in chosen.items()
        ]
    else:
        header = ["variable", "label", "unit", "kind", *keys]
        rows = [[r["name"], r["label"], r["unit"], r["kind"], *[r["statistics"][k] for k in keys]]
                for r in describe(frame, chosen)]
    stem = f"statistics-{analysis.level}" + (f"-by-{group}" if group else "")
    if _xlsx_or_csv() == "csv":
        return tabular_response(header, rows, stem, "csv")
    about = _about(analysis, dataset_counts(units), "analysis.statistics", [
        ("level", analysis.level),
        ("grouped_by", group or "not grouped"),
        ("percentile_method", "linear interpolation between values (numpy/pandas 'linear', "
                              "Excel PERCENTILE.INC)"),
        ("standard_deviation", "sample standard deviation (n - 1)"),
        ("confidence_interval", "mean ± t(0.975, n - 1) × std / √n (SciPy)"),
        ("skewness_method", "adjusted Fisher-Pearson coefficient (SciPy, bias=False)"),
        ("outlier_rule", "values below Q1 - 1.5 IQR or above Q3 + 1.5 IQR"),
        ("traci_version", cats[0].version if cats else "none stored"),
    ])
    about += [[], ["statistic", "label", "meaning"]]
    about += [[k, label, text] for k, (label, text) in STATISTICS.items()]
    about += _definitions(chosen)
    return workbook_response([("statistics", header, rows), ("about", None, about)], stem)


# ---------------------------------------------------------------------------
# Week 2: comparisons and rankings
# ---------------------------------------------------------------------------


def _comparison():
    analysis = parse_analysis(request.args)
    units, cats = load_selection(analysis)
    variables = all_variables("unit", cats)
    options = parse_compare_options(request.args, variables)
    result = compare_groups(units, variables[options["metric"]], options["by"], options["baseline"])
    return analysis, units, cats, variables, options, result


@analysis_bp.route("/compare")
def compare():
    """Compare facilities, states, fuels, unit types or years on one variable:
    ranks, percentiles, z-scores and differences from a reference."""
    analysis, units, cats, variables, options, result = _comparison()
    return render_template(
        "analysis_compare.html",
        **_page_context(analysis),
        errors=analysis.errors + options["errors"] + result["errors"],
        counts=dataset_counts(units),
        options=options,
        result=result,
        shown=shown_groups(result["groups"], options["order"], options["limit"]),
        variables=variables,
        dimensions=DIMENSIONS,
        limits=LIMITS,
    )


COMPARE_COLUMNS = [
    "rank", "group", "label", "value", "difference", "pct_difference", "share_pct",
    "percentile", "z_score", "median", "values_present", "records", "units", "facilities",
    "is_reference",
]


@analysis_bp.route("/compare/download")
def compare_download():
    """Every group of the comparison as CSV, or Excel with an "about" sheet."""
    analysis, units, cats, variables, options, result = _comparison()
    rows = [[g[c] for c in COMPARE_COLUMNS] for g in result["groups"]]
    metric = result["metric"]
    stem = f"compare-{options['by']}-{options['metric']}"
    if _xlsx_or_csv() == "csv":
        return tabular_response(COMPARE_COLUMNS, rows, stem, "csv")
    test = result["test"]
    about = _about(analysis, dataset_counts(units), "analysis.compare", [
        ("compared", f"{result['group_count']} {result['dimension_plural']}"),
        ("variable", f"{metric['name']}: {metric['label']} ({metric['unit']})"),
        ("group_value", "each group's value is the " + result["aggregation_text"]),
        ("reference", f"{result['reference']['label']} = {result['reference']['value']}"),
        ("difference", "group value - reference; pct_difference = difference / reference × 100"),
        ("share_pct", "group total / total of all groups × 100 (totals only)"),
        ("rank", "1 = highest value; ties share the best rank"),
        ("percentile", "share of groups with a value at or below this one"),
        ("z_score", "(value - mean of group values) / their sample standard deviation"),
        ("test", test["name"]),
        ("test_result", test["interpretation"]),
        *([("H", test["h"]), ("df", test["df"]), ("p_value", test["p_value"]),
           ("epsilon_squared", test["epsilon_squared"])] if test.get("available") else []),
    ])
    about += _definitions({metric["name"]: variables[metric["name"]]})
    return workbook_response([("comparison", COMPARE_COLUMNS, rows), ("about", None, about)], stem)


# ---------------------------------------------------------------------------
# Week 2: TRACI impact assessment
# ---------------------------------------------------------------------------


def _impacts():
    analysis = parse_analysis(request.args)
    errors = list(analysis.errors)
    stored = versions()
    version = (request.args.get("version") or "").strip() or None
    if version and version not in stored:
        errors.append(f"TRACI {version} is not stored, so the latest version was used.")
        version = None
    units, cats = load_selection(analysis, version)
    options = parse_impact_options(request.args, cats)
    errors += options["errors"]
    return analysis, units, cats, options, errors, stored


@analysis_bp.route("/impacts")
def impacts():
    """Potential environmental and health impacts of the selected emissions,
    by EPA TRACI impact category, with the calculation shown."""
    analysis, units, cats, options, errors, stored = _impacts()
    groups = grouped_impacts(units, cats, options["group"]) if cats and len(units) else []
    return render_template(
        "impacts.html",
        **_page_context(analysis),
        errors=errors,
        counts=dataset_counts(units),
        cats=cats,
        summary=impact_summary(units, cats),
        groups=sorted_groups(groups, options["metric"], options["limit"]),
        group_count=len(groups),
        options=options,
        impact_groups=IMPACT_GROUPS,
        limits=IMPACT_LIMITS,
        versions=stored,
        example=worked_example(units, cats),
        short_ton_kg=SHORT_TON_KG,
        disclaimer=DISCLAIMER,
    )


IMPACT_ID_COLUMNS = [
    "facility_id", "facility_name", "state_code", "epa_unit_id", "unit_key", "reporting_year",
    "primary_fuel", "unit_type", "gross_load", "co2_mass", "so2_mass", "nox_mass",
]


@analysis_bp.route("/impacts/download")
def impacts_download():
    """Impacts of every selected unit-year as CSV; Excel adds the totals, the
    groups, the factors used and an "about" sheet."""
    analysis, units, cats, options, errors, stored = _impacts()
    version = cats[0].version if cats else None
    metrics = sorted({m for cat in cats for m in cat.metrics})
    header = [*IMPACT_ID_COLUMNS, *[f"{m}_kg" for m in metrics],
              *[n for cat in cats for n in (cat.name, cat.intensity_name)], "traci_version"]
    frame = units.copy()
    for m in metrics:
        frame[f"{m}_kg"] = frame[m] * SHORT_TON_KG
    frame["traci_version"] = version
    frame = frame.reindex(columns=header)
    rows = list(frame.astype(object).where(frame.notna(), None).itertuples(index=False, name=None))
    stem = "traci-impacts"
    if _xlsx_or_csv() == "csv":
        return tabular_response(header, rows, stem, "csv")

    summary_header = ["impact_category", "label", "unit", "total", "per_mwh", "records_used",
                      "records_missing", "formula"]
    summary = [[s["category"], s["label"], s["unit"], s["total"], s["intensity"],
                s["records_used"], s["records_missing"], s["formula"]]
               for s in impact_summary(units, cats)]
    group_column = IMPACT_GROUPS[options["group"]][0]
    group_header = [group_column, "label", "records", "gross_load",
                    *[f"{cat.name}{suffix}" for cat in cats for suffix in ("", "_per_mwh", "_rank")]]
    group_rows = [
        [g["group"], g["label"], g["records"], g["gross_load"],
         *[g["impacts"][cat.name][field] for cat in cats for field in ("total", "intensity", "rank")]]
        for g in sorted_groups(grouped_impacts(units, cats, options["group"]) if len(units) else [],
                               options["metric"], None)
    ]
    factor_header = ["traci_version", "campd_metric", "pollutant_name", "impact_category",
                     "factor_value", "factor_unit", "source"]
    factor_rows = [[version, f.campd_metric, f.pollutant_name, cat.category, f.value, f.unit, f.source]
                   for cat in cats for f in cat.factors]
    about = _about(analysis, dataset_counts(units), "analysis.impacts", [
        ("traci_version", version or "none stored"),
        ("short_ton_kg", SHORT_TON_KG),
        ("mass_kg", "mass in short tons × 907.18474"),
        ("impact", "impact_c = sum over pollutants of mass_kg × characterization factor (CF)"),
        ("per_mwh", "impact divided by gross load (MWh); missing when gross load is missing or 0"),
        ("matching", "exact TRACI substance names, air emissions only: CO2 = CARBON DIOXIDE, "
                     "SO2 = SULFUR DIOXIDE, NOx = NITROGEN OXIDES"),
        ("incomplete_records", "a unit-year's impact in a category is calculated only when "
                               "every pollutant the category uses was reported"),
        ("note", DISCLAIMER),
    ])
    return workbook_response([
        ("impacts by unit-year", header, rows),
        ("summary", summary_header, summary),
        (f"by {options['group']}", group_header, group_rows),
        ("factors", factor_header, factor_rows),
        ("about", None, about),
    ], stem)
