"""TRACI assessment engine: potential environmental and health impacts of the
emissions in an analysis.

For each selected unit-year, and each TRACI impact category c::

    mass_kg      = mass_short_tons x 907.18474
    impact_c     = sum over pollutants i of  mass_kg_i x CF_i,c
    intensity_c  = impact_c / gross load (MWh)            (optional)

CF is the stored TRACI characterization factor (see app/services/traci.py),
matched exactly: CO2 -> CARBON DIOXIDE, SO2 -> SULFUR DIOXIDE, NOx ->
NITROGEN OXIDES, all emitted to air. Categories are never added together,
because each has its own equivalent unit (kg CO2 eq, kg SO2 eq, ...).

Missing values
--------------
A unit-year's impact in a category is calculated only when every pollutant
that category uses was reported. Acidification needs both SO2 and NOx, so a
unit-year with NOx but no reported SO2 has no acidification value rather than
an underestimate, and totals say how many unit-years were left out. An
intensity is missing when the gross load is missing or zero.

These are potential impacts for comparing sources, not measured harm.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

import numpy as np
import pandas as pd

from app.glossary import traci_category
from app.models import TraciFactor
from app.services.analysis import Variable, safe_ratio
from app.services.traci import factors_for

# The exact definition of the international avoirdupois short ton (2,000 lb).
SHORT_TON_KG = 907.18474

# Grouping choices for impact totals: parameter -> (column, label).
IMPACT_GROUPS = {
    "state": ("state_code", "State"),
    "primary_fuel": ("primary_fuel", "Primary fuel"),
    "unit_type": ("unit_type", "Unit type"),
    "year": ("reporting_year", "Year"),
    "facility": ("facility_id", "Facility"),
}

DISCLAIMER = (
    "TRACI results are potential impacts for comparing emission sources, not measured "
    "harm to people or places."
)


@dataclass(frozen=True)
class Factor:
    """One characterization factor, as used in a calculation."""

    campd_metric: str
    pollutant_name: str
    value: float
    unit: str
    source: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "campd_metric": self.campd_metric,
            "pollutant_name": self.pollutant_name,
            "factor_value": self.value,
            "factor_unit": self.unit,
            "source": self.source,
        }


@dataclass(frozen=True)
class ImpactCategory:
    """A TRACI impact category and the factors that feed it."""

    category: str  # TRACI's heading, e.g. "Acidification"
    version: str
    factors: tuple[Factor, ...]

    @property
    def slug(self) -> str:
        return re.sub(r"[^a-z0-9]+", "_", self.category.lower()).strip("_")

    @property
    def name(self) -> str:
        """The column holding this impact, e.g. ``traci_acidification``."""
        return f"traci_{self.slug}"

    @property
    def intensity_name(self) -> str:
        return f"{self.name}_per_mwh"

    @property
    def label(self) -> str:
        """'Acidification (acid rain)': the category with its everyday meaning."""
        return traci_category(self.category)

    @property
    def short_label(self) -> str:
        return self.label.split(" (")[0]

    @property
    def unit(self) -> str:
        """The equivalent unit of the result, e.g. 'kg SO2 eq'.

        TRACI writes factor units as '<equivalent> / kg substance'; multiplying
        by kilograms emitted leaves kilograms of the equivalent. EPA's sheet
        writes the particulate unit as 'PM2.5 eq', which is kg PM2.5 eq.
        """
        reference = self.factors[0].unit.split("/")[0].strip()
        return reference if reference.lower().startswith("kg ") else f"kg {reference}"

    @property
    def metrics(self) -> list[str]:
        return [f.campd_metric for f in self.factors]

    @property
    def formula(self) -> str:
        """e.g. 'SO2 kg x 1 + NOx kg x 0.7'."""
        terms = [f"{_POLLUTANT_SHORT[f.campd_metric]} kg × {_factor_text(f.value)}"
                 for f in self.factors]
        return " + ".join(terms)

    def variables(self) -> tuple[Variable, Variable]:
        """The impact and its intensity, as analysis variables."""
        total = Variable(
            self.name, self.label, self.unit,
            f"TRACI {self.version} potential impact: {self.formula}, with each mass "
            f"converted from short tons to kg (× {SHORT_TON_KG}).",
            "impact",
        )
        intensity = Variable(
            self.intensity_name, f"{self.short_label} per MWh", f"{self.unit}/MWh",
            f"{self.label} divided by gross load (electricity generated).",
            "impact", self.name, "gross_load",
        )
        return total, intensity

    def as_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "name": self.name,
            "intensity_name": self.intensity_name,
            "label": self.label,
            "unit": self.unit,
            "intensity_unit": f"{self.unit}/MWh",
            "formula": self.formula,
            "factors": [f.as_dict() for f in self.factors],
        }


_POLLUTANT_SHORT = {"co2_mass": "CO₂", "so2_mass": "SO₂", "nox_mass": "NOx"}


def _factor_text(value: float) -> str:
    return f"{value:.6g}"


def categories(version: Optional[str] = None,
               factors: Optional[list[TraciFactor]] = None) -> list[ImpactCategory]:
    """The impact categories of one stored TRACI version (the latest if none)."""
    factors = factors if factors is not None else factors_for(version)
    by_category: dict[str, list[Factor]] = {}
    for f in factors:
        if f.medium != "air":
            continue
        by_category.setdefault(f.impact_category, []).append(
            Factor(f.campd_metric, f.pollutant_name, float(f.factor_value), f.factor_unit, f.source)
        )
    order = list(_POLLUTANT_SHORT)
    return [
        ImpactCategory(
            category,
            factors[0].version,
            tuple(sorted(items, key=lambda f: order.index(f.campd_metric))),
        )
        for category, items in sorted(by_category.items())
    ]


def impact_variables(cats: list[ImpactCategory]) -> dict[str, Variable]:
    """Every impact and impact intensity, as analysis variables by name."""
    out: dict[str, Variable] = {}
    for cat in cats:
        for var in cat.variables():
            out[var.name] = var
    return out


# ---------------------------------------------------------------------------
# Calculations
# ---------------------------------------------------------------------------


def to_kg(short_tons: Any) -> pd.Series:
    """Short tons to kilograms; missing stays missing."""
    return pd.to_numeric(pd.Series(short_tons), errors="coerce").astype("float64") * SHORT_TON_KG


def contributions(units: pd.DataFrame, cat: ImpactCategory) -> pd.DataFrame:
    """Each pollutant's part of a category's impact for every unit-year
    (mass_kg x CF), missing where that pollutant was not reported."""
    return pd.DataFrame(
        {f.campd_metric: to_kg(units[f.campd_metric]).to_numpy() * f.value for f in cat.factors},
        index=units.index,
    )


def category_impact(units: pd.DataFrame, cat: ImpactCategory) -> pd.Series:
    """impact_c for every unit-year: missing unless every pollutant was reported."""
    return contributions(units, cat).sum(axis=1, min_count=len(cat.factors))


def add_impacts(units: pd.DataFrame, cats: list[ImpactCategory]) -> pd.DataFrame:
    """Add each category's impact and impact per MWh to unit-year rows."""
    frame = units.copy()
    for cat in cats:
        frame[cat.name] = category_impact(frame, cat)
        frame[cat.intensity_name] = safe_ratio(frame[cat.name], frame["gross_load"])
    return frame


def _total(values: pd.Series) -> Optional[float]:
    total = values.sum(min_count=1)
    return None if pd.isna(total) else float(total)


def _intensity(impact: pd.Series, gross_load: pd.Series) -> tuple[Optional[float], float]:
    """Total impact / total gross load over the unit-years that have both, and
    the gross load used."""
    both = impact.notna() & gross_load.notna()
    load = float(gross_load[both].sum())
    if not both.any() or load <= 0:
        return None, load
    return float(impact[both].sum()) / load, load


def summarize_category(units: pd.DataFrame, cat: ImpactCategory) -> dict[str, Any]:
    """Totals of one category over unit-years that already carry impacts."""
    impact = units[cat.name] if cat.name in units else category_impact(units, cat)
    used = impact.notna()
    parts = contributions(units, cat)[used]
    total = _total(impact)
    intensity, load = _intensity(impact, units["gross_load"])
    pollutants = []
    for f in cat.factors:
        mass_tons = units.loc[used, f.campd_metric]
        part = _total(parts[f.campd_metric])
        pollutants.append({
            **f.as_dict(),
            "mass_short_tons": _total(mass_tons),
            "mass_kg": None if _total(mass_tons) is None else _total(mass_tons) * SHORT_TON_KG,
            "impact": part,
            "share_pct": (100 * part / total) if part is not None and total else None,
        })
    return {
        **cat.as_dict(),
        "total": total,
        "intensity": intensity,
        "gross_load_used": load,
        "records_used": int(used.sum()),
        "records_missing": int((~used).sum()),
        "pollutants": pollutants,
    }


def impact_summary(units: pd.DataFrame, cats: list[ImpactCategory]) -> list[dict[str, Any]]:
    """One summary per category for every selected unit-year."""
    return [summarize_category(units, cat) for cat in cats]


def group_labels(units: pd.DataFrame, column: str) -> dict[Any, str]:
    """Readable names for group keys (facility IDs get their names)."""
    if column != "facility_id":
        return {}
    names = units.drop_duplicates("facility_id").set_index("facility_id")
    return {
        key: f"{row.facility_name} ({row.state_code})" for key, row in names.iterrows()
    }


def grouped_impacts(
    units: pd.DataFrame, cats: list[ImpactCategory], group: str
) -> list[dict[str, Any]]:
    """Impact totals and intensities for each group, ranked within each
    category (1 = largest potential impact)."""
    column = IMPACT_GROUPS[group][0]
    keys = units[column]
    keys = keys.astype(object).where(keys.notna() & (keys.astype(str).str.strip() != ""),
                                     "(not reported)")
    work = pd.DataFrame({"_group": keys, "gross_load": units["gross_load"]}, index=units.index)
    for cat in cats:
        impact = units[cat.name]
        both = impact.notna() & units["gross_load"].notna()
        work[cat.name] = impact
        work[f"{cat.name}__num"] = impact.where(both)
        work[f"{cat.name}__den"] = units["gross_load"].where(both)
        work[f"{cat.name}__missing"] = impact.isna()
    grouped = work.groupby("_group", sort=False)
    sums = grouped.sum(min_count=1)
    missing = grouped[[f"{cat.name}__missing" for cat in cats]].sum()
    sizes = grouped.size()
    labels = group_labels(units, column)

    rows = []
    for key in sums.index:
        rows.append({
            "group": key.item() if isinstance(key, np.generic) else key,
            "label": labels.get(key, str(key)),
            "records": int(sizes[key]),
            "gross_load": _float_or_none(sums.at[key, "gross_load"]),
            "impacts": {
                cat.name: {
                    "total": _float_or_none(sums.at[key, cat.name]),
                    "intensity": _ratio_or_none(sums.at[key, f"{cat.name}__num"],
                                                sums.at[key, f"{cat.name}__den"]),
                    "records_missing": int(missing.at[key, f"{cat.name}__missing"]),
                }
                for cat in cats
            },
        })
    for cat in cats:
        totals = pd.Series([r["impacts"][cat.name]["total"] for r in rows], dtype="float64")
        ranks = totals.rank(ascending=False, method="min")
        for r, rank in zip(rows, ranks):
            r["impacts"][cat.name]["rank"] = None if pd.isna(rank) else int(rank)
    return rows


def _float_or_none(value: Any) -> Optional[float]:
    return None if pd.isna(value) else float(value)


def _ratio_or_none(numerator: Any, denominator: Any) -> Optional[float]:
    if pd.isna(numerator) or pd.isna(denominator) or denominator <= 0:
        return None
    return float(numerator) / float(denominator)


def worked_example(units: pd.DataFrame, cats: list[ImpactCategory]) -> Optional[dict[str, Any]]:
    """The calculation spelled out for the largest unit-year that reported
    every pollutant, so a result can be checked by hand."""
    metrics = sorted({m for cat in cats for m in cat.metrics})
    if not cats or units.empty:
        return None
    complete = units.dropna(subset=metrics)
    if complete.empty:
        return None
    row = complete.loc[complete["co2_mass" if "co2_mass" in metrics else metrics[0]].idxmax()]
    masses = {
        m: {"short_tons": float(row[m]), "kg": float(row[m]) * SHORT_TON_KG} for m in metrics
    }
    steps = []
    for cat in cats:
        terms = [
            {"pollutant": _POLLUTANT_SHORT[f.campd_metric], "kg": masses[f.campd_metric]["kg"],
             "factor": f.value, "result": masses[f.campd_metric]["kg"] * f.value}
            for f in cat.factors
        ]
        total = sum(t["result"] for t in terms)
        load = row["gross_load"]
        steps.append({
            "category": cat.category,
            "label": cat.label,
            "unit": cat.unit,
            "terms": terms,
            "total": total,
            "intensity": float(total / load) if pd.notna(load) and load > 0 else None,
        })
    return {
        "facility_id": int(row["facility_id"]),
        "facility_name": row["facility_name"],
        "state_code": row["state_code"],
        "epa_unit_id": row["epa_unit_id"],
        "unit_key": int(row["unit_key"]),
        "reporting_year": int(row["reporting_year"]),
        "gross_load": None if pd.isna(row["gross_load"]) else float(row["gross_load"]),
        "masses": masses,
        "steps": steps,
    }


# ---------------------------------------------------------------------------
# Page and endpoint options
# ---------------------------------------------------------------------------

DEFAULT_GROUP = "state"
DEFAULT_SORT = "traci_global_climate"
LIMITS = (10, 25, 50, 100)
DEFAULT_LIMIT = 25


def parse_impact_options(args, cats: list[ImpactCategory]) -> dict[str, Any]:
    """``group_by``, ``metric`` (the impact or intensity groups are ranked by)
    and ``limit``."""
    errors: list[str] = []
    group = (args.get("group_by") or "").strip() or DEFAULT_GROUP
    if group not in IMPACT_GROUPS:
        errors.append(f"Impacts cannot be grouped by '{group}', so they are grouped by state.")
        group = DEFAULT_GROUP
    names = [n for cat in cats for n in (cat.name, cat.intensity_name)]
    metric = (args.get("metric") or "").strip()
    if metric and metric not in names:
        errors.append(f"'{metric}' is not a TRACI impact, so groups are ranked by the default.")
        metric = ""
    if not metric:
        metric = DEFAULT_SORT if DEFAULT_SORT in names else (names[0] if names else "")
    raw_limit = (args.get("limit") or "").strip().lower()
    if raw_limit == "all":
        limit = None
    else:
        limit = int(raw_limit) if raw_limit.isdigit() and int(raw_limit) in LIMITS else DEFAULT_LIMIT
    return {"group": group, "metric": metric, "limit": limit, "errors": errors}


def sorted_groups(rows: list[dict[str, Any]], metric: str, limit: Optional[int]) -> list[dict[str, Any]]:
    """Groups by one impact (or impact per MWh), largest first, missing last."""
    is_intensity = metric.endswith("_per_mwh")
    name = metric[: -len("_per_mwh")] if is_intensity else metric
    field = "intensity" if is_intensity else "total"

    def key(row):
        value = row["impacts"].get(name, {}).get(field)
        return (value is None, -(value or 0), str(row["label"]))

    ordered = sorted(rows, key=key)
    return ordered if limit is None else ordered[:limit]
