"""Analytical data layer: missing values, derived indicators, year-over-year
changes, facility totals and the JSON endpoints."""

import io
import json
import math

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select
from werkzeug.datastructures import MultiDict

from app import db
from app.models import AnnualRecord, Unit
from app.services.analysis import (
    analysis_frame,
    hours_in_year,
    missing_summary,
    parse_analysis,
    safe_ratio,
)
from app.services.ingest import ingest
from app.services.validation import read_table, validate
from tests.conftest import sample_csv

ROWS = [
    # Plant A (KY), unit 1: three consecutive years; 2020 is a leap year
    "Plant A,100,1,2020,KY,Mercer County,1000,10000,1000,0,5,8784,Coal,Stoker,",
    "Plant A,100,1,2021,KY,Mercer County,1000,10000,1000,0,5,4380,Coal,Stoker,",
    "Plant A,100,1,2022,KY,Mercer County,2000,18000,1500,10,6,8760,Coal,Stoker,",
    # unit 2: CO2 and SO2 not reported
    "Plant A,100,2,2022,KY,Mercer County,500,4000,,,2,1000,Pipeline Natural Gas,Combined cycle,",
    # unit 3: reported, but generated nothing
    "Plant A,100,3,2022,KY,Mercer County,0,0,0,0,0,0,Pipeline Natural Gas,Combustion turbine,",
    # Plant B (WV): no record for 2021
    "Plant B,200,1,2020,WV,,3000,30000,3000,30,9,8000,Coal,Stoker,",
    "Plant B,200,1,2022,WV,,3000,27000,2700,27,9,8000,Coal,Stoker,",
]


def _import(rows, name="analysis"):
    result = validate(read_table(io.BytesIO(sample_csv(rows)), "csv"))
    return ingest(
        result,
        dataset_name=name,
        data_source="CSV upload",
        source_type="upload",
        original_filename=f"{name}.csv",
    )


@pytest.fixture
def analysis_data(app):
    return _import(ROWS)


def frame_for(**params):
    analysis = parse_analysis(MultiDict(params))
    assert analysis.errors == []
    return analysis_frame(analysis)


def row(frame, facility, unit=None, year=2022):
    match = (frame["facility_id"] == facility) & (frame["reporting_year"] == year)
    if unit is not None:
        match &= frame["epa_unit_id"] == unit
    assert match.sum() == 1
    return frame[match].iloc[0]


def missing(value):
    return value is None or (isinstance(value, float) and math.isnan(value))


# --- missing values on import ------------------------------------------------


def _record(facility, unit, year):
    return db.session.scalar(
        select(AnnualRecord)
        .join(Unit)
        .where(Unit.facility_id == facility, Unit.epa_unit_id == unit,
               AnnualRecord.reporting_year == year)
    )


def test_import_flags_blank_metrics(app, sample_dataset):
    blank = _record(100, "2", 2022)
    assert blank.co2_mass == 0  # Phase 1 still stores a number ...
    assert blank.missing_metrics == (  # ... and now remembers it was blank
        "operating_time,gross_load,heat_input,co2_mass,so2_mass,nox_mass"
    )
    assert _record(100, "1", 2022).missing_metrics is None


def test_file_without_a_metric_column_flags_it_on_every_row(app):
    csv = (
        "Facility Name,Facility ID,Unit ID,Year,State,Gross Load (MWh),Heat Input (mmBtu),"
        "CO2 Mass (short tons),NOx Mass (short tons),Operating Time,Primary Fuel Type,Unit Type\n"
        "Plant X,700,1,2022,KY,100,1000,90,1,500,Coal,Stoker\n"
        "Plant X,700,2,2022,KY,100,1000,,1,500,Coal,Stoker\n"
    ).encode()
    ingest(validate(read_table(io.BytesIO(csv), "csv")), dataset_name="no so2",
           data_source="CSV upload", source_type="upload", original_filename="x.csv")
    assert _record(700, "1", 2022).missing_metrics == "so2_mass"
    assert _record(700, "2", 2022).missing_metrics == "co2_mass,so2_mass"


# --- calculations ------------------------------------------------------------


def test_safe_ratio_leaves_undefined_values_missing():
    result = safe_ratio([10, 10, np.nan, 10, 0], [2, 0, 5, np.nan, 4])
    assert result.tolist()[0] == 5
    assert all(math.isnan(v) for v in result.tolist()[1:4])
    assert result.tolist()[4] == 0  # a real zero over a positive denominator is 0
    assert not np.isinf(result).any()


def test_hours_in_year_counts_leap_years():
    assert hours_in_year(pd.Series([2020, 2022, 1900, 2000])).tolist() == [8784, 8760, 8760, 8784]


def test_unit_indicators(analysis_data):
    frame = frame_for()
    a1 = row(frame, 100, "1")
    assert a1["co2_intensity"] == pytest.approx(0.75)
    assert a1["so2_intensity"] == pytest.approx(0.005)
    assert a1["nox_intensity"] == pytest.approx(0.003)
    assert a1["heat_rate"] == pytest.approx(9.0)
    assert a1["operating_utilization"] == pytest.approx(1.0)
    assert a1["average_load"] == pytest.approx(2000 / 8760)
    assert row(frame, 100, "1", 2020)["operating_utilization"] == pytest.approx(1.0)  # 8,784 h
    assert row(frame, 100, "1", 2021)["operating_utilization"] == pytest.approx(0.5)


def test_unreported_emissions_stay_missing_not_zero(analysis_data):
    a2 = row(frame_for(), 100, "2")
    assert missing(a2["co2_mass"]) and missing(a2["co2_intensity"])
    assert missing(a2["so2_mass"]) and missing(a2["so2_intensity"])
    assert a2["nox_intensity"] == pytest.approx(0.004)  # reported values still work
    assert a2["heat_rate"] == pytest.approx(8.0)


def test_zero_gross_load_makes_intensities_missing(analysis_data):
    a3 = row(frame_for(), 100, "3")
    for name in ("co2_intensity", "so2_intensity", "nox_intensity", "heat_rate", "average_load"):
        assert missing(a3[name]), name
    assert a3["operating_utilization"] == 0  # 0 hours of a known year is a real 0


def test_year_over_year_changes(analysis_data):
    frame = frame_for(yoy="co2_mass,so2_mass,co2_intensity")
    a1 = row(frame, 100, "1")
    assert a1["co2_mass_change"] == 500
    assert a1["co2_mass_pct_change"] == pytest.approx(50.0)
    assert a1["so2_mass_change"] == 10
    assert missing(a1["so2_mass_pct_change"])  # up from 0: no percentage
    assert a1["co2_intensity_change"] == pytest.approx(-0.25)
    assert missing(row(frame, 200, "1")["co2_mass_change"])  # 2021 is missing
    assert missing(row(frame, 100, "1", 2020)["co2_mass_change"])  # no 2019


def test_year_filter_still_compares_with_the_previous_year(analysis_data):
    frame = frame_for(year="2022", yoy="co2_mass")
    assert set(frame["reporting_year"]) == {2022}  # 2021 is used, not returned
    assert row(frame, 100, "1")["co2_mass_change"] == 500
    frame = frame_for(year_min="2021", yoy="co2_mass")
    assert set(frame["reporting_year"]) == {2021, 2022}
    assert row(frame, 100, "1", 2021)["co2_mass_change"] == 0


def test_facility_totals_and_indicators(analysis_data):
    frame = frame_for(level="facility", yoy="co2_mass")
    plant_a = row(frame, 100)
    assert plant_a["unit_count"] == 3
    assert plant_a["units_with_missing_values"] == 1
    assert plant_a["co2_mass"] == 1500
    assert plant_a["gross_load"] == 2500
    # Unit 2 reported no CO2, so its generation is left out of CO2 intensity
    # rather than counted as zero-emission: 1,500 / 2,000, not 1,500 / 2,500.
    assert plant_a["co2_intensity"] == pytest.approx(0.75)
    assert plant_a["nox_intensity"] == pytest.approx(8 / 2500)
    assert plant_a["heat_rate"] == pytest.approx(22000 / 2500)
    assert plant_a["co2_mass_change"] == 500
    assert "operating_utilization" not in frame  # operating time is per unit


def test_facility_total_is_missing_only_when_no_unit_reported(app):
    _import([
        "Plant C,300,1,2022,KY,,100,1000,,,1,10,Coal,Stoker,",
        "Plant C,300,2,2022,KY,,100,1000,,,1,10,Coal,Stoker,",
    ], name="c")
    plant_c = row(frame_for(level="facility"), 300)
    assert missing(plant_c["co2_mass"]) and missing(plant_c["co2_intensity"])
    assert plant_c["nox_mass"] == 2


def test_missing_summary(analysis_data):
    summary = {r["variable"]: r for r in missing_summary(frame_for())}
    assert summary["co2_mass"]["missing"] == 1
    assert summary["co2_intensity"]["missing"] == 2  # unit 2 (no CO2) and unit 3 (0 MWh)
    assert summary["co2_intensity"]["zero_denominator"] == 1
    assert summary["steam_load"]["missing"] == 7
    assert summary["gross_load"]["missing_pct"] == 0.0


def test_sort_puts_missing_values_last(analysis_data):
    frame = frame_for(year="2022", sort="co2_intensity", order="asc")
    values = frame["co2_intensity"].tolist()
    assert values[:2] == pytest.approx([0.75, 0.9])
    assert all(math.isnan(v) for v in values[2:])


def test_bad_parameters_are_reported():
    analysis = parse_analysis(MultiDict({"level": "plant", "yoy": "facility_name", "sort": "x"}))
    assert len(analysis.errors) == 3
    analysis = parse_analysis(MultiDict({"level": "facility", "yoy": "operating_time"}))
    assert analysis.errors  # not added up for a facility


# --- JSON endpoints ------------------------------------------------------------


def test_summary_endpoint(client, analysis_data):
    body = client.get("/api/analysis/summary?state=KY&year_min=2021").get_json()
    assert body["counts"] == {"records": 4, "facilities": 1, "units": 3, "states": 1,
                              "years": [2021, 2022]}
    names = [v["name"] for v in body["variables"]["numeric"]]
    assert "co2_intensity" in names and "heat_rate" in names
    co2 = next(m for m in body["missing"] if m["variable"] == "co2_mass")
    assert co2["missing"] == 1


def test_indicators_endpoint_returns_null_for_missing(client, analysis_data):
    response = client.get("/api/analysis/indicators?year=2022&yoy=co2_mass&sort=co2_intensity")
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert "NaN" not in text and "Infinity" not in text
    body = json.loads(text)
    assert body["total"] == 4
    assert body["definitions"]["co2_intensity"]["unit"] == "short tons/MWh"
    unit2 = next(r for r in body["records"] if r["facility_id"] == 100 and r["epa_unit_id"] == "2")
    assert unit2["co2_mass"] is None and unit2["co2_intensity"] is None
    assert body["records"][0]["co2_intensity"] == pytest.approx(0.9)  # highest first


def test_indicators_endpoint_paginates_and_validates(client, analysis_data):
    body = client.get("/api/analysis/indicators?level=facility&per_page=1&page=2").get_json()
    # Plant A in 2020, 2021 and 2022; Plant B in 2020 and 2022
    assert (body["total"], body["total_pages"], body["page"]) == (5, 5, 2)
    assert len(body["records"]) == 1
    response = client.get("/api/analysis/indicators?yoy=nonsense")
    assert response.status_code == 400
    assert "nonsense" in response.get_json()["detail"][0]


# --- Analytical dataset builder page -------------------------------------------


def test_builder_page_shows_counts_variables_and_preview(client, analysis_data):
    page = client.get("/analysis?state=KY")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "Analytical dataset builder" in html
    assert 'id="variables-table"' in html and "co2_intensity" in html
    assert "Records</div><div class=\"stat-value\">5<" in html  # unit 1 in 3 years, units 2 and 3
    assert "Units</div><div class=\"stat-value\">3<" in html
    assert 'class="missing-value"' in html  # unit 2's CO2 is shown as missing, not 0
    assert "WHERE state_code = &#39;KY&#39;" in html
    assert "No TRACI factors are stored" in html  # the test database is not migrated


def test_builder_facility_level_with_year_over_year(client, analysis_data):
    html = client.get("/analysis?level=facility&yoy=co2_mass").get_data(as_text=True)
    assert "With gaps" in html and "% change" in html
    assert "+50.0%" in html  # Plant A, 2021 -> 2022


def test_builder_reports_bad_parameters(client, analysis_data):
    html = client.get("/analysis?yoy=bogus").get_data(as_text=True)
    assert "No year-over-year change for &#39;bogus&#39;" in html


def test_builder_is_in_the_sidebar(client):
    assert 'href="/analysis"' in client.get("/").get_data(as_text=True)


def test_dataset_download_csv(client, analysis_data):
    import csv

    response = client.get("/analysis/download?format=csv&year=2022")
    assert response.mimetype == "text/csv"
    rows = list(csv.DictReader(io.StringIO(response.get_data(as_text=True).lstrip("﻿"))))
    assert len(rows) == 4
    unit2 = next(r for r in rows if r["facility_id"] == "100" and r["epa_unit_id"] == "2")
    assert unit2["co2_mass"] == "" and unit2["co2_intensity"] == ""  # missing, not 0
    assert unit2["nox_intensity"] == "0.004"


def test_dataset_download_excel_has_definitions(client, analysis_data):
    import openpyxl

    response = client.get("/analysis/download?format=xlsx&level=facility")
    workbook = openpyxl.load_workbook(io.BytesIO(response.data))
    assert workbook.sheetnames == ["data", "about"]
    about = {row[0]: row[1] for row in workbook["about"].iter_rows(values_only=True) if row and row[0]}
    assert about["level"] == "facility"
    assert "never reported as 0" in about["missing_values"]
    assert about["co2_intensity"] == "CO2 intensity"


def test_configuration_file_reproduces_the_selection(client, analysis_data):
    response = client.get("/analysis/config.json?state=KY&yoy=co2_mass&page=2")
    config = json.loads(response.get_data(as_text=True))
    assert config["kind"] == "analysis-configuration"
    assert config["parameters"] == {"state": ["KY"], "yoy": ["co2_mass"]}
    assert config["url"].startswith("/analysis?") and "page" not in config["url"]
    assert config["selection"]["records"] == 5
    assert config["definitions"]["heat_rate"]["unit"] == "MMBtu/MWh"
