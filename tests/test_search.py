"""The data-explorer query builder."""

import io

import pytest
from werkzeug.datastructures import MultiDict

from app.services.ingest import ingest
from app.services.search import parse_search, run_search
from app.services.validation import read_table, validate

from tests.conftest import HEADER

ROWS = [
    # facility, id, unit, year, state, county, gross, heat, co2, so2, nox, optime, fuel, type, so2 ctl
    "Big Coal,1,1,2023,KY,Mercer County,900000,9000000,800000,400,300,8000,Coal,Tangentially-fired,Wet Limestone",
    "Big Coal,1,2,2023,KY,Mercer County,500000,5000000,450000,900,200,6000,Coal,Tangentially-fired,",
    "Big Coal,1,1,2022,KY,Mercer County,950000,9500000,850000,450,320,8100,Coal,Tangentially-fired,Wet Limestone",
    "Gas Plant,2,CT1,2023,KY,Jefferson County,300000,2500000,150000,1,20,3000,Pipeline Natural Gas,Combined cycle,",
    "WV Coal,3,1,2023,WV,Grant County,700000,7000000,700000,100,250,7500,Coal,Dry bottom wall-fired boiler,Wet Lime FGD",
    "WV Gas,4,1,2023,WV,Grant County,200000,1800000,90000,0.5,10,4000,Natural Gas,Combustion turbine,",
]


@pytest.fixture
def data(app):
    csv = ("\n".join([HEADER, *ROWS]) + "\n").encode()
    ingest(
        validate(read_table(io.BytesIO(csv), "csv")),
        dataset_name="search fixture",
        data_source="CSV upload",
        source_type="upload",
    )


def _search(**params):
    search = parse_search(MultiDict(params))
    return search, run_search(search)


def test_spec_example_multi_criteria(data):
    # Coal units in KY in 2023 with CO2 > 500,000 short tons.
    _, page = _search(state="KY", year="2023", primary_fuel="coal", co2_min="500,000")
    assert [(r.facility_id, r.epa_unit_id) for r in page.rows] == [(1, "1")]


def test_contains_filter_is_case_insensitive(data):
    _, page = _search(primary_fuel="NATURAL GAS", year="2023")
    assert {r.facility_id for r in page.rows} == {2, 4}


def test_range_filters_are_inclusive(data):
    _, page = _search(gross_load_min="300000", gross_load_max="700000", year="2023")
    assert {r.gross_load for r in page.rows} == {300000, 500000, 700000}


def test_control_none_matches_blank(data):
    _, page = _search(so2_control="none", primary_fuel="coal")
    assert [(r.facility_id, r.epa_unit_id) for r in page.rows] == [(1, "2")]


def test_control_not_excludes_a_value(data):
    search, page = _search(so2_control="not limestone")
    assert sorted((r.facility_id, r.epa_unit_id) for r in page.rows) == [
        (1, "2"), (2, "CT1"), (3, "1"), (4, "1"),
    ]
    assert "so2_control NOT CONTAINS 'limestone'" in search.describe()


def test_top_and_bottom_n(data):
    _, top = _search(year="2023", sort="co2_mass", order="desc", limit="2")
    assert [r.co2_mass for r in top.rows] == [800000, 700000]
    _, bottom = _search(year="2023", sort="so2_mass", order="asc", limit="1")
    assert bottom.rows[0].so2_mass == 0.5
    assert top.total == 2


def test_facility_level_sums_units_and_filters_totals(data):
    _, page = _search(level="facility", year="2023", sort="co2_mass", co2_min="1000000")
    assert len(page.rows) == 1
    row = page.rows[0]
    assert (row.facility_id, row.unit_count, row.co2_mass) == (1, 2, 1250000)


def test_group_and_rank_top_facility_per_state(data):
    _, page = _search(level="facility", year="2023", group_by="state", per_group="1", sort="co2_mass")
    assert [(r.state_code, r.facility_id, r.group_rank) for r in page.rows] == [
        ("KY", 1, 1),
        ("WV", 3, 1),
    ]


def test_historical_search_for_one_unit(data):
    _, page = _search(facility_id="1", unit_id="1", year_min="2015", year_max="2025",
                      sort="reporting_year", order="asc")
    assert [r.reporting_year for r in page.rows] == [2022, 2023]


def test_invalid_input_is_reported_not_raised(data):
    search, page = _search(year="20x3", co2_min="lots", sort="password", state="Kentucky")
    assert len(search.errors) == 4
    assert page.total == 6  # every bad filter was dropped


def test_pagination(data):
    _, page = _search(per_page="2", page="2", sort="co2_mass")
    assert (page.page, page.total_pages, len(page.rows)) == (2, 3, 2)


def test_describe_reads_like_the_spec(data):
    search, _ = _search(state="KY", year="2025", primary_fuel="coal", co2_min="500000")
    text = search.describe()
    assert "state_code = 'KY'" in text
    assert "reporting_year = 2025" in text
    assert "primary_fuel CONTAINS 'coal'" in text
    assert "co2_mass >= 500,000" in text
