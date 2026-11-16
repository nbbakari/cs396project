"""Clement's Week 3 pages (Data Sources, Coverage), the sidebar, and the
AI assistant's access to every page."""

import json
import re

import pytest

from app import db
from app.services import assistant
from app.services.exports import _cell
from app.services.traci import read_workbook, save_factors
from tests.test_traci import make_workbook


def test_pages_with_no_data(client):
    sources = client.get("/data-sources")
    assert sources.status_code == 200
    assert "No datasets loaded yet." in sources.get_data(as_text=True)
    coverage = client.get("/coverage").get_data(as_text=True)
    assert "No data is loaded yet" in coverage and "chart.js" not in coverage


def test_data_sources_lists_datasets_and_explains_terms(client, sample_dataset, tmp_path):
    save_factors(read_workbook(make_workbook(tmp_path / "t.xlsx")))
    db.session.commit()
    html = client.get("/data-sources").get_data(as_text=True)
    assert "sample" in html and "File upload: 1" in html
    assert "gross load (electricity generated)" in html
    assert "SO₂ (sulfur dioxide)" in html and "NOx (nitrogen oxides)" in html
    assert "TRACI 2.2, 6 factors" in html
    assert "acidification (acid rain)" in html


def test_coverage_counts_and_chart_data(client, sample_dataset):
    html = client.get("/coverage").get_data(as_text=True)
    cards = re.findall(r'class="stat-value">([\d,]+)<', html)
    # The sample import keeps three rows: Plant A units 1 and 2, and Plant E.
    assert cards[:3] == ["2", "3", "3"]
    data = json.loads(re.search(r'id="coverage-data" type="application/json">(.*?)</script>', html).group(1))
    assert sum(r["count"] for r in data["by_year"]) == int(cards[2].replace(",", ""))
    assert data["by_state"][0]["count"] >= 1
    # Axes carry titles with units.
    assert 'yTitle: "Annual records (unit-years)"' in html
    assert 'xTitle: "Facilities (power plants)"' in html


def test_sidebar_links_to_every_page(client):
    html = client.get("/").get_data(as_text=True)
    for path, label in (("/data-sources", "Data Sources"), ("/coverage", "Coverage Dashboard"),
                        ("/analysis/statistics", "Statistics &amp; Comparison"),
                        ("/analysis/impacts", "TRACI Impact Indicators")):
        assert f'href="{path}"' in html and label in html
    assert "Statistics &amp; Comparison <span class=\"planned-badge\">" not in html


def test_compare_page_highlights_its_sidebar_section(client):
    html = client.get("/analysis/compare").get_data(as_text=True)
    assert re.search(r'class="nav-link active" href="/analysis/statistics"', html)


@pytest.mark.parametrize("path,label", [
    ("/data-sources", "Data Sources"),
    ("/coverage", "Coverage Dashboard"),
    ("/analysis/statistics", "Descriptive statistics"),
    ("/analysis/compare", "Compare & rank"),
    ("/analysis/impacts", "TRACI impact assessment"),
])
def test_assistant_can_open_the_new_pages(app, path, label):
    with app.test_request_context():
        result = assistant.navigate(path, "year=2022")
        assert result["status"] == "success" and result["opens"] == label


def test_assistant_links_the_new_downloads(app):
    with app.test_request_context():
        result = assistant.navigate("/analysis/impacts/download", "format=xlsx")
        assert result["status"] == "success" and "download link" in result["note"]
        bad = assistant.navigate("/analysis/compare", "state=Kentucky")
        assert bad["status"] == "error"


def test_assistant_week2_tools(app, tmp_path):
    from tests.test_analysis import ROWS, _import
    save_factors(read_workbook(make_workbook(tmp_path / "t.xlsx")))
    db.session.commit()
    _import(ROWS)
    with app.test_request_context():
        ranked = assistant.compare_and_rank("year=2022", "state", "co2_mass")
        assert ranked["status"] == "success"
        assert [g["label"] for g in ranked["groups"]] == ["WV", "KY"]
        assert ranked["reference"]["value"] == 2100
        impacts = assistant.assess_traci_impacts("year=2022", "state")
        assert impacts["traci_version"] == "2.2"
        acid = next(c for c in impacts["categories"] if c["category"] == "Acidification (acid rain)")
        assert acid["unit"] == "kg SO2 eq" and acid["unit_years_left_out"] == 1
        assert impacts["groups"][0]["group"] == "WV"
        assert "not measured harm" in impacts["note"]
        stats = assistant.summarize_records("year=2022")["statistics"]["co2_mass"]
        assert (stats["count"], stats["missing"], stats["iqr"]) == (3, 1, 1350)
        assert assistant.compare_and_rank(metric="nope")["status"] == "error"
        assert assistant.assess_traci_impacts(group_by="planet")["status"] == "error"


def test_downloads_never_round_small_rates_to_zero():
    assert _cell(3.4e-07) == 3.4e-07
    assert _cell(907.18474) == 907.18474
    assert _cell(float("nan")) is None
