"""Week 2 TRACI assessment engine: short tons to kg, exact factor matching,
impact_c = sum(mass_kg x CF) per category, impact per MWh, missing values,
group totals, the worked example, versions, the page, downloads and API.

Every expected number is worked out by hand from the TRACI 2.2 factors."""

import io

import openpyxl
import pytest
from werkzeug.datastructures import MultiDict

from app import db
from app.models import TraciFactor
from app.services.analysis import parse_analysis
from app.services.impacts import (
    SHORT_TON_KG,
    categories,
    grouped_impacts,
    impact_summary,
    to_kg,
    worked_example,
)
from app.services.statistics import load_selection
from app.services.traci import read_workbook, save_factors
from tests.test_analysis import ROWS, _import
from tests.test_traci import make_workbook

KG = 907.18474
PM_SO2, PM_NOX = 0.06111111111111111, 0.007222222222222222
SMOG_NOX = 24.793589743589738


@pytest.fixture
def factors(app, tmp_path):
    save_factors(read_workbook(make_workbook(tmp_path / "traci_2_2.xlsx")))
    db.session.commit()


@pytest.fixture
def data(app, factors):
    return _import(ROWS)


def selection(**params):
    analysis = parse_analysis(MultiDict(params))
    assert analysis.errors == []
    return load_selection(analysis)


def unit_year(units, facility, unit, year):
    match = units[(units.facility_id == facility) & (units.epa_unit_id == unit)
                  & (units.reporting_year == year)]
    assert len(match) == 1
    return match.iloc[0]


def test_short_ton_is_907_18474_kg():
    assert SHORT_TON_KG == 907.18474
    assert list(to_kg([1, 2.5, None]).round(6).fillna(-1)) == [907.18474, 2267.96185, -1]


def test_categories_come_from_the_stored_factors(factors):
    cats = {c.category: c for c in categories()}
    assert set(cats) == {"Acidification", "Global Climate", "HH Particulate",
                         "Photochemical Oxidation Formation"}
    acid = cats["Acidification"]
    assert acid.name == "traci_acidification" and acid.version == "2.2"
    assert acid.label == "Acidification (acid rain)" and acid.unit == "kg SO2 eq"
    assert [(f.campd_metric, f.value) for f in acid.factors] == [("so2_mass", 1.0), ("nox_mass", 0.7)]
    assert acid.formula == "SO₂ kg × 1 + NOx kg × 0.7"
    assert cats["HH Particulate"].unit == "kg PM2.5 eq"  # EPA writes "PM2.5 eq / kg substance"
    smog = cats["Photochemical Oxidation Formation"]
    # NOx uses NITROGEN OXIDES' factor, never NITROGEN DIOXIDE's 16.85.
    assert [f.value for f in smog.factors] == [pytest.approx(SMOG_NOX)]


def test_impact_of_one_unit_year_by_hand(data):
    units, cats = selection()
    row = unit_year(units, 100, "1", 2022)  # CO2 1500 t, SO2 10 t, NOx 6 t, 2000 MWh
    co2, so2, nox = 1500 * KG, 10 * KG, 6 * KG
    assert row["traci_global_climate"] == pytest.approx(co2)
    assert row["traci_acidification"] == pytest.approx(so2 * 1 + nox * 0.7)
    assert row["traci_hh_particulate"] == pytest.approx(so2 * PM_SO2 + nox * PM_NOX)
    assert row["traci_photochemical_oxidation_formation"] == pytest.approx(nox * SMOG_NOX)
    assert row["traci_acidification_per_mwh"] == pytest.approx((so2 + nox * 0.7) / 2000)


def test_unreported_pollutant_leaves_the_category_missing(data):
    units, cats = selection()
    row = unit_year(units, 100, "2", 2022)  # CO2 and SO2 blank, NOx 2 t, 500 MWh
    assert row.isna()["traci_global_climate"] and row.isna()["traci_acidification"]
    assert row.isna()["traci_hh_particulate"]  # not NOx alone, an underestimate
    assert row["traci_photochemical_oxidation_formation"] == pytest.approx(2 * KG * SMOG_NOX)
    assert row["traci_photochemical_oxidation_formation_per_mwh"] == pytest.approx(2 * KG * SMOG_NOX / 500)


def test_reported_zero_is_zero_but_zero_load_has_no_intensity(data):
    units, cats = selection()
    row = unit_year(units, 100, "3", 2022)  # reported 0 for everything
    assert row["traci_acidification"] == 0
    assert row.isna()["traci_acidification_per_mwh"]


def test_totals_shares_and_left_out_records(data):
    units, cats = selection(year="2022")
    summary = {s["category"]: s for s in impact_summary(units, cats)}
    acid = summary["Acidification"]
    expected = (10 * KG + 6 * KG * 0.7) + 0 + (27 * KG + 9 * KG * 0.7)
    assert acid["total"] == pytest.approx(expected)
    assert (acid["records_used"], acid["records_missing"]) == (3, 1)
    assert acid["intensity"] == pytest.approx(expected / (2000 + 0 + 3000))
    shares = {p["campd_metric"]: p for p in acid["pollutants"]}
    assert shares["so2_mass"]["impact"] == pytest.approx(37 * KG)
    assert shares["so2_mass"]["mass_short_tons"] == 37 and shares["nox_mass"]["mass_short_tons"] == 15
    assert shares["so2_mass"]["share_pct"] + shares["nox_mass"]["share_pct"] == pytest.approx(100)
    smog = summary["Photochemical Oxidation Formation"]
    assert smog["total"] == pytest.approx((6 + 2 + 0 + 9) * KG * SMOG_NOX)
    assert smog["records_missing"] == 0
    assert summary["Global Climate"]["unit"] == "kg CO2 eq"


def test_group_totals_add_up_and_are_ranked(data):
    units, cats = selection(year="2022")
    groups = {g["group"]: g for g in grouped_impacts(units, cats, "state")}
    ky, wv = groups["KY"]["impacts"], groups["WV"]["impacts"]
    assert ky["traci_global_climate"]["total"] == pytest.approx(1500 * KG)
    assert wv["traci_global_climate"]["total"] == pytest.approx(2700 * KG)
    assert (wv["traci_global_climate"]["rank"], ky["traci_global_climate"]["rank"]) == (1, 2)
    assert ky["traci_acidification"]["records_missing"] == 1
    assert ky["traci_acidification"]["intensity"] == pytest.approx((10 * KG + 6 * KG * 0.7) / 2000)
    overall = impact_summary(units, cats)[1]["total"]
    assert sum(g["impacts"]["traci_global_climate"]["total"] for g in groups.values()) == pytest.approx(overall)
    facilities = {g["group"]: g for g in grouped_impacts(units, cats, "facility")}
    assert facilities[100]["label"] == "Plant A (KY)"


def test_worked_example_is_the_largest_complete_record(data):
    units, cats = selection(year="2022")
    example = worked_example(units, cats)
    assert (example["facility_name"], example["epa_unit_id"]) == ("Plant B", "1")
    assert example["masses"]["so2_mass"] == {"short_tons": 27, "kg": pytest.approx(27 * KG)}
    acid = next(s for s in example["steps"] if s["category"] == "Acidification")
    assert [t["result"] for t in acid["terms"]] == [pytest.approx(27 * KG), pytest.approx(9 * KG * 0.7)]
    assert acid["intensity"] == pytest.approx(acid["total"] / 3000)


def test_a_chosen_traci_version_is_used(data):
    db.session.add(TraciFactor(version="2.1", pollutant_name="CARBON DIOXIDE", cas_number="124389",
                               campd_metric="co2_mass", medium="air", impact_category="Global Climate",
                               factor_value=2.0, factor_unit="kg CO2 eq / kg substance", source="test"))
    db.session.commit()
    units, cats = load_selection(parse_analysis(MultiDict({"year": "2022"})), version="2.1")
    assert [c.category for c in cats] == ["Global Climate"] and cats[0].version == "2.1"
    assert impact_summary(units, cats)[0]["total"] == pytest.approx((1500 + 0 + 2700) * KG * 2)
    latest = load_selection(parse_analysis(MultiDict({"year": "2022"})))[1]
    assert latest[0].version == "2.2"


def test_impacts_page_shows_the_calculation(client, data):
    html = client.get("/analysis/impacts?year=2022").get_data(as_text=True)
    assert "TRACI impact assessment" in html and "EPA TRACI 2.2" in html
    for label in ("Acidification (acid rain)", "Global warming potential (climate change)",
                  "Photochemical ozone formation (smog)"):
        assert label in html
    assert "mass_short_tons × 907.18474" in html
    assert "SO₂ kg × 1 + NOx kg × 0.7" in html
    assert 'id="worked-example"' in html and "Plant B" in html
    assert "1 left out because a pollutant was not reported" in html
    assert "not measured harm" in html


def test_impacts_page_without_factors(client, app):
    _import(ROWS)
    html = client.get("/analysis/impacts").get_data(as_text=True)
    assert "No TRACI factors are stored" in html


def test_impacts_download(client, data):
    csv = client.get("/analysis/impacts/download?year=2022&format=csv").get_data(as_text=True)
    lines = csv.lstrip("﻿").splitlines()
    header = lines[0].split(",")
    assert {"co2_mass_kg", "traci_acidification", "traci_acidification_per_mwh", "traci_version"} <= set(header)
    assert len(lines) == 5 and all(line.endswith(",2.2") for line in lines[1:])
    response = client.get("/analysis/impacts/download?year=2022&format=xlsx&group_by=primary_fuel")
    workbook = openpyxl.load_workbook(io.BytesIO(response.data))
    assert workbook.sheetnames == ["impacts by unit-year", "summary", "by primary_fuel", "factors", "about"]
    factors = list(workbook["factors"].iter_rows(values_only=True))
    assert len(factors) == 7 and factors[1][0] == "2.2"
    about = {row[0]: row[1] for row in workbook["about"].iter_rows(values_only=True) if row and row[0]}
    assert about["traci_version"] == "2.2" and about["short_ton_kg"] == 907.18474


def test_impacts_endpoint(client, data):
    body = client.get("/api/traci/impacts?year=2022&group_by=state").get_json()
    assert body["traci_version"] == "2.2" and body["short_ton_kg"] == 907.18474
    acid = next(c for c in body["categories"] if c["category"] == "Acidification")
    assert acid["total"] == pytest.approx((37 * KG) + (15 * KG * 0.7))
    assert [g["group"] for g in body["groups"]] == ["WV", "KY"]
    assert body["worked_example"]["facility_name"] == "Plant B"
    assert client.get("/api/traci/impacts?version=9.9").status_code == 404
    assert client.get("/api/traci/impacts?group_by=planet").status_code == 400


def test_impacts_endpoint_without_factors(client, app):
    assert client.get("/api/traci/impacts").status_code == 404


def test_impacts_in_statistics_and_comparisons(client, data):
    body = client.get("/api/analysis/statistics?year=2022&variables=traci_acidification").get_json()
    stats = body["variables"][0]["statistics"]
    assert (stats["count"], stats["missing"]) == (3, 1)
    compare = client.get("/api/analysis/compare?year=2022&by=state&metric=traci_acidification_per_mwh").get_json()
    assert compare["aggregation"] == "ratio"
    assert compare["groups"][0]["value"] == pytest.approx((27 * KG + 9 * KG * 0.7) / 3000)
    facility = client.get("/api/analysis/statistics?year=2022&level=facility&variables=traci_global_climate").get_json()
    assert facility["variables"][0]["statistics"]["max"] == pytest.approx(2700 * KG)
