"""Description-based search: plain-English requests become explorer filters."""

import io
from urllib.parse import parse_qs, urlparse

import pytest
from werkzeug.datastructures import MultiDict

from app.services.describe import interpret
from app.services.ingest import ingest
from app.services.search import parse_search, run_search
from app.services.validation import read_table, validate

from tests.conftest import HEADER
from tests.test_search import ROWS


@pytest.fixture
def data(app):
    csv = ("\n".join([HEADER, *ROWS]) + "\n").encode()
    ingest(
        validate(read_table(io.BytesIO(csv), "csv")),
        dataset_name="describe fixture",
        data_source="CSV upload",
        source_type="upload",
    )


def _read(text):
    return interpret(text, latest_year=2023)


def _rows(params):
    return run_search(parse_search(MultiDict(params))).rows


def test_rubric_example_finds_high_co2_coal_units_in_kentucky(data):
    reading = _read("Find coal units in Kentucky with high CO2 emissions.")
    params = reading.params
    assert params["primary_fuel"] == "coal"
    assert params["state"] == "KY"
    assert params["sort"] == "co2_mass" and params["order"] == "desc"
    # "high" keeps the top quarter of Kentucky coal unit-years: here the one
    # at 850,000 tons, out of 850,000 / 800,000 / 450,000.
    assert params["co2_min"] == "850000"
    rows = _rows(params)
    assert [(r.facility_name, r.reporting_year) for r in rows] == [("Big Coal", 2022)]
    meanings = [r.meaning for r in reading.readings]
    assert "State is Kentucky (KY)" in meanings
    assert any(m.startswith("CO₂ ≥ 850,000 short tons") for m in meanings)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("top 10 facilities by CO2 in 2023",
         {"level": "facility", "year": "2023", "sort": "co2_mass", "limit": "10"}),
        ("units in West Virginia running more than 5,000 hours",
         {"state": "WV", "op_time_min": "5000"}),
        ("gas units with SO2 below 500 tons between 2015 and 2020",
         {"primary_fuel": "gas", "so2_max": "500", "year_min": "2015", "year_max": "2020"}),
        ("highest NOx emitter in each state",
         {"group_by": "state", "per_group": "1", "sort": "nox_mass", "order": "desc"}),
        ("top 3 coal units in each state by SO2",
         {"group_by": "state", "per_group": "3", "sort": "so2_mass"}),
        ("coal units in Ohio without SCR",
         {"nox_control": "not Selective Catalytic", "state": "OH", "primary_fuel": "coal"}),
        ("coal plants without scrubbers", {"level": "facility", "so2_control": "none"}),
        ("units with mercury controls", {"hg_control": "any"}),
        ("CO2 over 1.5 million tons", {"co2_min": "1500000"}),
        ("more than 2000 hours in 2022", {"op_time_min": "2000", "year": "2022"}),
        ("gross load between 100,000 and 1,000,000 MWh",
         {"gross_load_min": "100000", "gross_load_max": "1000000"}),
        ("heat input > 5,000,000 MMBtu", {"heat_input_min": "5000000"}),
        ("plants owned by Duke Energy in North Carolina",
         {"owner": "Duke Energy", "state": "NC", "level": "facility"}),
        ("units in IN and OH", {"state": "IN,OH"}),
        ("units in Mercer County", {"county": "Mercer"}),
        ("RGGI units", {"program": "RGGI"}),
        ("10 largest CO2 emitters last year",
         {"year": "2023", "sort": "co2_mass", "limit": "10"}),
        ("the 5 biggest plants in Texas",
         {"level": "facility", "state": "TX", "sort": "gross_load", "limit": "5"}),
        ("lowest NOx coal units",
         {"sort": "nox_mass", "order": "asc", "op_time_min": "1", "limit": "10"}),
        ("coal units with oil as backup fuel", {"secondary_fuel": "oil", "primary_fuel": "coal"}),
        ("unit 1 at facility 3", {"unit_id": "1", "facility_id": "3"}),
    ],
)
def test_descriptions_become_search_parameters(data, text, expected):
    params = _read(text).params
    assert {key: params.get(key) for key in expected} == expected
    assert not parse_search(MultiDict(params)).errors


def test_control_names_are_not_read_as_pollutant_levels(data):
    params = _read("low nox burners in Alabama").params
    assert params == {"nox_control": "Low NOx Burner", "state": "AL"}


def test_lowercase_state_codes_that_are_words_are_left_alone(data):
    assert _read("coal units in kentucky").params["state"] == "KY"
    # "in" is not Indiana and "or" is not Oregon unless typed as codes.
    assert _read("units in or near ohio").params["state"] == "OH"


def test_quantities_are_not_read_as_years(data):
    params = _read("units running more than 2000 hours").params
    assert params == {"op_time_min": "2000"}


def test_leftover_words_can_name_a_facility(data):
    row = "Mill Creek,5,1,2023,KY,Jefferson County,1,1,1,1,1,1,Coal,Stoker,"
    csv = ("\n".join([HEADER, row]) + "\n").encode()
    ingest(validate(read_table(io.BytesIO(csv), "csv")), dataset_name="mill creek",
           data_source="CSV upload", source_type="upload")
    reading = _read("Show me Mill Creek in 2023")
    assert reading.params == {"facility_name": "Mill Creek", "year": "2023"}
    assert {r.facility_name for r in _rows(reading.params)} == {"Mill Creek"}


def test_unrecognised_words_are_reported_not_guessed(data):
    reading = _read("purple elephants")
    assert reading.params == {}
    assert reading.ignored == ["purple", "elephants"]


def test_readings_follow_the_order_of_the_description(data):
    reading = _read("coal units in Kentucky in 2023")
    assert [r.phrase for r in reading.readings] == ["coal", "Kentucky", "2023"]


def test_mercury_emissions_explain_the_gap(data):
    reading = _read("mercury emissions in Texas")
    assert reading.params == {"state": "TX"}
    assert any("Mercury emissions are not part" in note for note in reading.notes)


# --- explorer page and API ------------------------------------------------------


@pytest.fixture
def client(app):
    return app.test_client()


def test_explorer_redirects_a_description_to_its_filters(client, data):
    response = client.get("/explorer?q=coal units in Kentucky in 2023")
    assert response.status_code == 302
    query = parse_qs(urlparse(response.headers["Location"]).query)
    assert query["state"] == ["KY"] and query["primary_fuel"] == ["coal"]
    assert query["q"] == ["coal units in Kentucky in 2023"]

    page = client.get(response.headers["Location"]).get_data(as_text=True)
    assert "Understood as" in page
    assert "State is Kentucky (KY)" in page
    assert "Big Coal" in page and "Gas Plant" not in page


def test_explorer_says_when_nothing_was_understood(client, data):
    response = client.get("/explorer?q=purple elephants")
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert "No search conditions were found" in page
    assert "purple" in page


def test_explorer_flags_filters_changed_after_reading(client, data):
    page = client.get("/explorer?q=coal units in Kentucky&state=WV&primary_fuel=coal")
    assert "have been changed since" in page.get_data(as_text=True)


def test_interpret_api(client, data):
    body = client.get("/api/emissions/interpret?q=top 10 facilities by CO2 in 2023").get_json()
    assert body["params"]["level"] == "facility"
    assert body["explorer_url"].startswith("/explorer?")
    records = client.get(body["records_url"]).get_json()
    assert records["level"] == "facility" and records["total"] == 4
    assert client.get("/api/emissions/interpret").status_code == 400
