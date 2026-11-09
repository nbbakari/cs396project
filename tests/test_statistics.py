"""Week 2 statistical engine: descriptive statistics, grouped statistics,
comparisons, rankings and the Kruskal-Wallis test, checked by hand on a
small dataset. The pages, downloads and JSON endpoints too."""

import io
import math
import statistics as py_stats

import numpy as np
import openpyxl
import pytest
from scipy import stats as scipy_stats
from werkzeug.datastructures import MultiDict

from app.services.analysis import parse_analysis
from app.services.statistics import (
    all_variables,
    compare_groups,
    describe,
    describe_grouped,
    describe_values,
    kruskal_test,
    level_frame,
    load_selection,
    shown_groups,
)
from tests.test_analysis import ROWS, _import
from tests.test_impacts import factors  # noqa: F401 - TRACI 2.2 factors fixture


@pytest.fixture
def analysis_data(app):
    return _import(ROWS)


def selection(**params):
    analysis = parse_analysis(MultiDict(params))
    assert analysis.errors == []
    units, cats = load_selection(analysis)
    return analysis, units, cats


def by_group(result):
    return {g["group"]: g for g in result["groups"]}


# --- describe_values -----------------------------------------------------------


def test_statistics_of_known_values():
    values = [1, 2, 3, 4, 100]
    s = describe_values(values)
    assert (s["count"], s["missing"]) == (5, 0)
    assert s["mean"] == 22 and s["median"] == 3
    assert (s["min"], s["max"]) == (1, 100)
    assert s["std"] == pytest.approx(py_stats.stdev(values))  # sample, n - 1
    assert (s["q1"], s["q3"], s["iqr"]) == (2, 4, 2)
    for key, q in (("p5", 5), ("p10", 10), ("p90", 90), ("p95", 95)):
        assert s[key] == pytest.approx(np.percentile(values, q, method="linear"))
    assert s["outliers"] == 1  # 100 is above Q3 + 1.5 IQR = 7
    assert s["skewness"] == pytest.approx(scipy_stats.skew(values, bias=False))
    assert s["skewness"] > 0  # a long tail of high values
    t = scipy_stats.t.ppf(0.975, 4)
    assert s["ci95_low"] == pytest.approx(22 - t * s["std"] / math.sqrt(5))
    assert s["ci95_high"] == pytest.approx(22 + t * s["std"] / math.sqrt(5))


def test_missing_values_are_excluded_and_counted_never_zero():
    s = describe_values([1.0, None, float("nan"), 3.0])
    assert (s["count"], s["missing"], s["mean"], s["min"]) == (2, 2, 2.0, 1.0)


def test_too_few_values_leave_spread_missing():
    one = describe_values([5.0])
    assert (one["count"], one["mean"], one["median"], one["iqr"]) == (1, 5.0, 5.0, 0.0)
    assert one["std"] is None and one["ci95_low"] is None and one["skewness"] is None
    none = describe_values([None, None])
    assert (none["count"], none["missing"], none["outliers"]) == (0, 2, 0)
    assert none["mean"] is None and none["q1"] is None
    assert describe_values([2, 2, 2])["skewness"] is None  # no spread, no skew


# --- statistics of a selection --------------------------------------------------


def test_describe_uses_reported_values_only(analysis_data):
    analysis, units, cats = selection(year="2022")
    rows = {r["name"]: r["statistics"] for r in describe(units, all_variables("unit", cats))}
    co2 = rows["co2_mass"]
    # Plant A unit 2 left CO2 blank: three values, not four with a 0.
    assert (co2["count"], co2["missing"], co2["min"], co2["max"]) == (3, 1, 0, 2700)
    assert co2["mean"] == pytest.approx((1500 + 0 + 2700) / 3)
    intensity = rows["co2_intensity"]
    # Unit 3 generated nothing (zero denominator) and unit 2 has no CO2.
    assert (intensity["count"], intensity["missing"]) == (2, 2)
    assert intensity["median"] == pytest.approx((0.75 + 0.9) / 2)


def test_statistics_grouped_by_state(analysis_data):
    analysis, units, cats = selection()
    variables = all_variables("unit", cats)
    groups = describe_grouped(units, {"co2_mass": variables["co2_mass"]}, "state")
    assert [g["group"] for g in groups] == ["KY", "WV"]
    ky, wv = (g["statistics"]["co2_mass"] for g in groups)
    assert (groups[0]["records"], ky["count"], ky["missing"]) == (5, 4, 1)
    assert wv["mean"] == 2850 and wv["std"] == pytest.approx(py_stats.stdev([3000, 2700]))


def test_facility_level_adds_up_units(analysis_data):
    analysis, units, cats = selection(year="2022", level="facility")
    frame = level_frame(units, "facility", cats)
    rows = {r["name"]: r["statistics"] for r in describe(frame, all_variables("facility", cats))}
    assert rows["co2_mass"]["count"] == 2  # Plant A and Plant B in 2022
    assert sorted(frame["co2_mass"]) == [1500, 2700]


# --- comparisons -----------------------------------------------------------------


def test_compare_states_on_a_total(analysis_data):
    analysis, units, cats = selection(year="2022")
    result = compare_groups(units, all_variables("unit", cats)["co2_mass"], "state")
    ky, wv = by_group(result)["KY"], by_group(result)["WV"]
    assert result["aggregation"] == "sum"
    assert (ky["value"], wv["value"]) == (1500, 2700)  # the blank CO2 is not a 0
    reference = result["reference"]
    assert reference["kind"] == "average" and reference["value"] == 2100
    assert (ky["difference"], wv["difference"]) == (-600, 600)
    assert ky["pct_difference"] == pytest.approx(-600 / 2100 * 100)
    assert ky["share_pct"] == pytest.approx(1500 / 4200 * 100)
    assert (wv["rank"], ky["rank"]) == (1, 2)
    assert (ky["percentile"], wv["percentile"]) == (50, 100)
    assert wv["z_score"] == pytest.approx(1 / math.sqrt(2))
    assert (ky["records"], ky["units"], ky["facilities"], ky["values_present"]) == (3, 3, 1, 2)
    assert [g["group"] for g in result["groups"]] == ["WV", "KY"]


def test_rates_are_ratios_of_totals_not_averages(analysis_data):
    analysis, units, cats = selection(year="2022")
    result = compare_groups(units, all_variables("unit", cats)["co2_intensity"], "state")
    ky = by_group(result)["KY"]
    assert result["aggregation"] == "ratio"
    # KY: (1500 + 0) t / (2000 + 0) MWh; unit 2 left CO2 blank, so its load is not used.
    assert ky["value"] == pytest.approx(0.75)
    assert result["reference"] == {"kind": "overall", "group": None,
                                   "label": "all selected records combined",
                                   "value": pytest.approx(4200 / 5000)}
    assert ky["pct_difference"] == pytest.approx((0.75 - 0.84) / 0.84 * 100)
    assert ky["share_pct"] is None  # shares only for totals
    assert "total CO₂ mass ÷ total gross load" in result["aggregation_text"]


def test_compare_with_a_chosen_group(analysis_data):
    analysis, units, cats = selection(year="2022")
    var = all_variables("unit", cats)["co2_mass"]
    result = compare_groups(units, var, "state", baseline="wv")
    assert result["reference"]["group"] == "WV" and result["errors"] == []
    ky = by_group(result)["KY"]
    assert ky["difference"] == -1200 and ky["pct_difference"] == pytest.approx(-1200 / 2700 * 100)
    assert by_group(result)["WV"]["is_reference"] and not ky["is_reference"]
    unknown = compare_groups(units, var, "state", baseline="TX")
    assert unknown["reference"]["kind"] == "average"
    assert "TX" in unknown["errors"][0]


def test_compare_fuels_and_order(analysis_data):
    analysis, units, cats = selection(year="2022")
    result = compare_groups(units, all_variables("unit", cats)["gross_load"], "primary_fuel")
    assert [g["group"] for g in result["groups"]] == ["Coal", "Pipeline Natural Gas"]
    assert [g["group"] for g in shown_groups(result["groups"], "asc", 1)] == ["Pipeline Natural Gas"]


def test_operating_time_is_averaged_per_unit_year(analysis_data):
    analysis, units, cats = selection(year="2022")
    result = compare_groups(units, all_variables("unit", cats)["operating_time"], "state")
    assert result["aggregation"] == "mean"
    assert by_group(result)["KY"]["value"] == pytest.approx((8760 + 1000 + 0) / 3)


def test_kruskal_wallis_matches_scipy(analysis_data):
    analysis, units, cats = selection()
    result = compare_groups(units, all_variables("unit", cats)["co2_mass"], "year")
    h, p = scipy_stats.kruskal([1000, 3000], [1000], [1500, 0, 2700])
    test = result["test"]
    assert test["available"] and test["df"] == 2 and test["observations"] == 6
    assert test["h"] == pytest.approx(h) and test["p_value"] == pytest.approx(p)
    assert test["epsilon_squared"] == pytest.approx(h / 5)
    assert test["significant"] == (p < 0.05)


def test_kruskal_wallis_needs_enough_values():
    assert kruskal_test([np.array([1.0])], "states")["available"] is False
    assert kruskal_test([np.array([1.0]), np.array([2.0])], "states")["available"] is False
    same = kruskal_test([np.array([1.0, 1.0]), np.array([1.0, 1.0])], "states")
    assert same["available"] is False and "same" in same["interpretation"]


# --- pages, downloads and endpoints -------------------------------------------------


def test_statistics_page(client, factors, analysis_data):  # noqa: F811
    html = client.get("/analysis/statistics?year=2022&group_by=state&variable=co2_intensity").get_data(as_text=True)
    assert "Descriptive statistics" in html and 'id="stats-table"' in html
    assert "CO2 intensity (CO₂ per MWh of electricity, short tons/MWh)" in html
    assert "TRACI potential impacts" in html
    assert 'id="grouped-table"' in html and "CO2 intensity by state" in html
    bad = client.get("/analysis/statistics?level=facility&group_by=primary_fuel").get_data(as_text=True)
    assert "cannot be grouped by" in bad


def test_compare_page(client, analysis_data):
    html = client.get("/analysis/compare?year=2022&by=state&metric=co2_mass&baseline=WV").get_data(as_text=True)
    assert 'id="compare-table"' in html and "Kruskal–Wallis H test" in html
    assert "reference</span>" in html and "small sample" in html
    assert client.get("/analysis/compare?by=nonsense").status_code == 200


def test_statistics_download(client, analysis_data):
    csv = client.get("/analysis/statistics/download?year=2022&format=csv").get_data(as_text=True)
    header = csv.lstrip("﻿").splitlines()[0].split(",")
    assert header[:4] == ["variable", "label", "unit", "kind"] and "iqr" in header
    co2 = next(line for line in csv.splitlines() if line.startswith("co2_mass,"))
    assert co2.split(",")[4:6] == ["3", "1"]  # count, missing
    response = client.get("/analysis/statistics/download?group_by=state&format=xlsx")
    workbook = openpyxl.load_workbook(io.BytesIO(response.data))
    assert workbook.sheetnames == ["statistics", "about"]
    about = {row[0]: row[1] for row in workbook["about"].iter_rows(values_only=True) if row and row[0]}
    assert about["standard_deviation"] == "sample standard deviation (n - 1)"
    assert about["grouped_by"] == "state"


def test_compare_download_has_every_group_and_the_test(client, analysis_data):
    csv = client.get("/analysis/compare/download?by=state&metric=co2_mass&format=csv").get_data(as_text=True)
    lines = csv.lstrip("﻿").splitlines()
    assert lines[0].startswith("rank,group,label,value,difference,pct_difference")
    assert len(lines) == 3
    workbook = openpyxl.load_workbook(io.BytesIO(
        client.get("/analysis/compare/download?by=year&metric=co2_mass&format=xlsx").data))
    about = {row[0]: row[1] for row in workbook["about"].iter_rows(values_only=True) if row and row[0]}
    assert about["test"] == "Kruskal–Wallis H test" and "p_value" in about


def test_statistics_endpoint(client, analysis_data):
    body = client.get("/api/analysis/statistics?year=2022&variables=co2_mass,heat_rate").get_json()
    assert [v["name"] for v in body["variables"]] == ["co2_mass", "heat_rate"]
    assert body["variables"][0]["statistics"]["count"] == 3
    grouped = client.get("/api/analysis/statistics?group_by=year&variables=co2_mass").get_json()
    assert [g["group"] for g in grouped["groups"]] == [2020, 2021, 2022]
    assert client.get("/api/analysis/statistics?variables=nope").status_code == 400
    assert client.get("/api/analysis/statistics?state=Kentucky").status_code == 400


def test_compare_endpoint(client, analysis_data):
    body = client.get("/api/analysis/compare?year=2022&by=state&metric=co2_intensity").get_json()
    assert body["aggregation"] == "ratio" and body["groups"][0]["group"] == "WV"
    assert body["reference"]["value"] == pytest.approx(0.84)
    assert body["test"]["name"] == "Kruskal–Wallis H test"
    assert client.get("/api/analysis/compare?metric=nope").status_code == 400
    assert client.get("/api/analysis/compare?by=planet").status_code == 400
