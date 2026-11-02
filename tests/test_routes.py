"""Pages, the upload/review/approve flow, retrieval, and downloads."""

import csv
import io
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import func, select

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
from app.services import campd

from tests.conftest import sample_csv


def _upload(client, data=None, name="sample.csv", **form):
    return client.post(
        "/upload",
        data={"data_file": (io.BytesIO(data or sample_csv()), name), **form},
        content_type="multipart/form-data",
    )


def _csv_rows(response):
    text = response.get_data(as_text=True).lstrip("﻿")
    return list(csv.reader(io.StringIO(text)))


# --- upload, review, approve, cancel -------------------------------------------


def test_upload_stages_without_importing(client, app):
    response = _upload(client)
    assert response.status_code == 302
    staged = db.session.scalars(select(UploadedFile)).one()
    assert staged.status == "staged"
    assert Path(app.config["UPLOAD_FOLDER"], staged.stored_filename).exists()
    assert db.session.scalar(select(func.count(AnnualRecord.id))) == 0

    page = client.get(response.headers["Location"])
    html = page.get_data(as_text=True)
    assert page.status_code == 200
    assert "Approve and import 3 record(s)" in html
    assert "negative" in html and "duplicate in file" in html


def test_review_report_lists_every_issue(client):
    staged_url = _upload(client).headers["Location"]
    rows = _csv_rows(client.get(staged_url + "/report?format=csv"))
    assert rows[0][:3] == ["source_row", "severity", "issue_type"]
    assert {r[0] for r in rows[1:]} >= {"2", "4", "5", "6", "7", "9", "11"}


def test_approve_imports_and_keeps_report(client):
    staged_url = _upload(client, dataset_name="My upload").headers["Location"]
    response = client.post(staged_url + "/confirm", data={"dataset_name": "Approved name"})
    assert response.status_code == 302
    dataset = db.session.scalars(select(Dataset)).one()
    assert dataset.dataset_name == "Approved name"
    assert dataset.accepted_records == 3
    assert dataset.uploaded_file.status == "imported"
    assert dataset.provenance.source_type == "upload"
    assert db.session.scalar(select(func.count(DataQualityIssue.id))) >= 9

    detail = client.get(response.headers["Location"])
    assert detail.status_code == 200
    assert "Approved name" in detail.get_data(as_text=True)

    # Approving twice is refused.
    again = client.post(staged_url + "/confirm")
    assert db.session.scalar(select(func.count(Dataset.id))) == 1
    assert again.status_code == 302


def test_cancel_discards_file(client, app):
    staged_url = _upload(client).headers["Location"]
    staged = db.session.scalars(select(UploadedFile)).one()
    client.post(staged_url + "/cancel")
    db.session.refresh(staged)
    assert staged.status == "discarded"
    assert not Path(app.config["UPLOAD_FOLDER"], staged.stored_filename).exists()
    assert db.session.scalar(select(func.count(Dataset.id))) == 0


def test_upload_rejects_wrong_extension(client):
    response = _upload(client, b"x", name="notes.txt")
    assert response.status_code == 302
    assert db.session.scalar(select(func.count(UploadedFile.id))) == 0


def test_missing_required_column_shows_blocked_review(client):
    staged_url = _upload(client, b"Facility ID,Year\n1,2022\n").headers["Location"]
    html = client.get(staged_url).get_data(as_text=True)
    assert "cannot be imported" in html
    assert "Approve and import" not in html


def test_excel_upload(client):
    frame = pd.read_csv(io.BytesIO(sample_csv()))
    buffer = io.BytesIO()
    frame.to_excel(buffer, index=False)
    staged_url = _upload(client, buffer.getvalue(), name="sample.xlsx").headers["Location"]
    client.post(staged_url + "/confirm")
    assert db.session.scalars(select(Dataset)).one().data_source == "Excel upload"


# --- pages ------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "/",
        "/upload",
        "/retrieve",
        "/explorer",
        "/explorer?state=KY&primary_fuel=coal&co2_min=100&sort=co2_mass",
        "/explorer?level=facility&group_by=state&sort=co2_mass",
        "/explorer?year=abc",
        "/compare",
        "/compare?facility_id=100,500&metric=nox_mass",
        "/downloads",
    ],
)
def test_pages_render(client, sample_dataset, url):
    assert client.get(url).status_code == 200


def test_detail_pages_render(client, sample_dataset):
    unit = db.session.scalars(select(Unit)).first()
    assert client.get(f"/facility/{unit.facility_id}").status_code == 200
    assert client.get(f"/unit/{unit.internal_unit_key}").status_code == 200
    assert client.get(f"/compare?unit={unit.internal_unit_key}").status_code == 200
    assert client.get(f"/dataset/{sample_dataset.id}?severity=rejected").status_code == 200
    assert client.get("/unit/999999").status_code == 404


def test_explorer_shows_query_and_results(client, sample_dataset):
    html = client.get("/explorer?state=KY&primary_fuel=coal").get_data(as_text=True)
    assert "state_code = &#39;KY&#39;" in html
    assert "Plant A" in html


# --- downloads ---------------------------------------------------------------


def test_search_download_matches_search(client, sample_dataset):
    rows = _csv_rows(client.get("/download/records?state=KY&format=csv"))
    header, body = rows[0], rows[1:]
    assert header[0] == "facility_id" and "co2_mass_short_tons" in header
    assert len(body) == 2  # rows 3 and 8; row 10's state was cleared


def test_downloads_of_reports_and_provenance(client, sample_dataset):
    rejected = _csv_rows(client.get(f"/download/issues?dataset={sample_dataset.id}&severity=rejected"))
    assert len(rejected) - 1 == 7
    provenance = _csv_rows(client.get("/download/provenance"))
    assert provenance[1][1] == "sample"
    xlsx = client.get("/download/records?format=xlsx")
    assert xlsx.mimetype.endswith("spreadsheetml.sheet")
    assert pd.read_excel(io.BytesIO(xlsx.data)).shape[0] == 3


def test_original_file_download(client):
    staged_url = _upload(client).headers["Location"]
    client.post(staged_url + "/confirm")
    dataset = db.session.scalars(select(Dataset)).one()
    response = client.get(f"/download/dataset/{dataset.id}/original")
    assert response.data == sample_csv()


# --- JSON API ------------------------------------------------------------------


def test_records_api_uses_search_parameters(client, sample_dataset):
    data = client.get("/api/emissions/records?state=KY&sort=co2_mass").get_json()
    assert data["total"] == 2
    assert data["records"][0]["co2_mass"] == 600000
    assert client.get("/api/emissions/records?year=abc").status_code == 400


def test_top_facilities_api(client, sample_dataset):
    data = client.get("/api/emissions/top-facilities?limit=1").get_json()
    assert data[0]["facility_id"] == 100 and data[0]["year"] == 2022


# --- retrieval ----------------------------------------------------------------


def test_retrieval_stages_api_data(client, monkeypatch):
    emissions = [
        {
            "stateCode": "KY", "facilityName": "E W Brown", "facilityId": 1355,
            "unitId": "3", "unit_id": 878, "year": 2023, "sumOpTime": 4652.29,
            "grossLoad": 1035777.21, "steamLoad": None, "so2Mass": 361.0,
            "co2Mass": 1111633.894, "noxMass": 233.7, "heatInput": 10834663.0,
            "primaryFuelInfo": "Coal", "secondaryFuelInfo": None,
            "unitType": "Tangentially-fired", "so2ControlInfo": "Wet Limestone",
            "noxControlInfo": None, "pmControlInfo": "Baghouse",
            "hgControlInfo": "Halogenated PAC Sorbent Injection",
            "programCodeInfo": "ARP, CSNOX",
        }
    ]
    attributes = [
        {
            "facilityId": 1355, "unitId": "3", "year": 2023, "county": "Mercer County",
            "latitude": 37.7889, "longitude": -84.7139, "sourceCategory": "Electric Utility",
            "ownerOperator": "Kentucky Utilities Company (Owner)|Kentucky Utilities Company (Operator)",
            "commercialOperationDate": "1971-07-01", "operatingStatus": "Operating",
        }
    ]
    calls = []

    def fake_get_json(path, params):
        calls.append((path, params))
        return attributes if path == campd.FACILITY_ATTRIBUTES_PATH else emissions

    monkeypatch.setattr(campd, "_get_json", fake_get_json)
    response = client.post(
        "/retrieve", data={"year": "2023", "state": "KY", "fuel_type": "Coal", "attributes": "on"}
    )
    assert response.status_code == 302
    assert calls[0][1] == {"year": "2023", "stateCode": "KY", "unitFuelType": "Coal"}

    staged = db.session.scalars(select(UploadedFile)).one()
    assert staged.source_type == "api"
    client.post(response.headers["Location"] + "/confirm")

    facility = db.session.get(Facility, 1355)
    assert facility.county == "Mercer County"
    assert facility.latitude == pytest.approx(37.7889)
    provenance = db.session.scalars(select(DataProvenance)).one()
    assert provenance.source_type == "api"
    assert "unitFuelType" in provenance.query_params
    unit = db.session.scalars(select(Unit)).one()
    assert str(unit.operating_date) == "1971-07-01"
    assert unit.owner_operator == (
        "Kentucky Utilities Company (Owner)|Kentucky Utilities Company (Operator)"
    )
    record = db.session.scalars(select(AnnualRecord)).one()
    assert record.hg_control == "Halogenated PAC Sorbent Injection"

    # Shown on the detail pages, searchable, and in the CSV download.
    facility_page = client.get("/facility/1355").get_data(as_text=True)
    assert "Kentucky Utilities Company (Owner)<br>Kentucky Utilities Company (Operator)" in facility_page
    unit_page = client.get(f"/unit/{unit.internal_unit_key}").get_data(as_text=True)
    assert "Kentucky Utilities Company (Operator)" in unit_page
    assert "Halogenated PAC Sorbent Injection" in unit_page
    assert "E W Brown" in client.get("/explorer?hg_control=sorbent&owner=kentucky").get_data(as_text=True)
    assert "No records match" in client.get("/explorer?owner=duke").get_data(as_text=True)
    csv = client.get("/download/records?owner=kentucky").get_data(as_text=True)
    header, row = csv.splitlines()[:2]
    assert "owner_operator" in header.split(",") and "hg_controls" in header.split(",")
    assert "Halogenated PAC Sorbent Injection" in row


def test_retrieval_reports_api_errors(client, monkeypatch):
    def failing(path, params):
        raise campd.CampdError("The CAMPD API returned HTTP 429: rate limit")

    monkeypatch.setattr(campd, "_get_json", failing)
    response = client.post("/retrieve", data={"year": "2023"})
    assert response.status_code == 502
    assert "rate limit" in response.get_data(as_text=True)


def test_retrieval_validates_year(client):
    assert client.post("/retrieve", data={"year": "1800"}).status_code == 400


# --- delete -------------------------------------------------------------------


def test_delete_dataset_removes_records_and_orphans(client, sample_dataset):
    client.post(f"/dataset/{sample_dataset.id}/delete")
    assert db.session.scalar(select(func.count(AnnualRecord.id))) == 0
    assert db.session.scalar(select(func.count(DataQualityIssue.id))) == 0
    assert db.session.scalar(select(func.count(Unit.internal_unit_key))) == 0
    assert db.session.scalar(select(func.count(Facility.epa_facility_id))) == 0
