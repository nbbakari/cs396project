"""Read, map and validate a CAMPD-format annual emissions table.

Validation never drops a row silently. Every row that will not be imported,
and every row imported with a value changed, produces an *issue* recording the
source row number, the field, and the reason. Those issues are shown on the
review page, stored with the dataset after approval, and downloadable as the
data-quality report.

Two pandas 3.x behaviours shape the code below:

* **Copy-on-Write is always on.** Chained assignment (``df[mask]["col"] = ...``)
  silently writes to a temporary, so every mutation here either reassigns the
  frame or writes through ``df[col]`` on a frame we own outright.
* **Blank cells arrive as ``pd.NA``**, and ``itertuples`` yields numpy scalars.
  psycopg2 and sqlite3 cannot adapt either, so the ingest step pushes every
  value back through the ``as_*`` coercion helpers before it reaches the ORM.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import IO, Any, Callable, Optional, Union

import numpy as np
import pandas as pd
from sqlalchemy import select

from app import db
from app.models import MAX_OPERATING_HOURS, AnnualRecord, Unit

ALLOWED_EXTENSIONS = {"csv", "xlsx"}

# CAMPD reporting began with the Acid Rain Program in 1995.
FIRST_REPORTING_YEAR = 1995

REJECTED = "rejected"
WARNING = "warning"

# Canonical field name -> header spellings we accept, already normalised by
# normalise_header(). Covers both the CAMPD bulk-download display headers
# ("Facility ID", "Gross Load (MWh)") and the easey API's camelCase JSON/CSV
# field names ("facilityId", "grossLoad"), which normalise to the same key.
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    # --- identifying keys -------------------------------------------------
    "facility_id": (
        "facilityid",
        "orisid",
        "oriscode",
        "orispl",
        "orisplcode",
        "facilityidorispl",
        "plantid",
    ),
    "epa_unit_id": ("unitid", "unitidentifier", "unitname"),
    "reporting_year": ("year", "reportingyear", "calendaryear", "opyear"),
    # --- facility attributes ---------------------------------------------
    "facility_name": ("facilityname", "plantname"),
    "state_code": ("state", "statecode", "stateabbreviation"),
    "county": ("county", "countyname"),
    "latitude": ("latitude", "lat"),
    "longitude": ("longitude", "lon", "lng"),
    "source_category": ("sourcecategory", "sourcecategorydescription"),
    # --- unit attributes --------------------------------------------------
    "unit_type": ("unittype", "unittypeinfo", "unittypedescription"),
    "primary_fuel": ("primaryfueltype", "primaryfuel", "primaryfuelinfo"),
    "secondary_fuel": ("secondaryfueltype", "secondaryfuel", "secondaryfuelinfo"),
    "owner_operator": ("owneroperator", "owneroperatorinfo", "owner", "owners"),
    "operating_date": ("commercialoperationdate", "operatingdate", "operationdate"),
    "retirement_date": ("retirementdate", "retiredate"),
    # --- annual operating / emissions metrics -----------------------------
    "operating_time": ("operatingtimecount", "operatingtime", "sumoptime", "optime"),
    "gross_load": ("grossload", "grossloadmwh"),
    "steam_load": ("steamload",),
    "heat_input": ("heatinput",),
    "co2_mass": ("co2mass",),
    "so2_mass": ("so2mass",),
    "nox_mass": ("noxmass",),
    "so2_control": ("so2controls", "so2controlinfo", "so2control"),
    "nox_control": ("noxcontrols", "noxcontrolinfo", "noxcontrol"),
    "pm_control": ("pmcontrols", "pmcontrolinfo", "pmcontrol"),
    "hg_control": ("hgcontrols", "hgcontrolinfo", "hgcontrol"),
    "program_code": ("programcode", "programcodeinfo", "programcodes", "programs"),
}

# Human-readable names, with units, used in the column report and messages.
FIELD_LABELS: dict[str, str] = {
    "facility_id": "Facility ID",
    "epa_unit_id": "Unit ID",
    "reporting_year": "Reporting year",
    "facility_name": "Facility name",
    "state_code": "State",
    "county": "County",
    "latitude": "Latitude",
    "longitude": "Longitude",
    "source_category": "Source category",
    "unit_type": "Unit type",
    "primary_fuel": "Primary fuel",
    "secondary_fuel": "Secondary fuel",
    "owner_operator": "Owner / operator",
    "operating_date": "Commercial operation date",
    "retirement_date": "Retirement date",
    "operating_time": "Operating time (hours)",
    "gross_load": "Gross load (MWh)",
    "steam_load": "Steam load (1000 lb)",
    "heat_input": "Heat input (MMBtu)",
    "co2_mass": "CO2 mass (short tons)",
    "so2_mass": "SO2 mass (short tons)",
    "nox_mass": "NOx mass (short tons)",
    "so2_control": "SO2 controls",
    "nox_control": "NOx controls",
    "pm_control": "PM controls",
    "hg_control": "Hg controls",
    "program_code": "Program codes",
}

KEY_FIELDS = ("facility_id", "epa_unit_id", "reporting_year")

# Without these three a row cannot be placed in the schema at all.
REQUIRED_FIELDS = KEY_FIELDS

NUMERIC_FIELDS = (
    "latitude",
    "longitude",
    "operating_time",
    "gross_load",
    "steam_load",
    "heat_input",
    "co2_mass",
    "so2_mass",
    "nox_mass",
)

# A negative value in any of these is physically meaningless and marks the row
# as corrupt, so the row is rejected rather than clamped -- clamping would
# quietly invent a plausible-looking number.
NON_NEGATIVE_FIELDS = (
    "operating_time",
    "gross_load",
    "steam_load",
    "heat_input",
    "co2_mass",
    "so2_mass",
    "nox_mass",
)

# NOT NULL on annual_records, so a missing value has to become something.
# Zero is the right reading for a unit that did not run in the reporting year,
# and each substitution is reported as a warning.
REQUIRED_NUMERIC_FIELDS = (
    "operating_time",
    "gross_load",
    "heat_input",
    "co2_mass",
    "so2_mass",
    "nox_mass",
)

DATE_FIELDS = ("operating_date", "retirement_date")

# Column widths from app.models, applied when coercing text so that one
# over-long value cannot fail the whole transaction.
TEXT_LIMITS = {
    "facility_name": 255,
    "state_code": 2,
    "county": 100,
    "source_category": 150,
    "epa_unit_id": 50,
    "unit_type": 100,
    "primary_fuel": 100,
    "secondary_fuel": 100,
    "so2_control": 150,
    "nox_control": 150,
    "pm_control": 150,
    "hg_control": 150,
    "program_code": 100,
}

_PARENTHETICAL = re.compile(r"\([^)]*\)")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_STATE_CODE = re.compile(r"^[A-Z]{2}$")


# ---------------------------------------------------------------------------
# Header resolution
# ---------------------------------------------------------------------------


def normalise_header(name: object) -> str:
    """Reduce a header to a comparable key.

    Drops parenthesised units so that ``"Gross Load (MWh)"``, ``"Gross Load"``
    and ``"grossLoad"`` all collapse to ``"grossload"``.
    """
    text = _PARENTHETICAL.sub(" ", str(name)).lower()
    return _NON_ALNUM.sub("", text)


def resolve_columns(df: pd.DataFrame) -> tuple[dict[str, str], list[str]]:
    """Map canonical field names onto the columns actually present.

    Returns ``(resolved, missing_required)``. Each source column is claimed by
    at most one field, so a file carrying both "Unit ID" and "Unit Name" does
    not bind them both to ``epa_unit_id``.
    """
    lookup: dict[str, str] = {}
    for column in df.columns:
        lookup.setdefault(normalise_header(column), column)

    resolved: dict[str, str] = {}
    claimed: set[str] = set()
    for field_name, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            column = lookup.get(alias)
            if column is not None and column not in claimed:
                resolved[field_name] = column
                claimed.add(column)
                break

    missing = [name for name in REQUIRED_FIELDS if name not in resolved]
    return resolved, missing


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def read_table(source: Union[str, Path, IO[bytes]], extension: str) -> pd.DataFrame:
    """Parse a CSV or Excel file into a DataFrame.

    ``dtype_backend="numpy_nullable"`` puts every column on a pandas nullable
    dtype, so a blank cell is ``pd.NA`` in text and numeric columns alike
    instead of the ``NaN`` / ``None`` / ``""`` mixture the default backend
    produces -- one missing-value check then covers the whole frame.
    """
    if extension == "csv":
        for encoding in ("utf-8-sig", "latin-1"):
            if hasattr(source, "seek"):
                source.seek(0)
            try:
                # utf-8-sig strips the BOM that Excel writes when saving as CSV.
                return pd.read_csv(
                    source,
                    dtype_backend="numpy_nullable",
                    skipinitialspace=True,
                    encoding=encoding,
                )
            except UnicodeDecodeError:
                continue
        raise ValueError("The file is not valid UTF-8 or Latin-1 text.")

    if extension == "xlsx":
        if hasattr(source, "seek"):
            source.seek(0)
        return pd.read_excel(source, engine="openpyxl", dtype_backend="numpy_nullable")

    raise ValueError(f"Unsupported file type '.{extension}'.")


# ---------------------------------------------------------------------------
# Series coercion
# ---------------------------------------------------------------------------


def _as_text_series(series: pd.Series) -> pd.Series:
    """Coerce to nullable string, writing whole floats without a ``.0``."""
    if pd.api.types.is_float_dtype(series):
        is_whole = (series == series.round()).fillna(False).astype(bool)
        whole = series.where(is_whole).astype("Int64").astype("string")
        return whole.fillna(series.astype("string"))
    return series.astype("string").str.strip()


def _to_float_series(series: pd.Series) -> pd.Series:
    """Coerce a column to nullable float, tolerating thousands separators."""
    if not pd.api.types.is_numeric_dtype(series):
        series = series.astype("string").str.replace(",", "", regex=False)
    return pd.to_numeric(series, errors="coerce").astype("Float64")


def _to_int_series(series: pd.Series) -> pd.Series:
    """Coerce a column to nullable integer; fractional values become NA."""
    numbers = _to_float_series(series)
    numbers = numbers.where(numbers == numbers.round())
    return numbers.astype("Int64")


def _to_datetime_series(series: pd.Series) -> pd.Series:
    """Coerce a column to datetimes, leaving unparseable values as NaT."""
    try:
        return pd.to_datetime(series, errors="coerce", format="mixed")
    except (ValueError, TypeError):
        return pd.to_datetime(series, errors="coerce")


# ---------------------------------------------------------------------------
# Scalar coercion (pandas/numpy -> Python builtins for the database driver)
# ---------------------------------------------------------------------------


def as_int(value: Any) -> Optional[int]:
    if value is None or pd.isna(value):
        return None
    return int(value)


def as_float(value: Any) -> Optional[float]:
    if value is None or pd.isna(value):
        return None
    return float(value)


def as_str(value: Any, limit: Optional[int] = None) -> Optional[str]:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:limit] if limit else text


def as_date(value: Any) -> Optional[date]:
    if value is None or pd.isna(value):
        return None
    return pd.Timestamp(value).date()


def as_text(value: Any, field_name: str) -> Optional[str]:
    """Coerce text to the column width declared in app.models."""
    return as_str(value, TEXT_LIMITS.get(field_name))


def _jsonable(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (list, dict)):
        return value
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, (pd.Timestamp, date)):
        return value.isoformat()
    return value


def _format_number(value: Any) -> str:
    number = float(value)
    return f"{number:,.0f}" if number == int(number) else f"{number:,.3f}"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@dataclass
class ValidationResult:
    """Outcome of validating one source table.

    ``clean`` holds the rows that will be imported, reindexed to every
    canonical column plus ``source_row``. ``issues`` lists every problem found,
    one dict per (row, problem).
    """

    clean: pd.DataFrame
    issues: list[dict[str, Any]]
    stats: dict[str, Any]
    resolved: dict[str, str]
    unrecognised: list[str]
    missing_required: list[str]
    # Trimmed source values and the original rows, both keyed by source_row,
    # used to describe rows in issues raised after validation.
    raw: pd.DataFrame = field(repr=False, default_factory=pd.DataFrame)
    original: pd.DataFrame = field(repr=False, default_factory=pd.DataFrame)

    @property
    def can_import(self) -> bool:
        return not self.missing_required and not self.clean.empty

    def column_report(self) -> list[dict[str, Any]]:
        """One entry per canonical field: where it came from, or what happens without it."""
        report = []
        for name in COLUMN_ALIASES:
            source = self.resolved.get(name)
            if name in REQUIRED_FIELDS:
                requirement = "required"
            elif name in REQUIRED_NUMERIC_FIELDS:
                requirement = "required metric"
            else:
                requirement = "optional"

            if source is not None:
                status = "found"
            elif requirement == "required":
                status = "missing - import blocked"
            elif requirement == "required metric":
                status = "missing - every row recorded as 0"
            else:
                status = "not in file - left blank"
            report.append(
                {
                    "field": name,
                    "label": FIELD_LABELS[name],
                    "requirement": requirement,
                    "source_column": source,
                    "status": status,
                }
            )
        return report

    def issue_counts(self) -> list[tuple[str, str, int]]:
        """``(severity, issue_type, count)``, rejections first, largest first."""
        counts = Counter((i["severity"], i["issue_type"]) for i in self.issues)
        return sorted(
            ((sev, kind, n) for (sev, kind), n in counts.items()),
            key=lambda item: (item[0] != REJECTED, -item[2]),
        )

    def add_issue(
        self,
        source_row: int,
        severity: str,
        issue_type: str,
        field_name: Optional[str],
        message: str,
    ) -> None:
        self.issues.append(
            _make_issue(self.raw, self.original, source_row, severity, issue_type, field_name, message)
        )


def _make_issue(
    raw: pd.DataFrame,
    original: pd.DataFrame,
    source_row: int,
    severity: str,
    issue_type: str,
    field_name: Optional[str],
    message: str,
) -> dict[str, Any]:
    raw_row = raw.loc[source_row]
    original_row = original.loc[source_row]
    return {
        "source_row": int(source_row),
        "severity": severity,
        "issue_type": issue_type,
        "field": field_name,
        "message": message,
        "facility_id": as_str(raw_row.get("facility_id"), 50),
        "epa_unit_id": as_str(raw_row.get("epa_unit_id"), 50),
        "reporting_year": as_str(raw_row.get("reporting_year"), 20),
        "raw_values": json.dumps(
            {str(k): _jsonable(v) for k, v in original_row.items()}, default=str
        ),
    }


class _IssueCollector:
    """Accumulates issues for masks of rows in the working frame."""

    def __init__(self, work: pd.DataFrame, raw: pd.DataFrame, original: pd.DataFrame):
        self.work = work
        self.raw = raw
        self.original = original
        self.issues: list[dict[str, Any]] = []
        self.rejected = pd.Series(False, index=work.index)
        self.warned = pd.Series(False, index=work.index)

    def add(
        self,
        mask: pd.Series,
        severity: str,
        issue_type: str,
        field_name: Optional[str],
        message: Callable[[Any], str],
    ) -> None:
        mask = mask.fillna(False).astype(bool)
        if not mask.any():
            return
        for index in mask[mask].index:
            source_row = int(self.work.at[index, "source_row"])
            self.issues.append(
                _make_issue(
                    self.raw,
                    self.original,
                    source_row,
                    severity,
                    issue_type,
                    field_name,
                    message(index),
                )
            )
        if severity == REJECTED:
            self.rejected = self.rejected | mask
        else:
            self.warned = self.warned | mask


def validate(df: pd.DataFrame, *, current_year: Optional[int] = None) -> ValidationResult:
    """Map, coerce and check every row of a source table.

    Rows are rejected for: a blank or unparseable facility ID, unit ID or year;
    a year outside CAMPD's reporting range; a non-numeric or negative metric;
    operating time above the hours in a year; or being superseded by a later
    row for the same unit-year. Rows are imported with a warning when a blank
    required metric is recorded as 0, or an optional value is cleared or
    truncated. Nothing is dropped without an issue.
    """
    current_year = current_year or date.today().year
    resolved, missing = resolve_columns(df)
    unrecognised = [str(c) for c in df.columns if c not in set(resolved.values())]

    original = df.reset_index(drop=True).copy()
    original.index = pd.RangeIndex(2, len(original) + 2, name="source_row")

    stats: dict[str, Any] = {
        "raw_records": int(len(df)),
        "absent_columns": [name for name in COLUMN_ALIASES if name not in resolved],
    }

    if missing or df.empty:
        stats.update(
            accepted_records=0, rejected_records=0, warning_records=0, reporting_years=[]
        )
        return ValidationResult(
            clean=pd.DataFrame(columns=["source_row", *COLUMN_ALIASES]),
            issues=[],
            stats=stats,
            resolved=resolved,
            unrecognised=unrecognised,
            missing_required=missing,
            raw=pd.DataFrame(),
            original=original,
        )

    # Keep only the columns we understand and rename them in one step. The
    # .copy() gives us a frame we own, so later work[col] = ... writes are real.
    work = df.loc[:, list(resolved.values())].copy().reset_index(drop=True)
    work.columns = list(resolved.keys())
    # Row 1 of the file is the header, so data row i is file row i + 2.
    work.insert(0, "source_row", np.arange(2, len(work) + 2))

    # Trim text and treat whitespace-only cells as missing.
    for column in resolved:
        if not pd.api.types.is_numeric_dtype(work[column]) and not pd.api.types.is_datetime64_any_dtype(work[column]):
            trimmed = work[column].astype("string").str.strip()
            work[column] = trimmed.mask(trimmed == "", pd.NA)

    # Trimmed source text, before any coercion: positionally (to build masks
    # against `work`) and by source row (to describe rows in issues).
    work_raw = work.copy()
    raw = work_raw.set_index("source_row", drop=False)
    collector = _IssueCollector(work, raw, original)

    def raw_value(index: Any, name: str) -> str:
        return str(work_raw.at[index, name])

    # --- keys -------------------------------------------------------------
    work["facility_id"] = _to_int_series(work["facility_id"])
    work["reporting_year"] = _to_int_series(work["reporting_year"])
    # Unit IDs are alphanumeric ("1A", "CS0AAB"); force text so that a file of
    # purely numeric IDs does not arrive as 1 vs "1" and duplicate its units.
    work["epa_unit_id"] = _as_text_series(work["epa_unit_id"])
    work["epa_unit_id"] = work["epa_unit_id"].mask(work["epa_unit_id"] == "", pd.NA)

    for name in KEY_FIELDS:
        label = FIELD_LABELS[name]
        blank = work_raw[name].isna()
        collector.add(
            blank, REJECTED, "missing_key", name, lambda i, label=label: f"{label} is blank."
        )
        invalid = ~blank & work[name].isna()
        collector.add(
            invalid,
            REJECTED,
            "invalid_value",
            name,
            lambda i, name=name, label=label: f"{label} '{raw_value(i, name)}' is not valid.",
        )

    year = work["reporting_year"]
    out_of_range = year.notna() & ((year < FIRST_REPORTING_YEAR) | (year > current_year))
    collector.add(
        out_of_range,
        REJECTED,
        "out_of_range",
        "reporting_year",
        lambda i: f"Reporting year {year[i]} is outside {FIRST_REPORTING_YEAR}-{current_year}.",
    )

    # --- numbers ------------------------------------------------------------
    for name in NUMERIC_FIELDS:
        if name not in work.columns:
            continue
        label = FIELD_LABELS[name]
        present = work_raw[name].notna()
        work[name] = _to_float_series(work[name])
        invalid = present & work[name].isna()
        if name in ("latitude", "longitude"):
            collector.add(
                invalid,
                WARNING,
                "invalid_value",
                name,
                lambda i, name=name, label=label: f"{label} '{raw_value(i, name)}' is not a number; left blank.",
            )
        else:
            collector.add(
                invalid,
                REJECTED,
                "invalid_value",
                name,
                lambda i, name=name, label=label: f"{label} '{raw_value(i, name)}' is not a number.",
            )

    for name in NON_NEGATIVE_FIELDS:
        if name not in work.columns:
            continue
        label = FIELD_LABELS[name]
        negative = work[name].lt(0)
        collector.add(
            negative,
            REJECTED,
            "negative_value",
            name,
            lambda i, name=name, label=label: f"{label} is negative ({_format_number(work.at[i, name])}).",
        )

    if "operating_time" in work.columns:
        too_long = work["operating_time"].gt(MAX_OPERATING_HOURS)
        collector.add(
            too_long,
            REJECTED,
            "out_of_range",
            "operating_time",
            lambda i: (
                f"Operating time {_format_number(work.at[i, 'operating_time'])} h exceeds "
                f"the {MAX_OPERATING_HOURS} hours in a year."
            ),
        )

    for name, low, high in (("latitude", -90, 90), ("longitude", -180, 180)):
        if name not in work.columns:
            continue
        bad = work[name].notna() & ((work[name] < low) | (work[name] > high))
        collector.add(
            bad,
            WARNING,
            "out_of_range",
            name,
            lambda i, name=name: f"{FIELD_LABELS[name]} {work.at[i, name]} is outside {low} to {high}; left blank.",
        )
        work[name] = work[name].mask(bad, pd.NA)

    # --- dates and text -------------------------------------------------------
    for name in DATE_FIELDS:
        if name not in work.columns:
            continue
        present = work_raw[name].notna()
        work[name] = _to_datetime_series(work[name])
        invalid = present & work[name].isna()
        collector.add(
            invalid,
            WARNING,
            "invalid_value",
            name,
            lambda i, name=name: f"{FIELD_LABELS[name]} '{raw_value(i, name)}' is not a date; left blank.",
        )

    if "state_code" in work.columns:
        state = work["state_code"].astype("string").str.upper()
        bad_state = state.notna() & ~state.fillna("").str.match(_STATE_CODE.pattern)
        collector.add(
            bad_state,
            WARNING,
            "invalid_value",
            "state_code",
            lambda i: f"State '{raw_value(i, 'state_code')}' is not a two-letter code; left blank.",
        )
        work["state_code"] = state.mask(bad_state, pd.NA)

    for name, limit in TEXT_LIMITS.items():
        if name not in work.columns or name == "state_code":
            continue
        lengths = work[name].astype("string").str.len()
        too_long = lengths.gt(limit)
        collector.add(
            too_long,
            WARNING,
            "truncated",
            name,
            lambda i, name=name, limit=limit: f"{FIELD_LABELS[name]} is longer than {limit} characters; truncated.",
        )

    # --- duplicates within the file -----------------------------------------
    # A unit-year is unique in the schema; keep the last occurrence so a
    # corrected row later in the file wins over the one it supersedes.
    candidates = work.loc[~collector.rejected]
    superseded = candidates.duplicated(subset=list(KEY_FIELDS), keep="last")
    if superseded.any():
        kept_row = candidates.groupby(list(KEY_FIELDS))["source_row"].transform("last")
        mask = pd.Series(False, index=work.index)
        mask.loc[superseded[superseded].index] = True
        collector.add(
            mask,
            REJECTED,
            "duplicate_in_file",
            None,
            lambda i: f"Same facility, unit and year as row {int(kept_row[i])}; the later row was kept.",
        )

    # --- blank required metrics -> 0 ----------------------------------------
    accepted = ~collector.rejected
    present_metrics = [name for name in REQUIRED_NUMERIC_FIELDS if name in work.columns]
    if present_metrics:
        blanks = work.loc[accepted, present_metrics].isna()
        has_blank = blanks.any(axis=1).reindex(work.index, fill_value=False)
        collector.add(
            has_blank,
            WARNING,
            "defaulted_to_zero",
            None,
            lambda i: "Blank "
            + ", ".join(FIELD_LABELS[n] for n in present_metrics if blanks.at[i, n])
            + " recorded as 0.",
        )
    for name in REQUIRED_NUMERIC_FIELDS:
        if name in work.columns:
            work[name] = work[name].fillna(0.0)
        else:
            work[name] = 0.0

    clean = (
        work.loc[~collector.rejected]
        .reindex(columns=["source_row", *COLUMN_ALIASES])
        .reset_index(drop=True)
    )

    result = ValidationResult(
        clean=clean,
        issues=collector.issues,
        stats=stats,
        resolved=resolved,
        unrecognised=unrecognised,
        missing_required=[],
        raw=raw,
        original=original,
    )
    _refresh_stats(result)
    return result


def _refresh_stats(result: ValidationResult) -> None:
    rejected_rows = {i["source_row"] for i in result.issues if i["severity"] == REJECTED}
    accepted_rows = set(result.clean["source_row"].astype(int)) if not result.clean.empty else set()
    warned_rows = {
        i["source_row"] for i in result.issues if i["severity"] == WARNING
    } & accepted_rows
    years = (
        sorted(int(y) for y in result.clean["reporting_year"].dropna().unique())
        if not result.clean.empty
        else []
    )
    result.stats.update(
        accepted_records=len(accepted_rows),
        rejected_records=len(rejected_rows),
        warning_records=len(warned_rows),
        reporting_years=years,
    )


def mark_already_stored(result: ValidationResult) -> int:
    """Reject rows whose unit-year is already in the database.

    Returns how many rows were moved from ``clean`` to the issue list. Safe to
    call more than once: a second call finds nothing left to move.
    """
    clean = result.clean
    if clean.empty:
        return 0

    years = sorted(int(y) for y in clean["reporting_year"].unique())
    stored = db.session.execute(
        select(
            Unit.facility_id,
            Unit.epa_unit_id,
            AnnualRecord.reporting_year,
            AnnualRecord.dataset_id,
        )
        .join(AnnualRecord, AnnualRecord.unit_id == Unit.internal_unit_key)
        .where(AnnualRecord.reporting_year.in_(years))
    ).all()
    if not stored:
        return 0
    existing = {(f, u, y): d for f, u, y, d in stored}

    hit_rows = []
    for position, row in enumerate(clean.itertuples(index=False)):
        key = (int(row.facility_id), as_text(row.epa_unit_id, "epa_unit_id"), int(row.reporting_year))
        dataset_id = existing.get(key)
        if dataset_id is not None:
            hit_rows.append(position)
            result.add_issue(
                int(row.source_row),
                REJECTED,
                "already_stored",
                None,
                f"This unit-year is already stored (dataset #{dataset_id}); the existing record was kept.",
            )

    if hit_rows:
        result.clean = clean.drop(index=hit_rows).reset_index(drop=True)
        _refresh_stats(result)
    return len(hit_rows)
