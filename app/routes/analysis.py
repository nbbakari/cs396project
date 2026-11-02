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
from app.services.exports import FORMATS, requested_format, tabular_response
from app.services.search import filter_choices
from app.services.traci import CATEGORY_LABELS, factors_for
from app.templating import url_for_query

analysis_bp = Blueprint("analysis", __name__, url_prefix="/analysis")

PREVIEW_PER_PAGE = 25

# Shown open when the URL uses one of them, so a shared link shows every filter.
CONTROL_FILTERS = ("so2_control", "nox_control", "pm_control", "hg_control", "program")


def _metadata(analysis, counts) -> dict[str, Any]:
    """Export metadata, pointing at the builder page for this selection."""
    parameters = {
        key: values
        for key in request.args
        if key not in ("format", "page") and (values := [v for v in request.args.getlist(key) if v])
    }
    url = url_for_query("analysis.builder", format=None, page=None)
    return metadata(analysis, url, parameters, counts)


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
