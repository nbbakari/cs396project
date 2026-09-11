"""CSV / Excel ingestion pipeline for EPA CAMPD extracts.

The upload route turns an uploaded file into rows across four tables:

* one :class:`~app.models.Dataset` row recording the ingestion run,
* a :class:`~app.models.Facility` per distinct EPA facility ID,
* a :class:`~app.models.Unit` per ``(facility_id, epa_unit_id)`` pair,
* one :class:`~app.models.AnnualRecord` per unit-year.

Everything happens inside a single transaction, so a failure part-way through
leaves no partial dataset behind.

Two pandas 3.x behaviours shape the code below:

* **Copy-on-Write is always on and can no longer be disabled.** Chained
  assignment (``df[mask]["col"] = ...``) silently writes to a temporary, so
  every mutation here either reassigns the frame or writes through ``df[col]``
  on a frame we own outright (``.copy()`` after the initial column selection).
* **Blank cells arrive as ``pd.NA``, not ``None`` or ``float('nan')``**, and
  ``itertuples`` yields numpy scalars (``numpy.int64``, ``numpy.float64``)
  rather than Python builtins. psycopg2 cannot adapt either, so every value is
  pushed back through the ``_as_*`` coercion helpers before it reaches the ORM.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Optional

import pandas as pd
from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from app import db
from app.models import AnnualRecord, Dataset, Facility, Unit

upload_bp = Blueprint("upload", __name__)

ALLOWED_EXTENSIONS = {"csv", "xlsx"}

# Canonical field name -> header spellings we accept, already normalised by
# _normalise_header(). Covers both the CAMPD bulk-download display headers
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
    "program_code": ("programcode", "programcodeinfo", "programcodes", "programs"),
}

# Without these three a row cannot be placed in the schema at all.
REQUIRED_FIELDS = ("facility_id", "epa_unit_id", "reporting_year")

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
# Zero is the right reading for a unit that did not run in the reporting year.
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
# over-long value cannot fail the whole transaction on PostgreSQL.
_TEXT_LIMITS = {
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
    "program_code": 100,
}

_PARENTHETICAL = re.compile(r"\([^)]*\)")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


# ---------------------------------------------------------------------------
# Header resolution
# ---------------------------------------------------------------------------


def _normalise_header(name: object) -> str:
    """Reduce a header to a comparable key.

    Drops parenthesised units so that ``"Gross Load (MWh)"``, ``"Gross Load"``
    and ``"grossLoad"`` all collapse to ``"grossload"``.
    """
    text = _PARENTHETICAL.sub(" ", str(name)).lower()
    return _NON_ALNUM.sub("", text)


def _resolve_columns(df: pd.DataFrame) -> tuple[dict[str, str], list[str]]:
    """Map canonical field names onto the columns actually present.

    Returns ``(resolved, missing_required)``. Each source column is claimed by
    at most one field, so a file carrying both "Unit ID" and "Unit Name" does
    not bind them both to ``epa_unit_id``.
    """
    lookup: dict[str, str] = {}
    for column in df.columns:
        lookup.setdefault(_normalise_header(column), column)

    resolved: dict[str, str] = {}
    claimed: set[str] = set()
    for field, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            column = lookup.get(alias)
            if column is not None and column not in claimed:
                resolved[field] = column
                claimed.add(column)
                break

    missing = [field for field in REQUIRED_FIELDS if field not in resolved]
    return resolved, missing


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _read_dataframe(storage: FileStorage, extension: str) -> pd.DataFrame:
    """Parse an uploaded file into a DataFrame.

    ``dtype_backend="numpy_nullable"`` puts every column on a pandas nullable
    dtype, so a blank cell is ``pd.NA`` in text and numeric columns alike
    instead of the ``NaN`` / ``None`` / ``""`` mixture the default backend
    produces -- one missing-value check then covers the whole frame.
    """
    stream = storage.stream
    stream.seek(0)

    if extension == "csv":
        try:
            # utf-8-sig strips the BOM that Excel writes when saving as CSV.
            return pd.read_csv(
                stream,
                dtype_backend="numpy_nullable",
                skipinitialspace=True,
                encoding="utf-8-sig",
            )
        except UnicodeDecodeError:
            stream.seek(0)
            return pd.read_csv(
                stream,
                dtype_backend="numpy_nullable",
                skipinitialspace=True,
                encoding="latin-1",
            )

    return pd.read_excel(stream, engine="openpyxl", dtype_backend="numpy_nullable")


# ---------------------------------------------------------------------------
# Cleaning
# ---------------------------------------------------------------------------


def _to_float_series(series: pd.Series) -> pd.Series:
    """Coerce a column to nullable float, tolerating thousands separators."""
    if pd.api.types.is_string_dtype(series):
        series = series.str.replace(",", "", regex=False)
    return pd.to_numeric(series, errors="coerce").astype("Float64")


def _to_int_series(series: pd.Series) -> pd.Series:
    """Coerce a column to nullable integer."""
    if pd.api.types.is_string_dtype(series):
        series = series.str.replace(",", "", regex=False)
    return pd.to_numeric(series, errors="coerce").round().astype("Int64")


def _to_datetime_series(series: pd.Series) -> pd.Series:
    """Coerce a column to datetimes, leaving unparseable values as NaT."""
    try:
        return pd.to_datetime(series, errors="coerce", format="mixed")
    except (ValueError, TypeError):
        return pd.to_datetime(series, errors="coerce")


def clean_dataframe(
    df: pd.DataFrame, resolved: dict[str, str]
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Normalise, validate and filter a raw upload.

    Returns the cleaned frame -- reindexed to every canonical column, so
    callers can read any field off a row without ``getattr`` guards -- plus a
    stats dict describing what was rejected and why.
    """
    stats: dict[str, Any] = {"raw_records": int(len(df))}

    # Keep only the columns we understand and rename them in one step. The
    # .copy() gives us a frame we own, so later df[col] = ... writes are real
    # writes rather than Copy-on-Write no-ops on a view.
    df = df.loc[:, list(resolved.values())].copy()
    df.columns = list(resolved.keys())

    # Trim text and treat whitespace-only cells as missing.
    for column in df.columns:
        if pd.api.types.is_string_dtype(df[column]):
            trimmed = df[column].str.strip()
            df[column] = trimmed.mask(trimmed == "", pd.NA)

    df["facility_id"] = _to_int_series(df["facility_id"])
    df["reporting_year"] = _to_int_series(df["reporting_year"])
    # Unit IDs are alphanumeric ("1A", "CS0AAB"); force text so that a file of
    # purely numeric IDs does not arrive as 1 vs "1" and duplicate its units.
    df["epa_unit_id"] = df["epa_unit_id"].astype("string").str.strip()

    for field in NUMERIC_FIELDS:
        if field in df.columns:
            df[field] = _to_float_series(df[field])

    for field in DATE_FIELDS:
        if field in df.columns:
            df[field] = _to_datetime_series(df[field])

    # Rows without a facility, unit or year cannot be placed in the schema.
    before = len(df)
    df = df.dropna(subset=["facility_id", "epa_unit_id", "reporting_year"])
    df = df.loc[df["epa_unit_id"] != ""]
    stats["dropped_missing_keys"] = before - len(df)

    # Reject physically impossible negatives.
    negative = pd.Series(False, index=df.index)
    for field in NON_NEGATIVE_FIELDS:
        if field in df.columns:
            negative |= df[field].lt(0).fillna(False).astype(bool)
    stats["dropped_negative"] = int(negative.sum())
    df = df.loc[~negative]

    # A unit-year is unique in the schema; keep the last occurrence so a
    # corrected row later in the file wins over the one it supersedes.
    before = len(df)
    df = df.drop_duplicates(
        subset=["facility_id", "epa_unit_id", "reporting_year"], keep="last"
    )
    stats["dropped_duplicates"] = before - len(df)

    # Fill the NOT NULL metrics.
    substituted = 0
    for field in REQUIRED_NUMERIC_FIELDS:
        if field in df.columns:
            substituted += int(df[field].isna().sum())
            df[field] = df[field].fillna(0.0)
        else:
            df[field] = 0.0
            substituted += len(df)
    stats["substituted_zero"] = substituted
    stats["absent_columns"] = [
        field for field in COLUMN_ALIASES if field not in resolved
    ]

    # Give every canonical column a place, so row.county is always readable.
    df = df.reindex(columns=list(COLUMN_ALIASES.keys())).reset_index(drop=True)
    stats["clean_records"] = int(len(df))
    return df, stats


# ---------------------------------------------------------------------------
# Scalar coercion (pandas/numpy -> Python builtins for psycopg2)
# ---------------------------------------------------------------------------


def _as_int(value: Any) -> Optional[int]:
    if value is None or pd.isna(value):
        return None
    return int(value)


def _as_float(value: Any) -> Optional[float]:
    if value is None or pd.isna(value):
        return None
    return float(value)


def _as_str(value: Any, limit: Optional[int] = None) -> Optional[str]:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:limit] if limit else text


def _as_date(value: Any) -> Optional[date]:
    if value is None or pd.isna(value):
        return None
    return pd.Timestamp(value).date()


def _required_float(value: Any) -> float:
    """Coerce a NOT NULL metric, treating a missing value as zero."""
    coerced = _as_float(value)
    return 0.0 if coerced is None else coerced


def _text(value: Any, field: str) -> Optional[str]:
    """Coerce text to the column width declared in app.models."""
    return _as_str(value, _TEXT_LIMITS.get(field))


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _format_notes(stats: dict[str, Any]) -> str:
    parts = [
        f"raw rows: {stats['raw_records']}",
        f"rejected (missing keys): {stats['dropped_missing_keys']}",
        f"rejected (negative values): {stats['dropped_negative']}",
        f"dropped (duplicate unit-year in file): {stats['dropped_duplicates']}",
        f"skipped (unit-year already stored): {stats.get('skipped_existing', 0)}",
        f"missing metrics defaulted to 0: {stats['substituted_zero']}",
    ]
    if stats["absent_columns"]:
        parts.append("columns not present in file: " + ", ".join(stats["absent_columns"]))
    return "; ".join(parts)


def ingest_dataframe(
    df: pd.DataFrame,
    stats: dict[str, Any],
    *,
    dataset_name: str,
    data_source: str,
    original_filename: str,
) -> Dataset:
    """Write a cleaned frame to the database inside one transaction.

    The caller is responsible for rolling back on error.
    """
    years = df["reporting_year"].dropna().astype(int)
    distinct_years = sorted(years.unique().tolist())

    dataset = Dataset(
        dataset_name=dataset_name,
        data_source=data_source,
        # The column holds a single year; for a multi-year file record the
        # latest and note the full span.
        reporting_year=int(distinct_years[-1]),
        original_filename=original_filename,
        raw_records=stats["raw_records"],
        accepted_records=0,
        notes=None,
    )
    db.session.add(dataset)
    db.session.flush()  # assigns dataset.id for the AnnualRecord FK

    # Per-request caches: the spec's check-before-insert is one query per row,
    # which for a file of 50k rows covering 300 facilities is 50k queries for
    # 300 answers. The cache keeps the same semantics at one query per new key.
    facilities: dict[int, Facility] = {}
    units: dict[tuple[int, str], Unit] = {}
    created_unit_keys: set[tuple[int, str]] = set()

    accepted = 0
    skipped_existing = 0

    for row in df.itertuples(index=False):
        facility_id = _as_int(row.facility_id)
        epa_unit_id = _text(row.epa_unit_id, "epa_unit_id")
        reporting_year = _as_int(row.reporting_year)

        facility = facilities.get(facility_id)
        if facility is None:
            facility = Facility.query.filter_by(epa_facility_id=facility_id).first()
        if facility is None:
            facility = Facility(
                epa_facility_id=facility_id,
                facility_name=_text(row.facility_name, "facility_name")
                or f"Facility {facility_id}",
                # CAMPD publishes a two-letter code; anything longer is
                # truncated to fit state_code's width.
                state_code=(_text(row.state_code, "state_code") or "").upper(),
                county=_text(row.county, "county"),
                latitude=_as_float(row.latitude),
                longitude=_as_float(row.longitude),
                source_category=_text(row.source_category, "source_category"),
            )
            db.session.add(facility)
            db.session.flush()
        facilities[facility_id] = facility

        unit_key = (facility_id, epa_unit_id)
        unit = units.get(unit_key)
        if unit is None:
            unit = Unit.query.filter_by(
                facility_id=facility_id, epa_unit_id=epa_unit_id
            ).first()
        if unit is None:
            unit = Unit(
                facility_id=facility_id,
                epa_unit_id=epa_unit_id,
                unit_type=_text(row.unit_type, "unit_type") or "Unknown",
                primary_fuel=_text(row.primary_fuel, "primary_fuel") or "Unknown",
                secondary_fuel=_text(row.secondary_fuel, "secondary_fuel"),
                operating_date=_as_date(row.operating_date),
                retirement_date=_as_date(row.retirement_date),
            )
            db.session.add(unit)
            db.session.flush()  # assigns internal_unit_key
            created_unit_keys.add(unit_key)
        units[unit_key] = unit

        # A unit created during this run cannot already own an annual record,
        # so only pre-existing units need the uniqueness check.
        if unit_key not in created_unit_keys:
            already_stored = AnnualRecord.query.filter_by(
                unit_id=unit.internal_unit_key, reporting_year=reporting_year
            ).first()
            if already_stored is not None:
                skipped_existing += 1
                continue

        db.session.add(
            AnnualRecord(
                unit_id=unit.internal_unit_key,
                dataset_id=dataset.id,
                reporting_year=reporting_year,
                operating_time=_required_float(row.operating_time),
                gross_load=_required_float(row.gross_load),
                steam_load=_as_float(row.steam_load),
                heat_input=_required_float(row.heat_input),
                co2_mass=_required_float(row.co2_mass),
                so2_mass=_required_float(row.so2_mass),
                nox_mass=_required_float(row.nox_mass),
                so2_control=_text(row.so2_control, "so2_control"),
                nox_control=_text(row.nox_control, "nox_control"),
                pm_control=_text(row.pm_control, "pm_control"),
                program_code=_text(row.program_code, "program_code"),
            )
        )
        accepted += 1

    stats["skipped_existing"] = skipped_existing
    stats["accepted_records"] = accepted
    stats["reporting_years"] = distinct_years

    dataset.accepted_records = accepted
    notes = _format_notes(stats)
    if len(distinct_years) > 1:
        notes = f"reporting years {distinct_years[0]}-{distinct_years[-1]}; " + notes
    dataset.notes = notes

    db.session.commit()
    return dataset


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@upload_bp.route("/upload", methods=["GET"])
def upload_form():
    """Render the upload form."""
    return render_template("upload.html")


@upload_bp.route("/upload", methods=["POST"])
def upload_file():
    """Validate, clean and ingest an uploaded CAMPD extract."""
    uploaded = request.files.get("data_file")
    if uploaded is None or not uploaded.filename:
        flash("Choose a .csv or .xlsx file to upload.", "warning")
        return redirect(url_for("upload.upload_form"))

    filename = secure_filename(uploaded.filename)
    if not filename:
        flash("That filename could not be processed. Rename the file and retry.", "danger")
        return redirect(url_for("upload.upload_form"))

    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if extension not in ALLOWED_EXTENSIONS:
        flash(
            f"Unsupported file type '.{extension}'. Upload a .csv or .xlsx file.",
            "danger",
        )
        return redirect(url_for("upload.upload_form"))

    # pandas raises a wide range of exception types on malformed input
    # (ParserError, ValueError, UnicodeDecodeError, zipfile.BadZipFile from
    # openpyxl), so the parse is guarded broadly and logged with a traceback.
    try:
        df = _read_dataframe(uploaded, extension)
    except Exception as exc:  # noqa: BLE001 - arbitrary user-supplied file
        current_app.logger.exception("Could not parse upload %s", filename)
        flash(f"Could not read {filename}: {exc}", "danger")
        return redirect(url_for("upload.upload_form"))

    if df.empty:
        flash(f"{filename} contains no data rows.", "warning")
        return redirect(url_for("upload.upload_form"))

    resolved, missing = _resolve_columns(df)
    if missing:
        flash(
            "Missing required column(s): "
            + ", ".join(missing)
            + ". Columns found: "
            + ", ".join(str(column) for column in df.columns),
            "danger",
        )
        return redirect(url_for("upload.upload_form"))

    clean, stats = clean_dataframe(df, resolved)
    if clean.empty:
        flash(
            f"No usable rows in {filename}: all {stats['raw_records']} row(s) were "
            "rejected (missing facility/unit/year, or negative metrics).",
            "warning",
        )
        return redirect(url_for("upload.upload_form"))

    dataset_name = (request.form.get("dataset_name") or "").strip() or filename
    data_source = "Excel upload" if extension == "xlsx" else "CSV upload"

    try:
        dataset = ingest_dataframe(
            clean,
            stats,
            dataset_name=dataset_name[:150],
            data_source=data_source,
            original_filename=filename[:255],
        )
    except SQLAlchemyError as exc:
        db.session.rollback()
        current_app.logger.exception("Database error ingesting %s", filename)
        flash(f"Database error while saving {filename}: {exc}", "danger")
        return redirect(url_for("upload.upload_form"))

    flash(
        f"Ingested {filename}: {dataset.accepted_records:,} annual record(s) accepted "
        f"from {stats['raw_records']:,} row(s) (dataset #{dataset.id}).",
        "success",
    )

    rejected = stats["dropped_missing_keys"] + stats["dropped_negative"]
    if rejected:
        flash(
            f"{rejected:,} row(s) rejected: {stats['dropped_missing_keys']:,} missing "
            f"facility/unit/year, {stats['dropped_negative']:,} with negative metrics.",
            "warning",
        )
    if stats["dropped_duplicates"]:
        flash(
            f"{stats['dropped_duplicates']:,} duplicate unit-year row(s) in the file; "
            "the last occurrence of each was kept.",
            "info",
        )
    if stats["skipped_existing"]:
        flash(
            f"{stats['skipped_existing']:,} unit-year(s) were already stored and were "
            "left unchanged.",
            "info",
        )
    if stats["substituted_zero"]:
        flash(
            f"{stats['substituted_zero']:,} missing metric value(s) were recorded as 0.",
            "info",
        )

    return redirect(url_for("upload.upload_form"))


@upload_bp.app_errorhandler(413)
def handle_oversized_upload(error):  # noqa: ARG001 - Flask passes the exception
    """Turn Werkzeug's 413 into a flash rather than a bare error page."""
    limit = current_app.config.get("MAX_CONTENT_LENGTH") or 0
    flash(f"That file is larger than the {limit // (1024 * 1024)} MB upload limit.", "danger")
    return redirect(url_for("upload.upload_form"))
