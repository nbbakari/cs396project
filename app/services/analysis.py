"""Analytical data layer: the unit-years in an analysis as a pandas DataFrame,
with missing values restored, derived performance indicators and
year-over-year changes.

The records in an analysis are chosen with the data explorer's URL
parameters (see app/services/search.py): year range, state, facility, unit,
fuel, unit type, control technology, program, minimum operating time or
gross load, and so on. So any analysis can be reproduced from its URL. The
analysis adds its own parameters:

    level        "unit" (one row per unit-year, default) or "facility"
                 (one row per facility-year)
    yoy          variables to compute year-over-year changes for,
                 comma-separated, e.g. ``yoy=co2_mass,heat_rate``
    sort, order  any output column, "asc" or "desc"

Missing values
--------------
Phase 1 stores a blank CAMPD metric as 0, because the schema needs a number,
and lists it in ``annual_records.missing_metrics``. Here those zeros become
missing again (NaN). Every derived value whose input is missing, or whose
denominator is zero, is missing too: nothing undefined is reported as 0.

Facility-level rows add up their units. A total is missing only when none of
the units reported that metric, and ``units_with_missing_values`` counts the
units that had a gap. A facility's indicators use only the units that
reported both inputs, so a unit with no reported CO2 does not count as
zero-emission generation.

CAMPD annual data has no nameplate capacity, so capacity factor is not
calculated. Operating utilization (operating hours as a share of the year) and
average operating load are calculated instead.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import numpy as np
import pandas as pd
from werkzeug.datastructures import MultiDict

from app import db, glossary
from app.services.search import UNIT_COLUMNS, Search, build_statement, parse_search

LEVELS = ("unit", "facility")

# Parameters the analysis reads itself; every other one is an explorer filter.
ANALYSIS_PARAMS = (
    "level", "yoy", "sort", "order", "page", "per_page", "limit", "group_by", "per_group",
)

# Denominator marker for indicators divided by the hours in the reporting year.
HOURS_IN_YEAR = "hours_in_year"


@dataclass(frozen=True)
class Variable:
    """A numeric variable available for analysis, with its definition."""

    name: str
    label: str
    unit: str
    definition: str
    kind: str = "measured"
    numerator: Optional[str] = None
    denominator: Optional[str] = None

    @property
    def meaning(self) -> str:
        """The everyday meaning, e.g. "electricity generated" for gross load."""
        return glossary.meaning(self.name)

    def as_dict(self) -> dict[str, Any]:
        values = {k: v for k, v in dataclasses.asdict(self).items() if v is not None}
        return {**values, "meaning": self.meaning} if self.meaning else values


MEASURED = {
    v.name: v
    for v in (
        Variable("operating_time", "Operating time", "hours",
                 "Hours the unit operated during the reporting year."),
        Variable("gross_load", "Gross load", "MWh",
                 "Gross electricity generated during the reporting year."),
        Variable("steam_load", "Steam load", "1000 lb",
                 "Steam produced during the reporting year, where reported."),
        Variable("heat_input", "Heat input", "MMBtu",
                 "Heat input from fuel during the reporting year."),
        Variable("co2_mass", "CO2 mass", "short tons", "CO2 emitted to air."),
        Variable("so2_mass", "SO2 mass", "short tons", "SO2 emitted to air."),
        Variable("nox_mass", "NOx mass", "short tons", "NOx emitted to air."),
    )
}

DERIVED = {
    v.name: v
    for v in (
        Variable("co2_intensity", "CO2 intensity", "short tons/MWh",
                 "CO2 mass divided by gross load.", "derived", "co2_mass", "gross_load"),
        Variable("so2_intensity", "SO2 intensity", "short tons/MWh",
                 "SO2 mass divided by gross load.", "derived", "so2_mass", "gross_load"),
        Variable("nox_intensity", "NOx intensity", "short tons/MWh",
                 "NOx mass divided by gross load.", "derived", "nox_mass", "gross_load"),
        Variable("heat_rate", "Heat rate", "MMBtu/MWh",
                 "Heat input divided by gross load; lower means more efficient.",
                 "derived", "heat_input", "gross_load"),
        Variable("operating_utilization", "Operating utilization", "share of the year",
                 "Operating time divided by the hours in the reporting year "
                 "(8,760, or 8,784 in a leap year).",
                 "derived", "operating_time", HOURS_IN_YEAR),
        Variable("average_load", "Average operating load", "MW",
                 "Gross load divided by operating time: average output while running.",
                 "derived", "gross_load", "operating_time"),
    )
}

# Operating time is per unit, so it is not added up for a facility, and the
# indicators built on it exist only at the unit level.
UNIT_ONLY = {"operating_time", "operating_utilization", "average_load"}

# Metrics CAMPD may leave blank (and Phase 1 stored as 0).
REPORTED_METRICS = ("operating_time", "gross_load", "heat_input", "co2_mass", "so2_mass", "nox_mass")

FACILITY_KEYS = ["facility_id", "facility_name", "state_code", "county", "reporting_year"]
FACILITY_TOTALS = ["gross_load", "steam_load", "heat_input", "co2_mass", "so2_mass", "nox_mass"]

CATEGORICAL = {
    "state_code": "State",
    "county": "County",
    "source_category": "Source category",
    "unit_type": "Unit type",
    "primary_fuel": "Primary fuel",
    "secondary_fuel": "Secondary fuel",
    "owner_operator": "Owner/operator",
    "so2_control": "SO2 controls",
    "nox_control": "NOx controls",
    "pm_control": "PM controls",
    "hg_control": "Mercury controls",
    "program_code": "Programs",
}
FACILITY_CATEGORICAL = ("state_code", "county")


def numeric_variables(level: str = "unit") -> dict[str, Variable]:
    """The numeric variables available at a level, measured then derived."""
    every = {**MEASURED, **DERIVED}
    if level == "facility":
        return {name: v for name, v in every.items() if name not in UNIT_ONLY}
    return every


def variables(level: str = "unit") -> dict[str, list[dict[str, Any]]]:
    """Every variable available for analysis at a level, for display."""
    categorical = FACILITY_CATEGORICAL if level == "facility" else tuple(CATEGORICAL)
    return {
        "numeric": [v.as_dict() for v in numeric_variables(level).values()],
        "categorical": [{"name": name, "label": CATEGORICAL[name]} for name in categorical],
    }


# ---------------------------------------------------------------------------
# Calculations
# ---------------------------------------------------------------------------


def safe_ratio(numerator: Any, denominator: Any) -> pd.Series:
    """numerator / denominator, missing wherever either is missing or the
    denominator is not positive -- never 0 or infinity in place of "undefined"."""
    num = pd.to_numeric(pd.Series(numerator), errors="coerce").astype("float64")
    den = pd.to_numeric(pd.Series(denominator, index=num.index), errors="coerce").astype("float64")
    return num / den.where(den > 0)


def hours_in_year(years: pd.Series) -> pd.Series:
    years = years.astype("int64")
    leap = (years % 4 == 0) & ((years % 100 != 0) | (years % 400 == 0))
    return pd.Series(np.where(leap, 8784.0, 8760.0), index=years.index)


def restore_missing(frame: pd.DataFrame) -> pd.DataFrame:
    """Turn the zeros stored for blank metrics back into missing values."""
    frame = frame.copy()
    flags = (
        frame["missing_metrics"].fillna("").astype(str)
        if "missing_metrics" in frame
        else pd.Series("", index=frame.index)
    )
    for name in MEASURED:
        if name not in frame:
            continue
        frame[name] = pd.to_numeric(frame[name], errors="coerce").astype("float64")
        if name in REPORTED_METRICS:
            frame.loc[flags.str.contains(rf"(?:^|,){name}(?:,|$)"), name] = np.nan
    return frame


def add_indicators(frame: pd.DataFrame) -> pd.DataFrame:
    """Add every unit-level derived indicator whose inputs are in the frame."""
    frame = frame.copy()
    for var in DERIVED.values():
        if var.numerator not in frame:
            continue
        if var.denominator == HOURS_IN_YEAR:
            denominator = hours_in_year(frame["reporting_year"])
        elif var.denominator in frame:
            denominator = frame[var.denominator]
        else:
            continue
        frame[var.name] = safe_ratio(frame[var.numerator], denominator)
    return frame


def facility_frame(units: pd.DataFrame) -> pd.DataFrame:
    """One row per facility-year from unit-years (see the module docstring)."""
    paired = [v for v in DERIVED.values() if v.name not in UNIT_ONLY]
    work = units.copy()
    for var in paired:
        both = work[var.numerator].notna() & work[var.denominator].notna()
        work[f"_{var.name}_num"] = work[var.numerator].where(both)
        work[f"_{var.name}_den"] = work[var.denominator].where(both)
    work["_has_gap"] = work[list(REPORTED_METRICS)].isna().any(axis=1)

    grouped = work.groupby(FACILITY_KEYS, dropna=False, sort=False)
    pair_columns = [f"_{v.name}_{part}" for v in paired for part in ("num", "den")]
    out = grouped[FACILITY_TOTALS + pair_columns].sum(min_count=1)
    out.insert(0, "unit_count", grouped["unit_key"].nunique())
    out.insert(1, "units_with_missing_values", grouped["_has_gap"].sum().astype("int64"))
    for var in paired:
        out[var.name] = safe_ratio(out.pop(f"_{var.name}_num"), out.pop(f"_{var.name}_den"))
    return out.reset_index()


def year_over_year(frame: pd.DataFrame, columns: list[str], entity: str) -> pd.DataFrame:
    """Add ``<column>_change`` and ``<column>_pct_change`` against the previous
    calendar year of the same unit or facility.

    The change is missing when the previous year is not in the frame or either
    value is missing; the percentage change is also missing when the previous
    value is 0.
    """
    previous = frame[[entity, "reporting_year", *columns]].copy()
    previous["reporting_year"] = previous["reporting_year"] + 1
    previous = previous.rename(columns={c: f"_{c}_previous" for c in columns})
    out = frame.merge(previous, on=[entity, "reporting_year"], how="left", validate="one_to_one")
    for column in columns:
        before = out.pop(f"_{column}_previous")
        out[f"{column}_change"] = out[column] - before
        out[f"{column}_pct_change"] = safe_ratio(out[f"{column}_change"], before) * 100
    return out


# ---------------------------------------------------------------------------
# Parameters and loading
# ---------------------------------------------------------------------------


@dataclass
class Analysis:
    """A parsed analysis request. Build with :func:`parse_analysis`."""

    search: Search
    level: str = "unit"
    yoy: list[str] = field(default_factory=list)
    sort: Optional[str] = None
    order: str = "desc"
    errors: list[str] = field(default_factory=list)

    @property
    def entity(self) -> str:
        return "facility_id" if self.level == "facility" else "unit_key"

    def columns(self) -> list[str]:
        """The output columns, in order."""
        if self.level == "facility":
            base = [*FACILITY_KEYS, "unit_count", "units_with_missing_values", *FACILITY_TOTALS]
        else:
            base = [column.key for column in UNIT_COLUMNS]
        derived = [name for name in DERIVED if name in numeric_variables(self.level)]
        changes = [f"{name}_{suffix}" for name in self.yoy for suffix in ("change", "pct_change")]
        return [*base, *derived, *changes]


def _listed(args: MultiDict, name: str) -> list[str]:
    values: list[str] = []
    for raw in args.getlist(name):
        values.extend(part.strip() for part in str(raw).split(",") if part.strip())
    return values


def parse_analysis(args: MultiDict) -> Analysis:
    """Read an analysis from query-string arguments; problems go to ``errors``."""
    filters = MultiDict([(k, v) for k, v in args.items(multi=True) if k not in ANALYSIS_PARAMS])
    search = parse_search(filters)
    analysis = Analysis(search=search, errors=search.errors)

    level = (args.get("level") or "unit").strip().lower()
    if level not in LEVELS:
        analysis.errors.append(f"Level '{level}' is not 'unit' or 'facility'; used 'unit'.")
        level = "unit"
    analysis.level = level

    available = numeric_variables(level)
    for name in _listed(args, "yoy"):
        if name in available:
            if name not in analysis.yoy:
                analysis.yoy.append(name)
        else:
            analysis.errors.append(
                f"No year-over-year change for '{name}' at the {level} level, so it was ignored."
            )

    sort = (args.get("sort") or "").strip()
    if sort:
        if sort in analysis.columns():
            analysis.sort = sort
        else:
            analysis.errors.append(f"Cannot sort by '{sort}', so the default order was used.")
    analysis.order = "asc" if (args.get("order") or "").lower() == "asc" else "desc"
    return analysis


def load_units(search: Search) -> pd.DataFrame:
    """The unit-years a search selects, with blank metrics restored to missing."""
    result = db.session.execute(build_statement(search))
    frame = pd.DataFrame.from_records(result.all(), columns=list(result.keys()))
    return restore_missing(frame)


def _with_previous_years(search: Search) -> Search:
    """The same search widened by one year, so the first year has a comparison."""
    return dataclasses.replace(
        search,
        years=sorted(set(search.years) | {year - 1 for year in search.years}),
        year_min=search.year_min - 1 if search.year_min is not None else None,
        errors=[],
    )


def _in_years(years: pd.Series, search: Search) -> pd.Series:
    keep = pd.Series(True, index=years.index)
    if search.years:
        keep &= years.isin(search.years)
    if search.year_min is not None:
        keep &= years >= search.year_min
    if search.year_max is not None:
        keep &= years <= search.year_max
    return keep


def _sorted(frame: pd.DataFrame, analysis: Analysis) -> pd.DataFrame:
    tiebreak = [c for c in ("facility_id", "epa_unit_id") if c in frame]
    frame = frame.sort_values(
        ["reporting_year", *tiebreak], ascending=[False, *[True] * len(tiebreak)], kind="stable"
    )
    if analysis.sort:
        frame = frame.sort_values(
            analysis.sort, ascending=analysis.order == "asc", na_position="last", kind="stable"
        )
    return frame.reset_index(drop=True)


def selected_units(analysis: Analysis) -> pd.DataFrame:
    """The unit-years an analysis needs, with indicators.

    For year-over-year changes the records of the year before the first
    selected year are loaded too, so that year has a comparison; use
    :func:`in_selection` to drop them again.
    """
    search = _with_previous_years(analysis.search) if analysis.yoy else analysis.search
    return add_indicators(load_units(search))


def in_selection(frame: pd.DataFrame, analysis: Analysis) -> pd.DataFrame:
    """Only the rows in the years the analysis selected."""
    return frame[_in_years(frame["reporting_year"], analysis.search)]


def analysis_frame(analysis: Analysis, units: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    """The full result of an analysis: one row per unit-year or facility-year.

    ``units`` is the output of :func:`selected_units`, when already loaded.
    """
    if units is None:
        units = selected_units(analysis)
    frame = facility_frame(units) if analysis.level == "facility" else units
    if analysis.yoy:
        frame = in_selection(year_over_year(frame, analysis.yoy, analysis.entity), analysis)
    return _sorted(frame.reindex(columns=analysis.columns()), analysis)


# ---------------------------------------------------------------------------
# Summaries
# ---------------------------------------------------------------------------


def dataset_counts(units: pd.DataFrame) -> dict[str, Any]:
    """How many records, facilities, units, states and years a selection has."""
    years = sorted(int(y) for y in units["reporting_year"].dropna().unique())
    return {
        "records": int(len(units)),
        "facilities": int(units["facility_id"].nunique()),
        "units": int(units["unit_key"].nunique()),
        "states": int(units["state_code"].nunique()),
        "years": years,
    }


def missing_summary(frame: pd.DataFrame, level: str = "unit") -> list[dict[str, Any]]:
    """For each numeric variable: values present, values missing, and, for an
    indicator, how many are missing only because the denominator is 0."""
    total = len(frame)
    rows = []
    for var in numeric_variables(level).values():
        values = frame[var.name] if var.name in frame else pd.Series(np.nan, index=frame.index)
        missing = int(values.isna().sum())
        row = {
            "variable": var.name,
            "label": var.label,
            "unit": var.unit,
            "kind": var.kind,
            "count": total - missing,
            "missing": missing,
            "missing_pct": round(100 * missing / total, 1) if total else None,
        }
        if var.kind == "derived" and var.numerator in frame and var.denominator in frame:
            inputs_present = frame[var.numerator].notna() & frame[var.denominator].notna()
            row["zero_denominator"] = int((inputs_present & (frame[var.denominator] <= 0)).sum())
        rows.append(row)
    return rows


def categorical_summary(frame: pd.DataFrame, level: str = "unit") -> list[dict[str, Any]]:
    """For each categorical variable: distinct values and blanks."""
    total = len(frame)
    rows = []
    for name in FACILITY_CATEGORICAL if level == "facility" else CATEGORICAL:
        values = frame[name] if name in frame else pd.Series(None, index=frame.index, dtype=object)
        blank = values.isna() | (values.astype(str).str.strip() == "")
        rows.append(
            {
                "variable": name,
                "label": CATEGORICAL[name],
                "meaning": glossary.meaning(name),
                "distinct": int(values[~blank].nunique()),
                "missing": int(blank.sum()),
                "missing_pct": round(100 * int(blank.sum()) / total, 1) if total else None,
            }
        )
    return rows


MISSING_VALUE_RULE = (
    "Metrics that were blank in the CAMPD source, and indicators whose input is "
    "missing or whose denominator is zero, are left empty -- never reported as 0."
)
FACILITY_RULE = (
    "Facility rows add up their units' reported values; a facility's indicators "
    "use only the units that reported both inputs."
)


def metadata(
    analysis: Analysis, url: str, parameters: dict[str, list[str]], counts: dict[str, Any]
) -> dict[str, Any]:
    """What an export needs to be reproduced and interpreted."""
    return {
        "application": "epaData",
        "kind": "analysis-configuration",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "url": url,
        "parameters": parameters,
        "level": analysis.level,
        "year_over_year": analysis.yoy,
        "filters": analysis.search.describe_filters(),
        "selection": counts,
        "missing_values": MISSING_VALUE_RULE,
        **({"facility_totals": FACILITY_RULE} if analysis.level == "facility" else {}),
        "definitions": {
            name: var.as_dict() for name, var in numeric_variables(analysis.level).items()
        },
    }


def json_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    """Rows as JSON-ready dicts: missing values become null, never NaN."""
    clean = frame.astype(object).where(frame.notna(), None)
    records = clean.to_dict(orient="records")
    for record in records:
        for key, value in record.items():
            if isinstance(value, np.generic):
                record[key] = value.item()
            elif hasattr(value, "isoformat"):
                record[key] = value.isoformat()
    return records
