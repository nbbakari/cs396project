"""Plain-language meanings of the technical terms used across epaData.

People reading the data may not know the science, so wherever a technical
term is shown it is followed by its everyday meaning in parentheses, for
example "Acidification (acid rain)". Pages, downloads, the AI assistant and
the README take the wording from here so it stays the same everywhere.
"""

from __future__ import annotations

# Everyday meaning of each measurement, indicator and category column, by the
# name the app uses for it. Shown after a label, or under a column heading.
MEANINGS = {
    "operating_time": "hours running",
    "gross_load": "electricity generated",
    "steam_load": "steam produced",
    "heat_input": "energy in the fuel burned",
    "co2_mass": "carbon dioxide",
    "so2_mass": "sulfur dioxide",
    "nox_mass": "nitrogen oxides",
    "co2_intensity": "CO₂ per MWh of electricity",
    "so2_intensity": "SO₂ per MWh of electricity",
    "nox_intensity": "NOx per MWh of electricity",
    "heat_rate": "fuel burned per MWh; lower is more efficient",
    "operating_utilization": "share of the year running",
    "average_load": "average output while running",
    "so2_control": "equipment that removes sulfur dioxide",
    "nox_control": "equipment that removes nitrogen oxides",
    "pm_control": "filters for dust and soot",
    "hg_control": "equipment that removes mercury",
    "program_code": "EPA emissions programs the unit reports under",
    "source_category": "kind of business, e.g. electric utility",
    "units_with_missing_values": "units with a value not reported",
}

# EPA TRACI's impact-category headings -> "Scientific name (everyday meaning)".
TRACI_CATEGORIES = {
    "Global Climate": "Global warming potential (climate change)",
    "Acidification": "Acidification (acid rain)",
    "HH Particulate": "Particulate matter formation (fine dust and soot that harm breathing)",
    "Photochemical Oxidation Formation": "Photochemical ozone formation (smog)",
    "Ozone Depletion": "Ozone depletion (thinning of the ozone layer)",
}

# The glossary shown in the README and given to the AI assistant.
GLOSSARY = [
    ("CO₂", "carbon dioxide, the main gas behind global warming"),
    ("SO₂", "sulfur dioxide, a gas that causes acid rain and harms breathing"),
    ("NOx", "nitrogen oxides, gases that cause smog and acid rain"),
    ("Hg", "mercury, a toxic metal"),
    ("PM", "particulate matter: fine dust and soot"),
    ("Gross load", "the electricity a unit generated"),
    ("Heat input", "the energy in the fuel a unit burned"),
    ("Heat rate", "fuel burned per MWh of electricity; lower means more efficient"),
    ("Emission intensity", "pollution per MWh of electricity generated"),
    ("Operating utilization", "the share of the year a unit was running"),
    ("Unit", "one generator (boiler or turbine) at a power plant"),
    ("Facility", "a power plant, which can have several units"),
    ("MWh", "megawatt-hour, 1,000 kilowatt-hours of electricity"),
    ("MMBtu", "million British thermal units, a measure of heat energy"),
    ("Short ton", "2,000 pounds (907 kg)"),
    ("Year-over-year change", "the change from the year before"),
    *((label.split(" (")[0], label.split(" (", 1)[1].rstrip(")")) for label in TRACI_CATEGORIES.values()),
    ("Characterization factor", "how strongly 1 kg of a pollutant adds to an impact"),
    ("CO₂-eq, SO₂-eq, O₃-eq, PM2.5-eq",
     "'equivalent': the amount of the reference pollutant that would have the same effect"),
    ("Potential impact", "a comparison of possible harm, not measured damage to people or places"),
]


def meaning(key: str) -> str:
    """The everyday meaning of a column, or an empty string."""
    return MEANINGS.get(key, "")


def with_meaning(label: str, key: str, unit: str = "") -> str:
    """'Gross load (electricity generated, MWh)': a label, its meaning and unit."""
    inside = ", ".join(part for part in (MEANINGS.get(key, ""), unit) if part)
    return f"{label} ({inside})" if inside else label


def traci_category(name: str) -> str:
    """A TRACI impact category as 'Scientific name (everyday meaning)'."""
    return TRACI_CATEGORIES.get(name, name)


def glossary_text() -> str:
    """The glossary as one line, for the AI assistant's instructions."""
    return "; ".join(f"{term} = {plain}" for term, plain in GLOSSARY)
