"""AI assistant: the database tool, the Gemini conversation (with a fake client,
so no key or network is needed) and the Settings key form."""

import pytest

from app.services import assistant
from app.services.assistant import query_epa_database, run_emissions_query, system_instruction
from tests.test_analysis import ROWS, _import


@pytest.fixture
def data(app):
    return _import(ROWS)


# --- the database tool -----------------------------------------------------------


def test_stats_leave_unreported_values_out(data):
    result = run_emissions_query(year=2022, state="KY")
    stats = result["stats"]
    assert result["status"] == "success"
    assert stats["record_count"] == 3
    assert stats["co2_mass_total"] == 1500  # unit 2's blank CO2 is not counted as 0
    assert stats["not_reported"] == {"co2_mass": 1, "so2_mass": 1}
    assert stats["co2_mass_average_per_unit_year"] == 750  # over the 2 units that reported
    assert stats["co2_intensity_tons_per_mwh"] == 0.75  # 1,500 t / 2,000 MWh, not / 2,500
    assert stats["heat_rate_mmbtu_per_mwh"] == 8.8


def test_lowest_ranking_puts_unreported_last(data):
    rows = run_emissions_query(year=2022, state="KY", level="unit", order="asc")["rows"]
    assert [r["epa_unit_id"] for r in rows] == ["3", "1", "2"]
    assert rows[-1]["co2_mass"] is None


def test_year_comparison_is_calculated_for_the_model(data):
    rows = run_emissions_query(state="KY", level="year")["rows"]
    assert [r["reporting_year"] for r in rows] == [2020, 2021, 2022]
    latest = rows[-1]
    assert latest["compared_with_year"] == 2021
    assert latest["co2_mass_change_from_previous"] == 500
    assert latest["co2_mass_pct_change_from_previous"] == 50.0
    gap = run_emissions_query(state="WV", level="year")["rows"][-1]
    assert gap["compared_with_year"] == 2020  # 2021 has no data, and the row says so
    assert gap["co2_mass_pct_change_from_previous"] == -10.0


def test_no_records_lists_the_years_with_data(data):
    result = run_emissions_query(state="ZZ")
    assert result == {"status": "no_records", "filters": {"state": "ZZ"},
                      "years_with_data": [2020, 2021, 2022]}


def test_tool_reports_errors_to_the_model(data, monkeypatch):
    def broken(**_):
        raise RuntimeError("boom")
    monkeypatch.setattr(assistant, "run_emissions_query", broken)
    assert query_epa_database(state="KY") == {"status": "error", "message": "boom"}


def test_instruction_names_the_years(data):
    text = system_instruction()
    assert "Reporting years with data: 2020, 2021, 2022" in text
    assert "use the latest one, 2022" in text


def test_tool_schema_for_gemini():
    types = pytest.importorskip("google.genai.types")
    declaration = types.FunctionDeclaration.from_callable_with_api_option(
        callable=query_epa_database, api_option="GEMINI_API"
    )
    properties = declaration.parameters.properties
    assert properties["year"].type == types.Type.INTEGER
    assert properties["state"].type == types.Type.STRING
    assert set(properties) >= {"level", "rank_by", "year_min", "year_max", "primary_fuel"}


# --- the endpoint ------------------------------------------------------------------


class FakeChat:
    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls = 0

    def send_message(self, prompt, config):
        self.calls += 1
        return self.behaviour(prompt, config)


class FakeClient:
    def __init__(self, chat):
        self.chat = chat
        self.created = None
        self.chats = self

    def create(self, model, history):
        self.created = {"model": model, "history": history}
        return self.chat


def fake_gemini(monkeypatch, app, behaviour):
    pytest.importorskip("google.genai")
    app.config["GEMINI_API_KEY"] = "test-gemini-key"
    client = FakeClient(FakeChat(behaviour))
    monkeypatch.setattr(assistant, "make_client", lambda key: client)
    monkeypatch.setattr(assistant.time, "sleep", lambda seconds: None)
    return client


def test_answers_by_calling_the_database_tool(client, app, data, monkeypatch):
    class Reply:
        def __init__(self, text):
            self.text = text

    def behaviour(prompt, config):
        # What Gemini does: call the tool it was given, then explain the result.
        result = config.tools[0](state="KY", year=2022)
        assert "Reporting years with data" in config.system_instruction
        return Reply(f"Kentucky emitted {result['stats']['co2_mass_total']:,.0f} tons of CO2.")

    fake = fake_gemini(monkeypatch, app, behaviour)
    history = [{"role": "user", "text": "hi"}, {"role": "model", "text": "Hello"}, {"role": "x"}]
    response = client.post("/api/ai/query", json={"prompt": "KY CO2 in 2022?", "history": history})
    assert response.status_code == 200
    assert response.get_json() == {"response": "Kentucky emitted 1,500 tons of CO2."}
    assert fake.created["model"] == "gemini-3.5-flash-lite"
    assert [c.role for c in fake.created["history"]] == ["user", "model"]


@pytest.mark.parametrize("error, status, message", [
    ("429 RESOURCE_EXHAUSTED", 429, "free usage limit"),
    ("400 API key not valid. API_KEY_INVALID", 503, "rejected the API key"),
    ("404 models/x is NOT_FOUND", 503, "not available"),
    ("503 UNAVAILABLE", 503, "AI call failed"),
])
def test_gemini_failures_become_clear_messages(client, app, data, monkeypatch, error, status, message):
    def behaviour(prompt, config):
        raise RuntimeError(error)

    fake = fake_gemini(monkeypatch, app, behaviour)
    response = client.post("/api/ai/query", json={"prompt": "anything"})
    assert response.status_code == status
    assert message in response.get_json()["error"]
    assert fake.chat.calls == (3 if "UNAVAILABLE" in error else 1)  # only overload is retried


def test_endpoint_checks_the_prompt(client):
    assert client.post("/api/ai/query", json={}).status_code == 400
    assert client.post("/api/ai/query", json={"prompt": "x" * 2001}).status_code == 400


def test_without_a_key_the_panel_points_to_settings(client):
    response = client.post("/api/ai/query", json={"prompt": "hello"})
    assert response.status_code == 503
    assert "Settings page" in response.get_json()["error"]
    assert "Not set up on this computer yet" in client.get("/").get_data(as_text=True)


def test_without_the_package_the_app_still_works(client, app, monkeypatch):
    monkeypatch.setattr(assistant, "genai", None)
    app.config["GEMINI_API_KEY"] = "test-gemini-key"
    response = client.post("/api/ai/query", json={"prompt": "hello"})
    assert response.status_code == 503
    assert "not installed" in response.get_json()["error"]
    assert client.get("/settings").status_code == 200


# --- Settings: the Gemini key --------------------------------------------------------


def test_saving_and_removing_the_gemini_key(client, app, monkeypatch, tmp_path):
    monkeypatch.setattr("app.routes.settings.verify_key", lambda key: None)
    key = "AIzaSyTestKey0123456789abcdefghijk"
    response = client.post("/settings/gemini-key", data={"gemini_key": key}, follow_redirects=True)
    assert "Google accepted your Gemini API key" in response.get_data(as_text=True)
    assert f"GEMINI_API_KEY={key}" in (tmp_path / ".env").read_text()
    assert app.config["GEMINI_API_KEY"] == key
    page = client.get("/settings").get_data(as_text=True)
    assert key not in page and "••••hijk" in page

    client.post("/settings/gemini-key/remove")
    assert "GEMINI_API_KEY" not in (tmp_path / ".env").read_text()
    assert app.config["GEMINI_API_KEY"] is None


def test_rejected_gemini_key_is_not_saved(client, monkeypatch, tmp_path):
    def reject(key):
        raise assistant.AssistantError("Google rejected that key.", 401)
    monkeypatch.setattr("app.routes.settings.verify_key", reject)
    response = client.post("/settings/gemini-key", data={"gemini_key": "A" * 39},
                           follow_redirects=True)
    assert "Google rejected that key" in response.get_data(as_text=True)
    assert not (tmp_path / ".env").exists()
