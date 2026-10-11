"""Validation and ingestion: every row is imported, or reported with a reason."""

import io
import json

import pytest
from sqlalchemy import func, select

from app import db
from app.models import AnnualRecord, DataProvenance, DataQualityIssue, Facility, Unit
from app.services.ingest import ingest
from app.services.validation import (
    mark_already_stored,
    normalise_header,
    read_table,
    resolve_columns,
    validate,
)

from tests.conftest import sample_csv


def _validate(data: bytes):
    return validate(read_table(io.BytesIO(data), "csv"))


def _issues_by_row(result):
    by_row = {}
    for issue in result.issues:
        by_row.setdefault(issue["source_row"], set()).add((issue["severity"], issue["issue_type"]))
    return by_row


def test_header_normalisation_accepts_display_and_api_spellings():
    assert normalise_header("Gross Load (MWh)") == normalise_header("grossLoad") == "grossload"
    assert normalise_header("CO2 Mass (short tons)") == "co2mass"


def test_resolve_columns_reports_missing_required_fields():
    import pandas as pd

    resolved, missing = resolve_columns(pd.DataFrame(columns=["Facility ID", "Gross Load"]))
    assert resolved["facility_id"] == "Facility ID"
    assert missing == ["epa_unit_id", "reporting_year"]


def test_every_row_is_imported_or_reported(app):
    result = _validate(sample_csv())
    by_row = _issues_by_row(result)

    assert result.stats["raw_records"] == 10
    assert sorted(result.clean["source_row"]) == [3, 8, 10]
    assert result.stats["accepted_records"] == 3
    assert result.stats["rejected_records"] == 7

    assert ("rejected", "duplicate_in_file") in by_row[2]
    assert ("warning", "defaulted_to_zero") in by_row[3]
    assert ("rejected", "missing_key") in by_row[4]
    assert ("rejected", "invalid_value") in by_row[5]
    assert ("rejected", "negative_value") in by_row[6]
    assert ("rejected", "out_of_range") in by_row[7]
    assert ("rejected", "out_of_range") in by_row[9]
    assert ("warning", "invalid_value") in by_row[10]
    assert ("rejected", "invalid_value") in by_row[11]

    # No row disappears without a trace.
    rows_in_file = set(range(2, 12))
    accounted = set(result.clean["source_row"]) | {
        i["source_row"] for i in result.issues if i["severity"] == "rejected"
    }
    assert accounted == rows_in_file


def test_issue_keeps_the_original_row(app):
    result = _validate(sample_csv())
    issue = next(i for i in result.issues if i["source_row"] == 11)
    original = json.loads(issue["raw_values"])
    assert original["Gross Load (MWh)"] == "lots"
    assert issue["facility_id"] == "600"


def test_missing_required_column_blocks_import(app):
    result = _validate(b"Facility ID,Year\n1,2022\n")
    assert result.missing_required == ["epa_unit_id"]
    assert not result.can_import


def test_ingest_writes_records_issues_and_provenance(sample_dataset):
    assert sample_dataset.accepted_records == 3
    assert db.session.scalar(select(func.count(AnnualRecord.id))) == 3
    assert db.session.scalar(select(func.count(DataQualityIssue.id))) >= 9
    provenance = db.session.scalars(select(DataProvenance)).one()
    assert provenance.rejected_records == 7
    assert provenance.warning_records == 2

    kept = db.session.scalars(
        select(AnnualRecord).join(Unit).where(Unit.facility_id == 100, Unit.epa_unit_id == "1")
    ).one()
    assert kept.co2_mass == 600000  # row 8 superseded row 2
    facility = db.session.get(Facility, 500)
    assert facility.state_code == ""  # "Kentucky" was cleared, with a warning


def test_reimport_marks_rows_already_stored(sample_dataset):
    result = _validate(sample_csv())
    moved = mark_already_stored(result)
    assert moved == 3
    assert result.clean.empty
    assert {i["issue_type"] for i in result.issues} >= {"already_stored"}
    with pytest.raises(ValueError):
        ingest(result, dataset_name="again", data_source="CSV upload", source_type="upload")


def test_later_import_fills_blank_facility_attributes(sample_dataset):
    rows = [
        "Plant E,500,1,2023,KY,Fayette County,100,100,200,1,1,100,Natural Gas,Combustion turbine,",
    ]
    result = _validate(sample_csv(rows))
    ingest(result, dataset_name="2023", data_source="CSV upload", source_type="upload")
    facility = db.session.get(Facility, 500)
    assert facility.state_code == "KY"
    assert facility.county == "Fayette County"


def test_owner_and_hg_controls_from_upload_headers(app):
    """CAMPD download headers map to owner and Hg controls; the newest year's owner wins."""

    def load(year, owner, hg_control):
        csv = f"Facility ID,Unit ID,Year,Owner/Operator,Hg Controls\n100,1,{year},{owner},{hg_control}\n"
        ingest(_validate(csv.encode()), dataset_name=str(year), data_source="CSV upload",
               source_type="upload")

    load(2022, "Old Power Co (Owner)", "Activated Carbon Injection")
    load(2024, "New Power Co (Owner)|New Power Co (Operator)", "")
    load(2023, "Middle Power Co (Owner)", "")

    unit = db.session.scalars(select(Unit)).one()
    assert unit.owner_operator == "New Power Co (Owner)|New Power Co (Operator)"
    hg = dict(db.session.execute(select(AnnualRecord.reporting_year, AnnualRecord.hg_control)).all())
    assert hg == {2022: "Activated Carbon Injection", 2023: None, 2024: None}


def test_database_rejects_impossible_values(app, sample_dataset):
    from sqlalchemy.exc import IntegrityError

    record = db.session.scalars(select(AnnualRecord)).first()
    record.co2_mass = -1
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_foreign_keys_are_enforced(app):
    from sqlalchemy.exc import IntegrityError

    db.session.add(Unit(facility_id=999999, epa_unit_id="X", unit_type="t", primary_fuel="f"))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()
