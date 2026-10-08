"""Stage source files for review and write approved imports to the database.

The import flow is the same for a user upload and a CAMPD API retrieval:

1. :func:`stage_bytes` keeps the file exactly as received under UPLOAD_FOLDER
   and records it in ``uploaded_files`` with status ``staged``.
2. :func:`validate_staged` re-reads it and produces a
   :class:`~app.services.validation.ValidationResult` for the review page.
3. On approval, :func:`ingest` writes the dataset, facilities, units, annual
   records, data-quality issues and provenance in one transaction.
   On cancel, :func:`discard_staged` deletes the file.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence

from flask import current_app
from sqlalchemy import func, insert, select

from app import db
from app.models import (
    AnnualRecord,
    DataProvenance,
    DataQualityIssue,
    Dataset,
    Facility,
    Unit,
    UploadedFile,
)
from app.services.validation import (
    ValidationResult,
    as_date,
    as_float,
    as_text,
    mark_already_stored,
    read_table,
    validate,
)

SOURCE_NAMES = {
    "api": "EPA Clean Air Markets Program Data (CAMPD) API - apportioned annual emissions",
    "upload": "User-uploaded file in CAMPD annual emissions format",
}

UNKNOWN = "Unknown"


# ---------------------------------------------------------------------------
# Staging
# ---------------------------------------------------------------------------


def upload_path(stored_filename: str) -> Path:
    return Path(current_app.config["UPLOAD_FOLDER"]) / stored_filename


def stage_bytes(
    data: bytes,
    *,
    original_filename: str,
    extension: str,
    source_type: str,
    dataset_name: Optional[str] = None,
    source_url: Optional[str] = None,
    query_params: Optional[dict[str, Any]] = None,
) -> UploadedFile:
    """Keep a received file under UPLOAD_FOLDER and record it as staged."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    stored_filename = f"{source_type}-{stamp}-{uuid.uuid4().hex[:8]}.{extension}"
    upload_path(stored_filename).write_bytes(data)

    staged = UploadedFile(
        source_type=source_type,
        original_filename=original_filename[:255],
        stored_filename=stored_filename,
        file_extension=extension,
        file_size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        status="staged",
        dataset_name=(dataset_name or "")[:150] or None,
        source_url=source_url,
        query_params=json.dumps(query_params) if query_params else None,
    )
    db.session.add(staged)
    db.session.commit()
    return staged


def validate_staged(staged: UploadedFile) -> ValidationResult:
    """Read a staged file and validate it against the schema and the database."""
    df = read_table(upload_path(staged.stored_filename), staged.file_extension)
    result = validate(df)
    mark_already_stored(result)
    return result


def discard_staged(staged: UploadedFile) -> None:
    """Delete a staged file that the user chose not to import."""
    remove_stored_file(staged)
    staged.status = "discarded"
    db.session.commit()


def remove_stored_file(staged: UploadedFile) -> None:
    path = upload_path(staged.stored_filename)
    if path.exists():
        path.unlink()


def earlier_import_of(staged: UploadedFile) -> Optional[UploadedFile]:
    """An already-imported file with identical contents, if there is one."""
    return db.session.scalars(
        select(UploadedFile)
        .where(
            UploadedFile.sha256 == staged.sha256,
            UploadedFile.status == "imported",
            UploadedFile.id != staged.id,
        )
        .limit(1)
    ).first()


def query_params_of(record: Any) -> dict[str, Any]:
    """Decode the JSON query_params column of an UploadedFile or DataProvenance."""
    if not getattr(record, "query_params", None):
        return {}
    try:
        return json.loads(record.query_params)
    except ValueError:
        return {}


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


def _chunks(values: Sequence[Any], size: int = 500) -> Iterator[Sequence[Any]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _format_years(years: list[int]) -> str:
    if not years:
        return ""
    if len(years) == 1:
        return str(years[0])
    if years == list(range(years[0], years[-1] + 1)):
        return f"{years[0]}-{years[-1]}"
    return ", ".join(str(y) for y in years)


def _summary(result: ValidationResult) -> str:
    stats = result.stats
    parts = [
        f"raw rows: {stats['raw_records']}",
        f"imported: {stats['accepted_records']}",
        f"rejected: {stats['rejected_records']}",
        f"imported with warnings: {stats['warning_records']}",
    ]
    for severity, issue_type, count in result.issue_counts():
        parts.append(f"{issue_type.replace('_', ' ')} ({severity}): {count}")
    if stats["absent_columns"]:
        parts.append("columns not in source: " + ", ".join(stats["absent_columns"]))
    return "; ".join(parts)


def _new_facility(facility_id: int, row: Any) -> Facility:
    return Facility(
        epa_facility_id=facility_id,
        facility_name=as_text(row.facility_name, "facility_name") or f"Facility {facility_id}",
        state_code=(as_text(row.state_code, "state_code") or "").upper(),
        county=as_text(row.county, "county"),
        latitude=as_float(row.latitude),
        longitude=as_float(row.longitude),
        source_category=as_text(row.source_category, "source_category"),
    )


def _fill_facility(facility: Facility, row: Any) -> None:
    """Fill attributes the stored facility is missing; never overwrite known values."""
    name = as_text(row.facility_name, "facility_name")
    if name and facility.facility_name == f"Facility {facility.epa_facility_id}":
        facility.facility_name = name
    state = as_text(row.state_code, "state_code")
    if state and not facility.state_code:
        facility.state_code = state.upper()
    for attr, value in (
        ("county", as_text(row.county, "county")),
        ("latitude", as_float(row.latitude)),
        ("longitude", as_float(row.longitude)),
        ("source_category", as_text(row.source_category, "source_category")),
    ):
        if value is not None and getattr(facility, attr) is None:
            setattr(facility, attr, value)


def _update_unit(unit: Unit, row: Any, *, is_latest_year: bool) -> None:
    """Keep a unit's type and fuel current as of its latest reporting year.

    Fuel and type can change over a unit's life (coal-to-gas conversions), and
    the schema keeps one value per unit, so the most recent year wins. Older
    years only fill values that are still unknown.
    """
    for attr in ("unit_type", "primary_fuel"):
        value = as_text(getattr(row, attr), attr)
        if value and (is_latest_year or getattr(unit, attr) in (None, "", UNKNOWN)):
            setattr(unit, attr, value)
    secondary = as_text(row.secondary_fuel, "secondary_fuel")
    if secondary and (is_latest_year or unit.secondary_fuel is None):
        unit.secondary_fuel = secondary
    for attr in ("operating_date", "retirement_date"):
        value = as_date(getattr(row, attr))
        if value is not None and getattr(unit, attr) is None:
            setattr(unit, attr, value)


def ingest(
    result: ValidationResult,
    *,
    dataset_name: str,
    data_source: str,
    source_type: str,
    original_filename: Optional[str] = None,
    uploaded_file: Optional[UploadedFile] = None,
    source_url: Optional[str] = None,
    query_params: Optional[dict[str, Any]] = None,
) -> Dataset:
    """Write a validated table to the database inside one transaction.

    Raises ValueError when no importable rows remain. The caller is
    responsible for rolling back on a database error.
    """
    # Re-check against the database: another import may have landed since
    # the review page was rendered.
    mark_already_stored(result)
    if result.clean.empty:
        raise ValueError("No rows are left to import.")

    clean = result.clean
    stats = result.stats
    years: list[int] = stats["reporting_years"]

    dataset = Dataset(
        dataset_name=dataset_name[:150],
        data_source=data_source[:150],
        # The column holds a single year; for a multi-year source record the
        # latest, and keep the full span in the provenance record.
        reporting_year=years[-1],
        original_filename=(original_filename or "")[:255] or None,
        raw_records=stats["raw_records"],
        accepted_records=0,
    )
    db.session.add(dataset)
    db.session.flush()  # assigns dataset.id

    # Load every facility and unit this table touches up front: one query per
    # 500 keys instead of one per row.
    facility_ids = sorted({int(v) for v in clean["facility_id"]})
    facilities: dict[int, Facility] = {}
    units: dict[tuple[int, str], Unit] = {}
    for chunk in _chunks(facility_ids):
        for facility in db.session.scalars(
            select(Facility).where(Facility.epa_facility_id.in_(chunk))
        ):
            facilities[facility.epa_facility_id] = facility
        for unit in db.session.scalars(select(Unit).where(Unit.facility_id.in_(chunk))):
            units[(unit.facility_id, unit.epa_unit_id)] = unit

    key_of = {unit.internal_unit_key: key for key, unit in units.items()}
    latest_year: dict[tuple[int, str], int] = {}
    for chunk in _chunks(list(key_of)):
        for unit_key, year in db.session.execute(
            select(AnnualRecord.unit_id, func.max(AnnualRecord.reporting_year))
            .where(AnnualRecord.unit_id.in_(chunk))
            .group_by(AnnualRecord.unit_id)
        ):
            latest_year[key_of[unit_key]] = year

    accepted = 0
    with db.session.no_autoflush:
        for row in clean.itertuples(index=False):
            facility_id = int(row.facility_id)
            epa_unit_id = as_text(row.epa_unit_id, "epa_unit_id")
            reporting_year = int(row.reporting_year)

            facility = facilities.get(facility_id)
            if facility is None:
                facility = _new_facility(facility_id, row)
                db.session.add(facility)
                facilities[facility_id] = facility
            else:
                _fill_facility(facility, row)

            key = (facility_id, epa_unit_id)
            unit = units.get(key)
            if unit is None:
                unit = Unit(
                    facility=facility,
                    epa_unit_id=epa_unit_id,
                    unit_type=as_text(row.unit_type, "unit_type") or UNKNOWN,
                    primary_fuel=as_text(row.primary_fuel, "primary_fuel") or UNKNOWN,
                    secondary_fuel=as_text(row.secondary_fuel, "secondary_fuel"),
                    operating_date=as_date(row.operating_date),
                    retirement_date=as_date(row.retirement_date),
                )
                db.session.add(unit)
                units[key] = unit
                latest_year[key] = reporting_year
            else:
                is_latest = reporting_year >= latest_year.get(key, reporting_year)
                _update_unit(unit, row, is_latest_year=is_latest)
                latest_year[key] = max(latest_year.get(key, reporting_year), reporting_year)

            db.session.add(
                AnnualRecord(
                    unit=unit,
                    dataset_id=dataset.id,
                    reporting_year=reporting_year,
                    operating_time=as_float(row.operating_time) or 0.0,
                    gross_load=as_float(row.gross_load) or 0.0,
                    steam_load=as_float(row.steam_load),
                    heat_input=as_float(row.heat_input) or 0.0,
                    co2_mass=as_float(row.co2_mass) or 0.0,
                    so2_mass=as_float(row.so2_mass) or 0.0,
                    nox_mass=as_float(row.nox_mass) or 0.0,
                    so2_control=as_text(row.so2_control, "so2_control"),
                    nox_control=as_text(row.nox_control, "nox_control"),
                    pm_control=as_text(row.pm_control, "pm_control"),
                    program_code=as_text(row.program_code, "program_code"),
                )
            )
            accepted += 1

    dataset.accepted_records = accepted
    dataset.notes = _summary(result)

    if result.issues:
        db.session.execute(
            insert(DataQualityIssue),
            [
                {**issue, "dataset_id": dataset.id}
                for issue in sorted(result.issues, key=lambda i: (i["source_row"], i["severity"]))
            ],
        )

    db.session.add(
        DataProvenance(
            dataset_id=dataset.id,
            source_type=source_type,
            source_name=SOURCE_NAMES.get(source_type, data_source),
            source_url=source_url,
            query_params=json.dumps(query_params) if query_params else None,
            original_filename=dataset.original_filename,
            file_sha256=uploaded_file.sha256 if uploaded_file else None,
            reporting_years=_format_years(years),
            raw_records=stats["raw_records"],
            accepted_records=accepted,
            rejected_records=stats["rejected_records"],
            warning_records=stats["warning_records"],
            notes=dataset.notes,
        )
    )

    if uploaded_file is not None:
        uploaded_file.dataset_id = dataset.id
        uploaded_file.status = "imported"

    db.session.commit()
    return dataset


def import_staged(staged: UploadedFile, *, dataset_name: Optional[str] = None) -> tuple[Dataset, ValidationResult]:
    """Validate and ingest a staged file in one step (used on approval and by the CLI)."""
    result = validate_staged(staged)
    if result.missing_required:
        raise ValueError("Missing required column(s): " + ", ".join(result.missing_required))
    if staged.source_type == "api":
        data_source = "CAMPD API"
    else:
        data_source = "Excel upload" if staged.file_extension == "xlsx" else "CSV upload"
    dataset = ingest(
        result,
        dataset_name=(dataset_name or staged.dataset_name or staged.original_filename),
        data_source=data_source,
        source_type=staged.source_type,
        original_filename=staged.original_filename,
        uploaded_file=staged,
        source_url=staged.source_url,
        query_params=query_params_of(staged) or None,
    )
    return dataset, result


# ---------------------------------------------------------------------------
# Attribute enrichment
# ---------------------------------------------------------------------------


def enrich_from_attributes(frame) -> tuple[int, int]:
    """Fill blank facility and unit attributes from a CAMPD attributes table.

    Used for facilities imported from sources without location data (e.g. an
    older CSV export). Known values are never overwritten. Returns the number
    of facilities and units changed.
    """
    import pandas as pd

    from app.services.validation import COLUMN_ALIASES, DATE_FIELDS, resolve_columns

    if frame.empty:
        return 0, 0
    resolved, missing = resolve_columns(frame)
    if missing:
        raise ValueError("Attribute table lacks " + ", ".join(missing))

    work = frame[list(resolved.values())].copy()
    work.columns = list(resolved)
    work = work.reindex(columns=list(COLUMN_ALIASES))
    for name in DATE_FIELDS:
        work[name] = pd.to_datetime(work[name], errors="coerce", format="mixed")

    facility_ids = sorted({int(v) for v in work["facility_id"].dropna()})
    facilities: dict[int, Facility] = {}
    units: dict[tuple[int, str], Unit] = {}
    for chunk in _chunks(facility_ids):
        for facility in db.session.scalars(
            select(Facility).where(Facility.epa_facility_id.in_(chunk))
        ):
            facilities[facility.epa_facility_id] = facility
        for unit in db.session.scalars(select(Unit).where(Unit.facility_id.in_(chunk))):
            units[(unit.facility_id, unit.epa_unit_id)] = unit

    changed_facilities: set[int] = set()
    changed_units: set[int] = set()
    with db.session.no_autoflush:
        for row in work.itertuples(index=False):
            if pd.isna(row.facility_id):
                continue
            facility = facilities.get(int(row.facility_id))
            if facility is None:
                continue
            _fill_facility(facility, row)
            if db.session.is_modified(facility):
                changed_facilities.add(facility.epa_facility_id)
            unit = units.get((facility.epa_facility_id, as_text(row.epa_unit_id, "epa_unit_id")))
            if unit is not None:
                _update_unit(unit, row, is_latest_year=False)
                if db.session.is_modified(unit):
                    changed_units.add(unit.internal_unit_key)
    db.session.commit()
    return len(changed_facilities), len(changed_units)
