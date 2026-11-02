"""Every technical term the app shows has an everyday meaning next to it."""

import re

from app.glossary import GLOSSARY, MEANINGS, TRACI_CATEGORIES, glossary_text, with_meaning
from app.services.analysis import CATEGORICAL, numeric_variables
from app.services.assistant import system_instruction
from app.services.search import METRIC_LABELS, METRICS


def test_every_variable_and_metric_has_a_meaning():
    for name in [*numeric_variables("unit"), *METRICS, "so2_control", "nox_control",
                 "pm_control", "hg_control", "program_code"]:
        assert MEANINGS.get(name), name
    assert set(CATEGORICAL) & set(MEANINGS) >= {"so2_control", "pm_control", "hg_control"}


def test_labels_read_term_then_meaning():
    assert METRIC_LABELS["gross_load"] == "Gross load (electricity generated, MWh)"
    assert METRIC_LABELS["so2_mass"] == "SO2 (sulfur dioxide, short tons)"
    assert with_meaning("Heat rate", "heat_rate") == "Heat rate (fuel burned per MWh; lower is more efficient)"
    assert with_meaning("Something else", "unknown") == "Something else"
    for label in TRACI_CATEGORIES.values():
        assert re.fullmatch(r"[A-Z][^()]+ \([^()]+\)", label), label
    assert TRACI_CATEGORIES["Acidification"] == "Acidification (acid rain)"


def test_glossary_covers_the_impact_categories():
    terms = dict(GLOSSARY)
    assert terms["Acidification"] == "acid rain"
    assert terms["Photochemical ozone formation"] == "smog"
    assert "Heat rate = fuel burned per MWh" in glossary_text()


def test_pages_show_meanings(client, sample_dataset):
    explorer = client.get("/explorer").get_data(as_text=True)
    assert '<span class="th-hint">electricity generated</span>' in explorer
    assert "SO₂ control (equipment that removes sulfur dioxide)" in explorer
    assert "Gross load (electricity generated, MWh)" in explorer  # sort and range labels
    builder = client.get("/analysis").get_data(as_text=True)
    assert "(fuel burned per MWh; lower is more efficient)" in builder
    unit = client.get("/unit/1").get_data(as_text=True)
    assert '<span class="th-hint">filters for dust and soot</span>' in unit
    assert "heat input (energy in the fuel" in client.get("/").get_data(as_text=True)


def test_definitions_and_ai_carry_meanings(client, app, sample_dataset):
    body = client.get("/api/analysis/indicators?per_page=1").get_json()
    assert body["definitions"]["gross_load"]["meaning"] == "electricity generated"
    with app.app_context():
        text = system_instruction()
    assert "acidification (acid rain)" in text and "Glossary:" in text
