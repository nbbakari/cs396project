"""Description-based search: read a plain-English request as explorer filters.

"Find coal units in Kentucky with high CO2 emissions" becomes::

    primary_fuel=coal  state=KY  co2_min=<top-quarter threshold>  sort=co2_mass

The interpreter is rule-based, so it runs offline and gives the same answer
every time. It recognises states, counties, fuels, unit types, control
equipment, programs, owners, facility names and IDs, years, quantities with
comparison words ("more than 5,000 hours", "SO2 below 500 tons", "between
100,000 and 1,000,000 MWh"), and ranking words ("top 10", "highest", "high",
"in each state").

Its output is ordinary explorer parameters, so a description lands on a normal
explorer URL that can be bookmarked, edited in the filter form, or downloaded.
Every phrase it used is reported with the condition it became, and words it
could not use are listed, so the user can see exactly how the request was read.

Rules run in a fixed order, and each one blanks out the words it used so that
later rules cannot read them again: "low NOx burners" is a control before it
can be "low NOx", "greenhouse gas" is CO2 before "gas" can be a fuel, and
"2,000 hours" is a quantity before "2000" can be a year.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import func, select
from werkzeug.datastructures import MultiDict

from app import db
from app.models import Facility
from app.services.campd import STATES
from app.services.search import build_statement, parse_search

MAX_LENGTH = 300

# "high CO2" keeps the top quarter of the rows the other conditions match;
# "low CO2" keeps the bottom quarter of those that operated.
QUANTILE = 0.25
# Rows returned for "highest ..." / "lowest ..." when no number is given.
DEFAULT_TOP_N = 10
# Leftover words are tried as a facility name when they match at most this
# many facilities ("Barry", "Fort Martin"); broader words are left alone.
MAX_NAME_MATCHES = 25


@dataclass
class Reading:
    """One phrase from the description and the condition it became.

    ``phrase`` is empty for a condition implied by others (such as the top-10
    limit on "highest CO2").
    """

    phrase: str
    meaning: str
    # Where the phrase starts in the description, so readings follow its order.
    position: int = field(default=1_000_000, repr=False, compare=False)


@dataclass
class Interpretation:
    text: str
    params: dict[str, str] = field(default_factory=dict)
    readings: list[Reading] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "description": self.text,
            "params": self.params,
            "readings": [{"phrase": r.phrase, "meaning": r.meaning} for r in self.readings],
            "ignored": self.ignored,
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

# metric -> (range-filter prefix, sort column, label, unit of measure)
METRICS = {
    "co2": ("co2", "co2_mass", "CO₂", "short tons"),
    "so2": ("so2", "so2_mass", "SO₂", "short tons"),
    "nox": ("nox", "nox_mass", "NOx", "short tons"),
    "heat_input": ("heat_input", "heat_input", "heat input", "MMBtu"),
    "gross_load": ("gross_load", "gross_load", "gross load", "MWh"),
    "op_time": ("op_time", "operating_time", "operating time", "hours"),
}
POLLUTANTS = ("co2", "so2", "nox")

METRIC_WORDS = [
    ("co2", r"carbon\s+dioxide|co2|co₂|carbon|greenhouse\s+gas(?:es)?|ghgs?"),
    ("so2", r"sulfur\s+dioxide|sulphur\s+dioxide|so2|so₂|sulfur|sulphur"),
    ("nox", r"nitrogen\s+oxides?|nox|noₓ"),
    ("heat_input", r"heat\s+input|heat|fuel\s+use|fuel\s+consumption"),
    ("gross_load", r"gross\s+load|gross\s+generation|net\s+generation|electricity\s+generation"
                   r"|generation|electricity|power\s+output|output|load|megawatt[- ]hours|mwh"),
    ("op_time", r"operating\s+time|operating\s+hours|hours\s+of\s+operation|hours\s+operated"
                r"|run\s*time|hours"),
]
METRIC = "(?:" + "|".join(pattern for _, pattern in METRIC_WORDS) + ")"
METRIC_LIST = rf"{METRIC}(?:\s*(?:,|and|&|/)\s*{METRIC})*"
# Verbs that name a measure: "running more than 5,000 hours".
RUN_VERB = r"running|ran|runs|run|operat(?:ing|ed|es)"
EMISSION_WORD = r"emissions?|emitters?|emitting|emits?|polluters?|polluting|pollution"

HIGHEST = r"highest|most|largest|biggest|greatest|top|worst|dirtiest|heaviest|leading|maximum"
LOWEST = r"lowest|least|smallest|fewest|minimum|bottom"
HIGH = r"high|higher|heavy|large|big|significant|major|elevated|lots\s+of|a\s+lot\s+of"
LOW = r"low|lower|small|little|minimal|light"
# Words that size a unit or plant rather than name a pollutant.
SIZE_WORDS = {"largest", "biggest", "smallest", "large", "big", "huge", "small"}

GT = (r"more\s+than|greater\s+than|higher\s+than|larger\s+than|bigger\s+than|in\s+excess\s+of"
      r"|over|above|exceed(?:ing|s)?|at\s+least|no\s+less\s+than|minimum\s+of|>=|≥|>")
LT = (r"less\s+than|fewer\s+than|lower\s+than|smaller\s+than|under|below|at\s+most"
      r"|no\s+more\s+than|up\s+to|maximum\s+of|<=|≤|<")

NUMBER = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
MULTIPLIERS = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mm": 1e6, "mil": 1e6, "million": 1e6,
               "b": 1e9, "bn": 1e9, "billion": 1e9}
MULTIPLIER = r"(?:thousand|million|billion|mil|mm|bn|k|m|b)\b"
UNIT = (r"(?:short\s+tons?|tons?|tonnes?|tpy|mwh|megawatt[- ]hours?|mmbtu|million\s+btu"
        r"|hours?|hrs?|h)\b")
YEAR = r"(?:19[89]\d|20[0-4]\d)"
# Words that end a captured name ("owned by Duke Energy in North Carolina").
NAME_END = (r"(?=\s+(?:in|with|that|which|who|and|for|during|from|since|between|before|after"
            r"|by|owned|operated|where|having|whose|using|burning|emitting|running|over|under"
            r"|above|below|more|less|than)\b|\s*[,;:!?)]|\s*\.(?:\s|$)|\s*$)")

STATE_NAMES = {name.lower(): code for code, name in STATES}
STATE_CODES = {code for code, _ in STATES}
# Two-letter codes that are also everyday words; typed in lower case they are
# read as words, so "units in kentucky" is not Indiana.
AMBIGUOUS_CODES = {"in", "or", "me", "ok", "hi", "de", "al", "co", "ma", "la", "id", "oh",
                   "pa", "ms", "ne"}

# (pattern, value for the unit_type "contains" filter). Specific names first.
UNIT_TYPES = [
    (r"igcc|integrated\s+gasification(?:\s+combined[- ]cycle)?", "integrated gasification"),
    (r"combined[- ]cycles?(?:\s+(?:units?|plants?))?|ngccs?|ccgts?", "combined cycle"),
    (r"(?:combustion|gas|simple[- ]cycle|peaking)\s+turbines?|simple[- ]cycle|peakers?"
     r"|peaking\s+units?", "combustion turbine"),
    (r"tangential(?:ly)?[- ]fired(?:\s+boilers?)?|tangential", "tangentially"),
    (r"cyclone\s+boilers?", "cyclone boiler"),
    (r"stokers?", "stoker"),
    (r"circulating\s+fluidi[sz]ed[- ]bed(?:\s+boilers?)?|cfbs?", "circulating fluidized"),
    (r"(?:bubbling\s+|pressurized\s+)?fluidi[sz]ed[- ]bed(?:\s+boilers?)?|fbcs?", "fluidized bed"),
    (r"wall[- ]fired(?:\s+boilers?)?", "wall-fired"),
    (r"cell\s+burners?(?:\s+boilers?)?", "cell burner"),
    (r"arch[- ]fired(?:\s+boilers?)?", "arch-fired"),
    (r"(?:internal\s+combustion|reciprocating)\s+engines?|engines?", "internal combustion"),
    (r"process\s+heaters?", "process heater"),
    (r"boilers?", "boiler"),
    (r"turbines?", "turbine"),
]

# (pattern, value for the fuel "contains" filters). Specific names first.
FUELS = [
    (r"pipeline\s+natural\s+gas", "pipeline natural gas"),
    (r"natural[- ]gas(?:[- ](?:fired|burning))?|nat\s+gas", "natural gas"),
    (r"coal\s+refuse|waste\s+coal", "coal refuse"),
    (r"petroleum\s+coke|pet\s*coke", "petroleum coke"),
    (r"coal(?:[- ](?:fired|burning))?", "coal"),
    (r"diesel(?:\s+oil)?", "diesel"),
    (r"residual(?:\s+fuel)?\s+oil", "residual oil"),
    (r"(?:fuel\s+)?oil(?:[- ](?:fired|burning))?", "oil"),
    (r"wood|biomass", "wood"),
    (r"tire[- ]derived\s+fuel|tires?", "tire"),
    (r"lpg|propane|liqu[ei]fied\s+petroleum\s+gas", "liquified petroleum"),
    (r"process\s+gas", "process gas"),
    (r"(?<!greenhouse\s)gas(?:[- ](?:fired|burning))?", "gas"),
]
FUEL = "(?:" + "|".join(pattern for pattern, _ in FUELS) + ")"

# (pattern, filter, value, plain-words name). Specific names first.
CONTROLS = [
    (r"low[- ]nox\s+burners?(?:\s+technology)?|lnbs?", "nox_control", "Low NOx Burner",
     "low-NOx burners"),
    (r"selective\s+non[- ]?catalytic\s+reduction|sncr", "nox_control", "Selective Non-catalytic",
     "SNCR"),
    (r"selective\s+catalytic\s+reduction|scrs?", "nox_control", "Selective Catalytic", "SCR"),
    (r"overfire\s+air|ofa", "nox_control", "Overfire Air", "overfire air"),
    (r"wet\s+limestone", "so2_control", "Wet Limestone", "wet limestone scrubbers"),
    (r"dry\s+sorbent\s+injection|dsi", "so2_control", "Dry Sorbent", "dry sorbent injection"),
    (r"scrubbers?|scrubbed|fgds?|flue[- ]gas\s+desulfuri[sz]ation|desulfuri[sz]ation",
     "so2_control", "any", "scrubbers (any SO₂ control)"),
    (r"baghouses?|fabric\s+filters?", "pm_control", "Baghouse", "baghouses"),
    (r"wet\s+esps?|wet\s+electrostatic\s+precipitators?", "pm_control", "Wet ESP", "wet ESPs"),
    (r"electrostatic\s+precipitators?|esps?", "pm_control", "Electrostatic Precipitator",
     "electrostatic precipitators"),
    (r"activated\s+carbon(?:\s+injection)?|aci|pac(?:\s+sorbent)?(?:\s+injection)?"
     r"|carbon\s+injection", "hg_control", "PAC Sorbent", "activated-carbon injection"),
]
# "SO2 controls", "mercury control equipment"
CONTROL_POLLUTANTS = [
    (r"so2|so₂|sulfur(?:\s+dioxide)?|sulphur(?:\s+dioxide)?", "so2_control", "SO₂"),
    (r"nox|noₓ|nitrogen\s+oxides?", "nox_control", "NOx"),
    (r"pm|particulate(?:\s+matter)?|particulates|dust", "pm_control", "PM"),
    (r"mercury|hg", "hg_control", "mercury"),
]
NEGATION = r"(?:without|with\s+no|no|not|lacking|lacks?|w/o|excluding|minus|non)"

PROGRAMS = [
    (r"acid\s+rain(?:\s+program)?|arp", "ARP", "Acid Rain Program"),
    (r"rggi|regional\s+greenhouse\s+gas\s+initiative", "RGGI", "RGGI"),
    (r"csapr|cross[- ]state(?:\s+air\s+pollution\s+rule)?", "CS", "Cross-State (CSAPR) programs"),
    (r"nox\s+budget(?:\s+program)?|nbp", "NBP", "NOx Budget Program"),
    (r"texas\s+so2(?:\s+trading)?|txso2", "TXSO2", "Texas SO₂ trading"),
]

GROUP_WORDS = {
    "state": "state", "year": "year", "fuel": "primary_fuel", "fuel type": "primary_fuel",
    "unit type": "unit_type", "facility": "facility", "plant": "facility",
}
GROUP_LABELS = {
    "state": "state", "year": "year", "primary_fuel": "primary fuel",
    "unit_type": "unit type", "facility": "facility",
}

STOPWORDS = set("""
a an the and or of in on at to for from with within by as is are was were be been being
that which who whose where what when how i me my we our us you your show find list get give
display search look looking see want need please all any some every their its it this these
those there here than then also only just about around across into near like such data record
records result results row rows unit units facility facilities plant plants power station
stations generator generators generating site sites emission emissions emitting emit emits
emitted emitter emitters polluting pollution polluter polluters fired fueled powered burning
burn burns using use uses used located based state states year years reporting reported annual
total totals had have has having did do does done can could would should will tell which
whats what's electric utility utilities level levels amount amounts value values each per s
but yet so if both either neither nor very really much many more less among between including
include includes only over under also too again ones one kind kinds type types
""".split())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _metric_of(word: Optional[str]) -> Optional[str]:
    if not word:
        return None
    word = re.sub(r"\s+", " ", word.strip())
    for key, pattern in METRIC_WORDS:
        if re.fullmatch(pattern, word):
            return key
    if re.fullmatch(RUN_VERB, word):
        return "op_time"
    return None


def _metrics_in(text: str) -> list[str]:
    """Every metric named in a phrase such as "SO2 and NOx", in order."""
    found = []
    for match in re.finditer(METRIC, text):
        key = _metric_of(match.group(0))
        if key and key not in found:
            found.append(key)
    return found


def _unit_kind(unit: Optional[str]) -> Optional[str]:
    if not unit:
        return None
    unit = unit.strip()
    if re.fullmatch(r"short\s+tons?|tons?|tonnes?|tpy", unit):
        return "mass"
    if re.fullmatch(r"mwh|megawatt[- ]hours?", unit):
        return "gross_load"
    if re.fullmatch(r"mmbtu|million\s+btu", unit):
        return "heat_input"
    return "op_time"


def _number(text: str, multiplier: Optional[str]) -> float:
    value = float(text.replace(",", ""))
    return value * MULTIPLIERS.get((multiplier or "").strip(), 1)


def _plain(value: float) -> str:
    """A number as a URL parameter: no exponent, no trailing ".0"."""
    return str(int(value)) if float(value).is_integer() else f"{value:.6g}"


def _shown(value: float) -> str:
    return f"{value:,.0f}" if abs(value) >= 100 or float(value).is_integer() else f"{value:,.2f}"


def _round_sig(value: float, *, down: bool) -> float:
    """Round to three significant figures, toward the side that keeps the row."""
    if value <= 0:
        return 0.0
    step = 10 ** (math.floor(math.log10(value)) - 2)
    rounded = (math.floor if down else math.ceil)(value / step) * step
    return float(round(rounded, 6))


# ---------------------------------------------------------------------------
# Interpreter
# ---------------------------------------------------------------------------


class _Reader:
    """Reads one description; each ``_read_*`` method handles one kind of phrase."""

    def __init__(self, text: str, latest_year: Optional[int]):
        self.text = text
        # Lower-case one character at a time, so positions match the original.
        self.lower = "".join(c.lower() if len(c.lower()) == 1 else c for c in text)
        self.work = self.lower
        self.latest_year = latest_year
        self.result = Interpretation(text=text)

        self.text_filters: dict[str, str] = {}
        self.states: list[str] = []
        self.years: list[int] = []
        self.year_min: Optional[int] = None
        self.year_max: Optional[int] = None
        self.ranges: dict[str, list[Optional[float]]] = {}
        self.facility_ids: list[str] = []
        self.level = "unit"
        self.sort: Optional[str] = None  # a METRICS key
        self.order: Optional[str] = None
        self.limit: Optional[int] = None
        self.count: Optional[int] = None  # "top 5"
        self.superlative = False
        self.group_by: Optional[str] = None
        self.relative: list[tuple[str, bool, str]] = []  # (metric, high?, phrase)

    # --- bookkeeping ------------------------------------------------------

    def matches(self, pattern: str) -> list[re.Match]:
        return list(re.finditer(pattern, self.work))

    def phrase(self, start: int, end: int) -> str:
        return " ".join(self.text[start:end].split())

    def use(self, match: re.Match, meaning: Optional[str] = None,
            span: Optional[tuple[int, int]] = None) -> None:
        start, end = span or match.span()
        self.work = self.work[:start] + " " * (end - start) + self.work[end:]
        if meaning:
            self.result.readings.append(Reading(self.phrase(start, end), meaning, start))

    def note(self, message: str) -> None:
        if message not in self.result.notes:
            self.result.notes.append(message)

    def set_text(self, name: str, value: str) -> bool:
        """Set a "contains" filter once; a second value is reported, not merged."""
        if name in self.text_filters and self.text_filters[name] != value:
            self.note(
                f"Only one {name.replace('_', ' ')} can be searched at a time, "
                f"so “{value}” was not used."
            )
            return False
        self.text_filters[name] = value
        return True

    def set_range(self, metric: str, low: Optional[float], high: Optional[float]) -> None:
        prefix = METRICS[metric][0]
        current = self.ranges.setdefault(prefix, [None, None])
        if low is not None:
            current[0] = low
        if high is not None:
            current[1] = high

    def derived(self, meaning: str) -> None:
        """Record a condition implied by other phrases rather than typed."""
        self.result.readings.append(Reading("", meaning))

    def negated_before(self, start: int) -> Optional[int]:
        """Where a "without"/"no" just before position ``start`` begins, if any."""
        window = self.lower[max(0, start - 30):start]
        found = re.search(rf"\b{NEGATION}\s+(?:an?\s+|any\s+)?$", window)
        return max(0, start - 30) + found.start() if found else None

    # --- rules, in the order they run --------------------------------------

    def run(self) -> Interpretation:
        self._read_quoted_name()
        self._read_owner()
        self._read_named_facility()
        self._read_facility_id()
        self._read_unit_id()
        self._read_county()
        self._read_state_names()
        self._read_programs()
        self._read_controls()
        self._read_mercury_emissions()
        self._read_unit_types()
        self._read_fuels()
        self._read_year_ranges()
        self._read_quantities()
        self._read_years()
        self._read_numbered_ranking()
        self._read_groups()
        self._read_superlatives()
        self._read_relative()
        self._read_level()
        self._read_operating()
        self._read_plain_metric()
        self._read_state_codes()
        self._read_facility_name()
        self._collect_ignored()
        self._finish()
        self.result.readings.sort(key=lambda reading: reading.position)
        return self.result

    def _read_quoted_name(self) -> None:
        for match in self.matches(r"(?:\b(?:named|called)\s+)?[\"“”]([^\"“”]{2,60})[\"“”]"):
            name = self.text[match.start(1):match.end(1)].strip()
            if name and self.set_text("facility_name", name):
                self.use(match, f"Facility name contains “{name}”")

    def _read_owner(self) -> None:
        pattern = rf"\b(?:owned|operated|run|managed)\s+by\s+(?:the\s+)?(.+?){NAME_END}"
        pattern2 = rf"\bbelonging\s+to\s+(?:the\s+)?(.+?){NAME_END}"
        for match in self.matches(pattern) + self.matches(pattern2):
            owner = self.text[match.start(1):match.end(1)].strip(" .")
            if not self.work[match.start(1):match.end(1)].strip():
                continue
            if owner and len(owner.split()) <= 6 and self.set_text("owner", owner):
                self.use(match, f"Owner or operator contains “{owner}”")

    def _read_named_facility(self) -> None:
        pattern = rf"\b(?:named|called)\s+(.+?){NAME_END}"
        for match in self.matches(pattern):
            name = self.text[match.start(1):match.end(1)].strip(" .")
            if not self.work[match.start(1):match.end(1)].strip():
                continue
            if name and len(name.split()) <= 6 and self.set_text("facility_name", name):
                self.use(match, f"Facility name contains “{name}”")

    def _read_facility_id(self) -> None:
        pattern = r"\b(?:facility|plant|oris)\s*(?:id|code|number|no\.?)?\s*#?\s*(\d{1,6})\b"
        for match in self.matches(pattern):
            self.facility_ids.append(match.group(1))
            self.use(match, f"Facility ID {match.group(1)}")

    def _read_unit_id(self) -> None:
        for match in self.matches(r"\bunit\s+(?:id\s+)?#?\s*([a-z]*\d[a-z0-9-]*)\b"):
            unit_id = self.text[match.start(1):match.end(1)]
            if self.set_text("unit_id", unit_id):
                self.use(match, f"EPA unit ID is “{unit_id}”")

    def _read_county(self) -> None:
        for match in self.matches(r"\b([a-z.'-]+(?:\s+[a-z.'-]+)?)\s+(?:county|parish)\b"):
            words = self.text[match.start(1):match.end(1)].split()
            while words and words[0].lower() in STOPWORDS:
                words.pop(0)
            if not words:
                continue
            county = " ".join(words)
            start = match.start(1) + self.text[match.start(1):match.end(1)].find(words[0])
            if self.set_text("county", county):
                self.use(match, f"County contains “{county}”", span=(start, match.end()))

    def _read_state_names(self) -> None:
        names = sorted(STATE_NAMES, key=len, reverse=True)
        dc = r"washington,?\s+d\.?\s?c\.?|district\s+of\s+columbia|d\.c\."
        pattern = rf"\b(?:{dc}|" + "|".join(re.escape(n).replace(r"\ ", r"\s+") for n in names) + r")\b"
        for match in self.matches(pattern):
            words = " ".join(match.group(0).split())
            code = "DC" if re.fullmatch(dc, words) else STATE_NAMES.get(words)
            if code and code not in self.states:
                self.states.append(code)
                name = dict(STATES)[code]
                self.use(match, f"State is {name} ({code})")

    def _read_programs(self) -> None:
        for pattern, code, name in PROGRAMS:
            for match in self.matches(rf"\b(?:{pattern})\b"):
                if self.set_text("program", code):
                    self.use(match, f"Program codes include {code} ({name})")

    def _read_controls(self) -> None:
        for pattern, name, value, words in CONTROLS:
            for match in self.matches(rf"\b(?:{pattern})\b"):
                self._control(match, name, value, words)

        generic = (r"\b(?P<pol>" + "|".join(p for p, _, _ in CONTROL_POLLUTANTS) + r")"
                   r"\s+(?:emissions?\s+)?(?:controls?|control\s+(?:equipment|devices?|technology))\b")
        generic_after = (r"\b(?:controls?|control\s+equipment)\s+(?:for|on)\s+(?P<pol>"
                         + "|".join(p for p, _, _ in CONTROL_POLLUTANTS) + r")\b")
        for match in self.matches(generic) + self.matches(generic_after):
            pol = match.group("pol")
            for pattern, name, label in CONTROL_POLLUTANTS:
                if re.fullmatch(pattern, " ".join(pol.split())):
                    self._control(match, name, "any", f"{label} controls")
                    break

        # "without any controls", "uncontrolled": no SO2, NOx or PM equipment.
        for match in self.matches(rf"\b{NEGATION}\s+(?:any\s+)?(?:emissions?\s+)?controls?\b"
                                  r"|\buncontrolled\b"):
            for name in ("so2_control", "nox_control", "pm_control"):
                self.set_text(name, "none")
            self.use(match, "No SO₂, NOx or PM control equipment")

    def _control(self, match: re.Match, name: str, value: str, words: str) -> None:
        negation = self.negated_before(match.start())
        if negation is not None:
            # "without scrubbers" means no SO2 control at all; "without SCR"
            # means any NOx equipment except SCR.
            value = "none" if value == "any" else f"not {value}"
            meaning = (f"No {words.split(' (')[0]}" if value == "none"
                       else f"{_FILTER_NAMES[name]} do not include {value[4:]}")
            if self.set_text(name, value):
                self.use(match, meaning, span=(negation, match.end()))
            return
        meaning = (f"Has {words}" if value == "any"
                   else f"{_FILTER_NAMES[name]} include {value}")
        if self.set_text(name, value):
            self.use(match, meaning)

    def _read_mercury_emissions(self) -> None:
        for match in self.matches(r"\b(?:mercury|hg)(?:\s+(?:emissions?|mass|output))?\b"):
            self.use(match)
            self.note(
                "Mercury emissions are not part of CAMPD's annual data, so “mercury” was "
                "not used as a measure. Mercury control equipment can be searched: try "
                "“units with mercury controls”."
            )

    def _read_unit_types(self) -> None:
        for pattern, value in UNIT_TYPES:
            for match in self.matches(rf"\b(?:{pattern})\b"):
                if self.set_text("unit_type", value):
                    self.use(match, f"Unit type contains “{value}”")

    def _read_fuels(self) -> None:
        secondary = (rf"\b(?:secondary|backup|back-up)\s+(?:fuel\s+(?:of\s+|is\s+)?)?(?P<f>{FUEL})\b"
                     rf"|\b(?P<f2>{FUEL})\s+as\s+(?:an?\s+|the\s+)?(?:secondary|backup|back-up)"
                     r"(?:\s+fuel)?\b")
        for match in self.matches(secondary):
            fuel_text = match.group("f") or match.group("f2")
            value = self._fuel_value(fuel_text)
            if value and self.set_text("secondary_fuel", value):
                self.use(match, f"Secondary fuel contains “{value}”")
        for pattern, value in FUELS:
            for match in self.matches(rf"\b(?:{pattern})\b"):
                if self.set_text("primary_fuel", value):
                    self.use(match, f"Primary fuel contains “{value}”")

    @staticmethod
    def _fuel_value(text: str) -> Optional[str]:
        for pattern, value in FUELS:
            if re.fullmatch(pattern, " ".join(text.split())):
                return value
        return None

    def _read_year_ranges(self) -> None:
        not_quantity = rf"(?!\s*(?:{MULTIPLIER}|{UNIT}))"
        ranges = [
            rf"\b(?:between|from)\s+(?P<a>{YEAR})\s*(?:and|to|through|thru|until|-|–|—)\s*"
            rf"(?P<b>{YEAR})\b{not_quantity}",
            rf"\b(?P<a>{YEAR})\s*(?:-|–|—|to|through|thru)\s*(?P<b>{YEAR})\b{not_quantity}",
        ]
        for pattern in ranges:
            for match in self.matches(pattern):
                low, high = sorted((int(match.group("a")), int(match.group("b"))))
                self.year_min, self.year_max = low, high
                self.use(match, f"Reporting years {low}–{high}")

        bounds = (rf"\b(?P<w>since|after|from|starting(?:\s+in)?|beginning(?:\s+in)?|before"
                  rf"|until|through|thru|prior\s+to|up\s+to|no\s+later\s+than)\s+"
                  rf"(?P<y>{YEAR})\b{not_quantity}")
        for match in self.matches(bounds):
            word, year = " ".join(match.group("w").split()), int(match.group("y"))
            if word in ("after",):
                self.year_min = year + 1
                self.use(match, f"Reporting years {year + 1} and later")
            elif word in ("before", "prior to"):
                self.year_max = year - 1
                self.use(match, f"Reporting years {year - 1} and earlier")
            elif word in ("until", "through", "thru", "up to", "no later than"):
                self.year_max = year
                self.use(match, f"Reporting years up to {year}")
            else:
                self.year_min = year
                self.use(match, f"Reporting years {year} and later")

        if self.latest_year is not None:
            for match in self.matches(r"\b(?:in\s+)?(?:the\s+)?(?:last|past|previous|recent)\s+"
                                      r"(?P<n>\d{1,2})\s+years?\b"):
                n = int(match.group("n"))
                self.year_min = self.latest_year - n + 1
                self.use(match, f"Reporting years {self.year_min}–{self.latest_year} "
                                f"(the last {n} on file)")
            for match in self.matches(r"\b(?:(?:in\s+)?(?:the\s+)?(?:last|latest|most\s+recent"
                                      r"|this|current)\s+(?:reporting\s+)?year|latest"
                                      r"|most\s+recent(?:ly)?|currently)\b"):
                self.years = [self.latest_year]
                self.use(match, f"Reporting year {self.latest_year} (the latest on file)")

    def _read_quantities(self) -> None:
        before = (rf"(?:(?P<mb>{METRIC}|{RUN_VERB}|{EMISSION_WORD})"
                  r"(?:\s+(?:emissions?|mass|levels?|output|of|is|are|was|were|totals?"
                  r"|totaling|totalling|that\s+(?:is|are|was|were)))*\s*)?")
        after = rf"(?:\s+(?:of\s+)?(?P<ma>{METRIC})(?:\s+emissions?)?)?"

        def number(name: str) -> str:
            return rf"(?P<{name}>{NUMBER})(?:\s*(?P<{name}_mult>{MULTIPLIER}))?"

        between = (rf"\b{before}(?:between|from)\s+{number('a')}(?:\s*(?P<ua>{UNIT}))?\s*"
                   rf"(?:and|to|-|–|—)\s*{number('b')}(?:\s*(?P<unit>{UNIT}))?{after}")
        dashed = (rf"\b{before}{number('a')}\s*(?:-|–|—|to)\s*{number('b')}\s*"
                  rf"(?P<unit>{UNIT}){after}")
        for pattern in (between, dashed):
            for match in self.matches(pattern):
                unit = match.group("unit") or match.groupdict().get("ua")
                metric = self._quantity_metric(match, unit)
                if metric is None:
                    continue
                low = _number(match.group("a"), match.group("a_mult"))
                high = _number(match.group("b"), match.group("b_mult"))
                low, high = sorted((low, high))
                self.set_range(metric, low, high)
                label, measure = METRICS[metric][2], METRICS[metric][3]
                self.use(match, f"{label} between {_shown(low)} and {_shown(high)} {measure}")

        single = (rf"\b{before}(?P<cmp>{GT}|{LT})\s*{number('n')}"
                  rf"(?:\s*(?P<unit>{UNIT}))?{after}")
        # "<" and ">" are not word characters, so they cannot follow \b.
        single_symbol = single.replace(r"\b", "", 1)
        seen: set[tuple[int, int]] = set()
        for pattern in (single, single_symbol):
            for match in self.matches(pattern):
                if match.span() in seen or not self.work[match.start():match.end()].strip():
                    continue
                seen.add(match.span())
                metric = self._quantity_metric(match, match.group("unit"))
                if metric is None:
                    continue
                value = _number(match.group("n"), match.group("n_mult"))
                is_min = re.fullmatch(GT, " ".join(match.group("cmp").split())) is not None
                self.set_range(metric, value if is_min else None, None if is_min else value)
                label, measure = METRICS[metric][2], METRICS[metric][3]
                sign = "≥" if is_min else "≤"
                self.use(match, f"{label} {sign} {_shown(value)} {measure}")

    def _quantity_metric(self, match: re.Match, unit: Optional[str]) -> Optional[str]:
        kind = _unit_kind(unit)
        if kind in ("gross_load", "heat_input", "op_time"):
            return kind
        named = _metric_of(match.group("mb")) or _metric_of(match.group("ma"))
        if kind is None and named is None:
            generic = match.group("mb") and re.fullmatch(EMISSION_WORD, match.group("mb"))
            if not generic:
                return None  # a bare number: leave it for the year and ranking rules
        if named in POLLUTANTS or (named and kind is None):
            return named
        # Tons of something unnamed: the pollutant the request mentions, or CO2.
        mentioned = [m for m in _metrics_in(self.lower) if m in POLLUTANTS]
        if len(mentioned) == 1:
            return mentioned[0]
        self.note("A quantity in tons named no pollutant, so it was read as CO₂.")
        return "co2"

    def _read_years(self) -> None:
        pattern = rf"\b(?P<y>{YEAR})\b(?!\s*(?:{MULTIPLIER}|{UNIT}))"
        for match in self.matches(pattern):
            year = int(match.group("y"))
            if year not in self.years:
                self.years.append(year)
            self.use(match, f"Reporting year {year}")

    def _read_numbered_ranking(self) -> None:
        then = (rf"(?:\s+(?P<m>{METRIC_LIST}))?(?:\s+(?P<e>{EMISSION_WORD}))?"
                r"(?!\s+years?\b)")
        patterns = [
            rf"\b(?:the\s+)?(?P<dir>top|bottom|first|last)\s+(?P<n>\d{{1,3}})\b{then}",
            rf"\b(?P<n>\d{{1,3}})\s+(?P<dir>{HIGHEST}|{LOWEST})\b{then}",
        ]
        for pattern in patterns:
            for match in self.matches(pattern):
                n = int(match.group("n"))
                if n < 1:
                    continue
                direction = match.group("dir")
                low = direction in ("bottom", "last") or re.fullmatch(LOWEST, direction)
                self.count = n
                self.order = "asc" if low else "desc"
                metric = self._ranking_metric(match, direction)
                if metric and self.sort is None:
                    self.sort = metric
                label = f" by {METRICS[metric][2]}" if metric else ""
                self.use(match, f"{'Bottom' if low else 'Top'} {n}{label}")

    def _ranking_metric(self, match: re.Match, word: str) -> Optional[str]:
        """The measure a ranking phrase names, if it names one."""
        metrics = _metrics_in(match.group("m") or "")
        if len(metrics) > 1:
            self.note("Results can be ranked by one measure at a time, so they are "
                      f"ranked by {METRICS[metrics[0]][2]}.")
        if metrics:
            return metrics[0]
        if match.groupdict().get("e") or re.search(rf"\b(?:{EMISSION_WORD})\b", match.group(0)):
            self.note("“Emissions” with no pollutant named is read as CO₂; name SO₂ or NOx "
                      "to rank by those instead.")
            return "co2"
        if word in SIZE_WORDS:
            return "gross_load"
        return None

    def _read_groups(self) -> None:
        target = r"(?P<g>states?|years?|fuel\s+types?|fuels?|unit\s+types?|facilit(?:y|ies)|plants?)"
        patterns = [
            rf"\b(?:in|for|within|from|of)?\s*(?:each|every)\s+{target}\b",
            rf"\bper\s+{target}\b",
            rf"\bby\s+{target}\b",
            r"\b(?P<g>state)[-'’]?s?\s+(?:top|highest|largest|biggest)\b",
        ]
        for pattern in patterns:
            for match in self.matches(pattern):
                word = re.sub(r"ies$", "y", " ".join(match.group("g").split()))
                word = re.sub(r"s$", "", word)
                group = GROUP_WORDS.get(word)
                if group and self.group_by in (None, group):
                    self.group_by = group
                    self.use(match, f"Ranked within each {GROUP_LABELS[group]}")

    def _read_superlatives(self) -> None:
        pattern = (rf"\b(?:the\s+)?(?P<q>{HIGHEST}|{LOWEST}|dirtiest|worst)"
                   rf"(?:[- ](?P<e>emitting|polluting|emissions?))?"
                   rf"(?:\s+(?:(?:levels?|amounts?|quantit(?:y|ies)|volumes?|totals?)\s+of\s+)?"
                   rf"(?:in\s+|of\s+)?(?P<m>{METRIC_LIST}))?"
                   rf"(?:\s+(?:{EMISSION_WORD}))?")
        for match in self.matches(pattern):
            word = match.group("q")
            if word in ("most", "least") and not (match.group("m") or match.group("e")
                                                   or re.search(EMISSION_WORD, match.group(0))):
                # "most units" is not a ranking, but "emit the most" is.
                if not re.search(rf"\b(?:{EMISSION_WORD})\s+(?:the\s+)?$",
                                 self.lower[max(0, match.start() - 20):match.start()] + " "):
                    continue
            metric = self._ranking_metric(match, word)
            if metric is None and word in ("most", "least"):
                metric = "co2"
            low = re.fullmatch(LOWEST, word) is not None
            if metric and self.sort is None:
                self.sort = metric
            self.order = "asc" if low else "desc"
            self.superlative = True
            label = f"by {METRICS[metric][2]}, " if metric else ""
            self.use(match, f"Ranked {label}{'lowest' if low else 'highest'} first")

    def _read_relative(self) -> None:
        patterns = [
            rf"\b(?P<q>{HIGH}|{LOW})(?:[- ](?:emitting|polluting|emission))?"
            rf"\s+(?:(?:levels?|amounts?|quantit(?:y|ies)|volumes?)\s+of\s+)?"
            rf"(?P<m>{METRIC_LIST})(?:\s+(?:{EMISSION_WORD}|levels?|output))?",
            rf"\b(?P<q>{HIGH}|{LOW})[- ]?(?P<e>{EMISSION_WORD})\b",
            rf"\b(?P<m>{METRIC_LIST})(?:\s+(?:{EMISSION_WORD}|levels?|output))?"
            rf"\s+(?:is|are|was|were)\s+(?:very\s+)?(?P<q>high|low)\b",
            r"\b(?P<q>large|big|huge|small)\b",
        ]
        for pattern in patterns:
            for match in self.matches(pattern):
                word = " ".join(match.group("q").split())
                groups = match.groupdict()
                if word == "huge":
                    word = "large"
                metrics = _metrics_in(groups.get("m") or "")
                if not metrics and groups.get("e"):
                    self.note("“Emissions” with no pollutant named is read as CO₂; name SO₂ "
                              "or NOx to search those instead.")
                if not metrics:
                    metrics = ["gross_load"] if word in SIZE_WORDS else ["co2"]
                high = re.fullmatch(HIGH, word) is not None
                for metric in metrics:
                    self.relative.append((metric, high, self.phrase(*match.span())))
                self.use(match)

    def _read_level(self) -> None:
        facility_words = r"\b(?:power\s+)?(?:plants?|facilit(?:y|ies)|stations?|sites?)\b"
        unit_words = r"\b(?:units?|generators?|boilers?|turbines?|engines?|stokers?)\b"
        found = self.matches(facility_words)
        if found and not re.search(unit_words, self.lower):
            self.level = "facility"
            for match in found:
                self.use(match)
            self.result.readings.append(Reading(
                self.phrase(*found[0].span()),
                "One row per facility and year, with its units' totals",
                found[0].start(),
            ))

    def _read_operating(self) -> None:
        for match in self.matches(r"\b(?:idle|did\s+not\s+(?:operate|run)|not\s+(?:operating|running)"
                                  r"|never\s+(?:operated|ran))\b"):
            self.set_range("op_time", None, 0)
            self.use(match, "Did not operate (0 operating hours)")
        for match in self.matches(r"\b(?:operating|active|running|in\s+operation|operated)\b"
                                  r"(?!\s+(?:time|hours))"):
            if "op_time" not in self.ranges:
                self.set_range("op_time", 1, None)
                self.use(match, "Operated during the year (at least 1 hour)")

    def _read_plain_metric(self) -> None:
        for match in self.matches(rf"\b(?P<m>{METRIC})\b"):
            metric = _metric_of(match.group("m"))
            if metric and self.sort is None and not self.relative:
                self.sort = metric
                self.use(match, f"Sorted by {METRICS[metric][2]}")
                return

    def _read_state_codes(self) -> None:
        shouting = self.text.upper() == self.text
        for match in self.matches(r"\b[a-z]{2}\b"):
            original = self.text[match.start():match.end()]
            code = original.upper()
            if code not in STATE_CODES or code in self.states:
                continue
            if match.group(0) in AMBIGUOUS_CODES and (shouting or not original.isupper()):
                continue
            self.states.append(code)
            self.use(match, f"State is {dict(STATES)[code]} ({code})")

    def _read_facility_name(self) -> None:
        """Try runs of leftover words as a facility name: "Barry", "Fort Martin"."""
        if "facility_name" in self.text_filters:
            return
        runs: list[list[re.Match]] = []
        for token in re.finditer(r"[a-z0-9][a-z0-9'’&.-]*", self.work):
            if token.group(0) in STOPWORDS or token.group(0).isdigit():
                runs.append([])
                continue
            if runs and runs[-1] and not self.lower[runs[-1][-1].end():token.start()].strip():
                runs[-1].append(token)
            else:
                runs.append([token])
        for run in sorted((r for r in runs if r), key=len, reverse=True):
            candidates = [run] + ([[t] for t in run] if len(run) > 1 else [])
            for tokens in candidates:
                start, end = tokens[0].start(), tokens[-1].end()
                name = self.phrase(start, end).strip(" .,'’")
                if len(name) < 4:
                    continue
                matches = db.session.scalar(
                    select(func.count(Facility.epa_facility_id))
                    .where(Facility.facility_name.ilike(f"%{name}%"))
                ) or 0
                if 0 < matches <= MAX_NAME_MATCHES:
                    self.set_text("facility_name", name)
                    noun = "facility" if matches == 1 else "facilities"
                    self.use_span(start, end, f"Facility name contains “{name}” "
                                              f"({matches} {noun} on file)")
                    return

    def use_span(self, start: int, end: int, meaning: str) -> None:
        self.work = self.work[:start] + " " * (end - start) + self.work[end:]
        self.result.readings.append(Reading(self.phrase(start, end), meaning, start))

    def _collect_ignored(self) -> None:
        for word in re.findall(r"[a-z0-9₂ₓ][a-z0-9₂ₓ'’&/-]*", self.work):
            word = word.strip("'’-/")
            if word and word not in STOPWORDS and word not in self.result.ignored:
                self.result.ignored.append(word)
        for word, hint in HINTS.items():
            if word in self.result.ignored:
                self.note(hint)

    # --- assembling the parameters -----------------------------------------

    def _finish(self) -> None:
        params: dict[str, str] = {}
        result = self.result

        # Facility totals cannot be ranked by unit-only attributes.
        if self.level == "facility" and (
            self.group_by in ("primary_fuel", "unit_type", "facility")
            or self.sort == "op_time"
            or any(metric == "op_time" for metric, _, _ in self.relative)
        ):
            self.level = "unit"
            result.readings = [r for r in result.readings if not r.meaning.startswith("One row per facility")]
            self.note("That ranking needs one row per unit, so units are listed rather than "
                      "facility totals.")
        if self.level == "facility":
            params["level"] = "facility"

        params.update(self.text_filters)
        if self.facility_ids:
            params["facility_id"] = ",".join(self.facility_ids)
        if self.states:
            params["state"] = ",".join(self.states)
        if self.years:
            params["year"] = ",".join(str(y) for y in sorted(self.years))
        if self.year_min is not None:
            params["year_min"] = str(self.year_min)
        if self.year_max is not None:
            params["year_max"] = str(self.year_max)
        for prefix, (low, high) in self.ranges.items():
            if low is not None:
                params[f"{prefix}_min"] = _plain(low)
            if high is not None:
                params[f"{prefix}_max"] = _plain(high)

        self._apply_relative(params)

        ranked = self.superlative or self.count is not None or self.group_by is not None
        if ranked and self.sort is None:
            self.sort = "co2"
            self.note("No measure was named for the ranking, so it uses CO₂; add one "
                      "such as “by SO2” or “by gross load” to change it.")
        if self.sort:
            params["sort"] = METRICS[self.sort][1]
            params["order"] = self.order or "desc"
            low_first = params["order"] == "asc"
            if low_first and "op_time_min" not in params and "op_time_max" not in params:
                params["op_time_min"] = "1"
                self.derived("Only units that operated, so idle units with zero emissions "
                             "don't fill the list")
        if self.group_by:
            params["group_by"] = self.group_by
            params["per_group"] = str(self.count or 1)
            side = "Top" if params["order"] == "desc" else "Bottom"
            self.derived(f"{side} {self.count or 1} per {GROUP_LABELS[self.group_by]}")
        elif self.count is not None or self.superlative:
            params["limit"] = str(self.count or DEFAULT_TOP_N)
            if self.count is None:
                side = "Top" if params["order"] == "desc" else "Bottom"
                self.derived(f"{side} {DEFAULT_TOP_N} rows (no number was given)")

        # Anything the explorer would reject is dropped, not passed on.
        problems = parse_search(MultiDict(params)).errors
        for problem in problems:
            self.note(problem)
        result.params = params

    def _apply_relative(self, params: dict[str, str]) -> None:
        """Turn "high CO2" into a CO2 minimum at the top quarter of matching rows."""
        base = dict(params)
        for metric, high, phrase in self.relative:
            prefix, _, label, measure = METRICS[metric]
            scope = dict(base)
            if not high and "op_time_min" not in scope and "op_time_max" not in scope:
                scope["op_time_min"] = "1"
                params["op_time_min"] = "1"
            if self.level == "facility":
                scope["level"] = "facility"
            threshold = _quantile(scope, metric, high=high)
            rows = "facility-years" if self.level == "facility" else "unit-years"
            if threshold is None:
                self.note(f"No matching records to measure “{phrase}” against, so it was not "
                          "applied.")
                continue
            if high:
                params[f"{prefix}_min"] = _plain(threshold)
                meaning = (f"{label} ≥ {_shown(threshold)} {measure}: the top quarter of "
                           f"matching {rows}")
            else:
                params[f"{prefix}_max"] = _plain(threshold)
                meaning = (f"{label} ≤ {_shown(threshold)} {measure}: the bottom quarter of "
                           f"matching {rows} that operated")
            self.result.readings.append(Reading(phrase, meaning, self.text.find(phrase)))
            if self.sort is None:
                self.sort = metric
                self.order = "desc" if high else "asc"


# Words people use that have no single matching filter.
HINTS = {
    "cleanest": "“Cleanest” has no single measure; try “lowest SO2”, “lowest NOx” or "
                "“lowest CO2”.",
    "retired": "Retirement is shown on unit pages but is not a search filter.",
    "oldest": "Unit age is shown on unit pages but is not a search filter.",
    "newest": "Unit age is shown on unit pages but is not a search filter.",
    "efficient": "Efficiency is not in the data; heat input and gross load can be compared "
                 "on the unit pages.",
}

_FILTER_NAMES = {
    "so2_control": "SO₂ controls",
    "nox_control": "NOx controls",
    "pm_control": "PM controls",
    "hg_control": "Mercury controls",
}


def _quantile(params: dict[str, str], metric: str, *, high: bool) -> Optional[float]:
    """The value that splits off the top (or bottom) quarter of matching rows."""
    column = METRICS[metric][1]
    search = parse_search(MultiDict(params))
    inner = build_statement(search).order_by(None).subquery()
    if column not in inner.c:
        return None
    total = db.session.scalar(select(func.count()).select_from(inner))
    if not total:
        return None
    value_column = inner.c[column]
    ordered = value_column.desc() if high else value_column.asc()
    offset = min(total - 1, int(total * QUANTILE))
    value = db.session.scalar(select(value_column).order_by(ordered).offset(offset).limit(1))
    if value is None:
        return None
    rounded = _round_sig(float(value), down=high)
    if high and rounded <= 0:
        return None
    return rounded


def interpret(text: str, *, latest_year: Optional[int] = None) -> Interpretation:
    """Read a plain-English description as explorer search parameters."""
    text = " ".join((text or "").split())[:MAX_LENGTH]
    if not text:
        return Interpretation(text="")
    return _Reader(text, latest_year).run()
