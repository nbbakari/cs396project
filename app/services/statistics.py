"""Statistical engine: descriptive statistics, group comparisons and rankings
for the records an analysis selects.

Descriptive statistics
----------------------
For every numeric variable (measurements, derived indicators and TRACI
impacts), over the values that are present -- a missing value is never
counted as 0:

    count, missing           values present and missing
    mean, std                mean and *sample* standard deviation (n - 1)
    min, q1, median, q3, max quartiles, with linear interpolation between
                             values (numpy/pandas "linear", Excel
                             PERCENTILE.INC, R type 7)
    iqr                      q3 - q1
    p5, p10, p90, p95        more percentiles, same method
    sem, ci95_low/high       standard error of the mean and its 95%
                             confidence interval (Student's t, SciPy)
    skewness                 adjusted Fisher-Pearson skewness (SciPy, bias
                             corrected); positive = a long tail of high values
    outliers                 values outside q1 - 1.5 iqr .. q3 + 1.5 iqr
                             (Tukey's fences, the box-plot rule)

Statistics can be grouped by year, state, fuel or unit type.

Comparisons
-----------
Groups (facilities, states, fuels, unit types or years) are compared on one
variable. Each group's value is aggregated from its unit-years:

    totals (mass, load, heat input, TRACI impacts)    sum
    rates (intensities, heat rate, impact per MWh)    sum of numerators /
                                                      sum of denominators, over
                                                      unit-years with both
    operating time                                    mean per unit-year

A rate is never the average of unit rates, which would let a small unit
count as much as a large one. Each group gets a rank (1 = highest), a
percentile rank, a z-score, and its absolute and percentage difference from
a reference: a chosen group, or by default the average group (for totals)
or all selected records combined (for rates and averages). A Kruskal-Wallis
H test (SciPy) says whether the unit-year values differ between the groups
by more than chance would explain.
"""

from __future__ import annotations

import math
import warnings
from typing import Any, Optional

import numpy as np
import pandas as pd
from scipy import stats as scipy_stats

from app.services.analysis import (
    DERIVED,
    HOURS_IN_YEAR,
    MEASURED,
    Analysis,
    Variable,
    add_indicators,
    facility_frame,
    hours_in_year,
    load_units,
    numeric_variables,
)
from app.services.impacts import (
    ImpactCategory,
    add_impacts,
    categories,
    group_labels,
    impact_variables,
)

# key -> (label, explanation), in display order.
STATISTICS: dict[str, tuple[str, str]] = {
    "count": ("Count", "values present"),
    "missing": ("Missing", "values not reported, or undefined"),
    "mean": ("Mean", "the average"),
    "std": ("Std dev", "sample standard deviation: how far values typically are from the mean"),
    "min": ("Min", "smallest value"),
    "q1": ("Q1", "25th percentile: a quarter of values are lower"),
    "median": ("Median", "middle value: half are lower, half higher"),
    "q3": ("Q3", "75th percentile: three quarters of values are lower"),
    "max": ("Max", "largest value"),
    "iqr": ("IQR", "interquartile range, Q3 − Q1: the spread of the middle half"),
    "p5": ("P5", "5th percentile"),
    "p10": ("P10", "10th percentile"),
    "p90": ("P90", "90th percentile"),
    "p95": ("P95", "95th percentile"),
    "sem": ("SE of mean", "standard error of the mean"),
    "ci95_low": ("95% CI low", "lower end of the 95% confidence interval of the mean"),
    "ci95_high": ("95% CI high", "upper end of the 95% confidence interval of the mean"),
    "skewness": ("Skewness", "above 0: a long tail of high values; below 0: of low values"),
    "outliers": ("Outliers", "values beyond 1.5 × IQR from the quartiles (box-plot rule)"),
}
# Shown in the page's main table; the rest appear with "more statistics".
MAIN_STATISTICS = ("count", "missing", "mean", "std", "min", "q1", "median", "q3", "max", "iqr")

PERCENTILES = {"p5": 0.05, "p10": 0.10, "q1": 0.25, "median": 0.50, "q3": 0.75, "p90": 0.90, "p95": 0.95}

# Grouping for descriptive statistics: parameter -> (column, label).
STAT_GROUPS = {
    "year": ("reporting_year", "Year"),
    "state": ("state_code", "State"),
    "primary_fuel": ("primary_fuel", "Primary fuel"),
    "unit_type": ("unit_type", "Unit type"),
}
FACILITY_STAT_GROUPS = ("year", "state")

# Comparison dimensions: parameter -> (column, singular, plural).
DIMENSIONS = {
    "state": ("state_code", "state", "states"),
    "primary_fuel": ("primary_fuel", "fuel", "fuels"),
    "facility": ("facility_id", "facility", "facilities"),
    "unit_type": ("unit_type", "unit type", "unit types"),
    "year": ("reporting_year", "year", "years"),
}

BLANK_GROUP = "(not reported)"
SIGNIFICANCE = 0.05


def _float(value: Any) -> Optional[float]:
    if value is None:
        return None
    value = float(value)
    return None if math.isnan(value) or math.isinf(value) else value


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_selection(
    analysis: Analysis, version: Optional[str] = None
) -> tuple[pd.DataFrame, list[ImpactCategory]]:
    """The unit-years an analysis selects, with derived indicators and the
    TRACI impacts of the stored factors (latest version unless given)."""
    cats = categories(version)
    units = add_impacts(add_indicators(load_units(analysis.search)), cats)
    return units, cats


def all_variables(level: str, cats: list[ImpactCategory]) -> dict[str, Variable]:
    """Measurements, derived indicators and TRACI impacts available at a level."""
    return {**numeric_variables(level), **impact_variables(cats)}


def level_frame(units: pd.DataFrame, level: str, cats: list[ImpactCategory]) -> pd.DataFrame:
    """Unit-year rows, or facility-year totals with impacts added up."""
    if level != "facility":
        return units
    return facility_frame(units, extra=list(impact_variables(cats).values()))


# ---------------------------------------------------------------------------
# Descriptive statistics
# ---------------------------------------------------------------------------


def describe_values(values: Any) -> dict[str, Any]:
    """Descriptive statistics of the values present (see the module docstring)."""
    series = pd.to_numeric(pd.Series(values), errors="coerce").astype("float64")
    series = series.replace([np.inf, -np.inf], np.nan)
    present = series.dropna()
    n = int(len(present))
    out: dict[str, Any] = {key: None for key in STATISTICS}
    out.update(count=n, missing=int(len(series) - n))
    if n == 0:
        out["outliers"] = 0
        return out

    quantiles = present.quantile(list(PERCENTILES.values()), interpolation="linear")
    for key, q in PERCENTILES.items():
        out[key] = _float(quantiles.loc[q])
    out["mean"] = _float(present.mean())
    out["min"] = _float(present.min())
    out["max"] = _float(present.max())
    out["iqr"] = out["q3"] - out["q1"]
    low, high = out["q1"] - 1.5 * out["iqr"], out["q3"] + 1.5 * out["iqr"]
    out["outliers"] = int(((present < low) | (present > high)).sum())

    if n >= 2:
        std = float(present.std(ddof=1))
        out["std"] = std
        out["sem"] = std / math.sqrt(n)
        t = float(scipy_stats.t.ppf(0.975, df=n - 1))
        out["ci95_low"] = out["mean"] - t * out["sem"]
        out["ci95_high"] = out["mean"] + t * out["sem"]
        if n >= 3 and std > 0:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                out["skewness"] = _float(scipy_stats.skew(present.to_numpy(), bias=False))
    return out


def describe(frame: pd.DataFrame, variables: dict[str, Variable]) -> list[dict[str, Any]]:
    """Statistics of every variable over all rows."""
    return [
        {**var.as_dict(), "statistics": describe_values(
            frame[name] if name in frame else pd.Series(np.nan, index=frame.index))}
        for name, var in variables.items()
    ]


def _group_keys(frame: pd.DataFrame, column: str) -> pd.Series:
    keys = frame[column]
    blank = keys.isna() | (keys.astype(str).str.strip() == "")
    return keys.astype(object).where(~blank, BLANK_GROUP)


def _plain(key: Any) -> Any:
    return key.item() if isinstance(key, np.generic) else key


def describe_grouped(
    frame: pd.DataFrame, variables: dict[str, Variable], group: str
) -> list[dict[str, Any]]:
    """Statistics of each variable within each group, groups in their natural order."""
    column = STAT_GROUPS[group][0]
    keys = _group_keys(frame, column)
    out = []
    for key, part in frame.groupby(keys, sort=False):
        out.append({
            "group": _plain(key),
            "records": int(len(part)),
            "statistics": {
                name: describe_values(part[name] if name in part else [])
                for name in variables
            },
        })
    return sorted(out, key=_natural_order)


def _natural_order(group: dict[str, Any]) -> tuple:
    """Years in order, names alphabetically, "(not reported)" last."""
    key = group["group"]
    return (key == BLANK_GROUP, key if isinstance(key, (int, float)) else str(key).lower())


# ---------------------------------------------------------------------------
# Comparisons and rankings
# ---------------------------------------------------------------------------


def aggregation(var: Variable) -> str:
    """How a group's value is formed from its unit-years: sum, ratio or mean."""
    if var.numerator and var.denominator:
        return "ratio"
    if var.name == "operating_time":
        return "mean"
    return "sum"


# Read after "Each state's value is the ...".
AGGREGATION_TEXT = {
    "sum": "total of its unit-years",
    "ratio": "total {num} ÷ total {den}, over its unit-years that reported both",
    "mean": "average of its unit-years",
}


def _label_of(name: Optional[str]) -> str:
    if not name:
        return ""
    if name == HOURS_IN_YEAR:
        return "hours in the year"
    if name.startswith("traci_"):
        return "potential impact"
    var = MEASURED.get(name) or DERIVED.get(name)
    return var.label.lower().replace("co2", "CO₂").replace("so2", "SO₂").replace("nox", "NOx") if var else name


def _denominator(units: pd.DataFrame, var: Variable) -> pd.Series:
    if var.denominator == HOURS_IN_YEAR:
        return hours_in_year(units["reporting_year"])
    return units[var.denominator]


def _aggregate(units: pd.DataFrame, var: Variable, how: str) -> Optional[float]:
    if how == "ratio":
        num = units[var.numerator]
        den = _denominator(units, var)
        both = num.notna() & den.notna()
        total_den = float(den[both].sum())
        return float(num[both].sum()) / total_den if both.any() and total_den > 0 else None
    values = units[var.name].dropna() if var.name in units else pd.Series(dtype="float64")
    if values.empty:
        return None
    return float(values.mean() if how == "mean" else values.sum())


def kruskal_test(samples: list[np.ndarray], plural: str) -> dict[str, Any]:
    """Kruskal-Wallis H test of whether unit-year values differ between groups.

    Rank-based, so it suits skewed emissions data with a few very large
    sources. Unit-years of one unit in different years are not independent,
    so the p-value is a guide, not a proof.
    """
    samples = [s for s in samples if len(s)]
    n = sum(len(s) for s in samples)
    result: dict[str, Any] = {"name": "Kruskal–Wallis H test", "groups_tested": len(samples),
                              "observations": n}
    if len(samples) < 2 or n <= len(samples):
        return {**result, "available": False,
                "interpretation": f"At least two {plural} with values are needed for a test."}
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            h, p = (float(x) for x in scipy_stats.kruskal(*samples))
    except ValueError:
        h = p = float("nan")
    if math.isnan(h) or math.isnan(p):
        return {**result, "available": False,
                "interpretation": "Every value is the same, so there is nothing to test."}
    epsilon = h / (n - 1)
    size = ("negligible" if epsilon < 0.01 else "small" if epsilon < 0.08
            else "medium" if epsilon < 0.26 else "large")
    significant = p < SIGNIFICANCE
    if significant:
        text = (f"The {plural} differ by more than chance alone would explain "
                f"(p {'< 0.001' if p < 0.001 else f'= {p:.3f}'}), with a {size} effect "
                f"(ε² = {epsilon:.2f}).")
    else:
        text = (f"The differences between the {plural} could be due to chance "
                f"(p = {p:.3f}, not below {SIGNIFICANCE}).")
    return {**result, "available": True, "h": h, "df": len(samples) - 1, "p_value": p,
            "epsilon_squared": epsilon, "effect_size": size, "significant": significant,
            "alpha": SIGNIFICANCE, "interpretation": text}


def compare_groups(
    units: pd.DataFrame,
    var: Variable,
    by: str,
    baseline: Optional[str] = None,
) -> dict[str, Any]:
    """Compare the groups of one dimension on one variable (see module docstring)."""
    column, singular, plural = DIMENSIONS[by]
    how = aggregation(var)
    keys = _group_keys(units, column)
    labels = group_labels(units, column)
    groups = []
    samples = []
    for key, part in units.groupby(keys, sort=False):
        values = part[var.name].dropna() if var.name in part else pd.Series(dtype="float64")
        samples.append(values.to_numpy(dtype="float64"))
        groups.append({
            "group": _plain(key),
            "label": labels.get(key, str(_plain(key))),
            "records": int(len(part)),
            "units": int(part["unit_key"].nunique()),
            "facilities": int(part["facility_id"].nunique()),
            "value": _aggregate(part, var, how),
            "median": _float(values.median()) if len(values) else None,
            "values_present": int(len(values)),
        })

    values = pd.Series([g["value"] for g in groups], dtype="float64")
    present = values.dropna()
    ranks = values.rank(ascending=False, method="min")
    percentiles = values.rank(ascending=True, method="max", pct=True) * 100
    if len(present) >= 2 and present.std(ddof=1) > 0:
        z = pd.Series(scipy_stats.zscore(present.to_numpy(), ddof=1), index=present.index)
    else:
        z = pd.Series(dtype="float64")
    grand_total = float(present.sum()) if how == "sum" and len(present) else None

    reference, errors = _reference(units, var, how, groups, present, baseline, singular, plural)
    for i, g in enumerate(groups):
        value = g["value"]
        g["rank"] = None if pd.isna(ranks[i]) else int(ranks[i])
        g["percentile"] = None if pd.isna(percentiles[i]) else float(percentiles[i])
        g["z_score"] = _float(z.get(i)) if i in z.index else None
        ref = reference["value"]
        g["difference"] = value - ref if value is not None and ref is not None else None
        g["pct_difference"] = (100 * g["difference"] / ref
                               if g["difference"] is not None and ref else None)
        g["share_pct"] = (100 * value / grand_total
                          if how == "sum" and value is not None and grand_total else None)
        g["is_reference"] = reference.get("group") is not None and g["group"] == reference["group"]
    groups.sort(key=lambda g: (g["rank"] is None, g["rank"] or 0, str(g["label"])))

    text = AGGREGATION_TEXT[how].format(num=_label_of(var.numerator), den=_label_of(var.denominator))
    return {
        "by": by,
        "dimension": singular,
        "dimension_plural": plural,
        "metric": var.as_dict(),
        "aggregation": how,
        "aggregation_text": text,
        "reference": reference,
        "group_count": len(groups),
        "groups_with_value": int(len(present)),
        "total": grand_total,
        "groups": groups,
        "test": kruskal_test(samples, plural),
        "errors": errors,
    }


def _reference(units, var, how, groups, present, baseline, singular, plural):
    """The value every group is compared with, and any problem with ``baseline``."""
    errors: list[str] = []
    if baseline:
        wanted = str(baseline).strip().lower()
        match = next((g for g in groups if str(g["group"]).lower() == wanted
                      or str(g["label"]).lower() == wanted), None)
        if match is not None and match["value"] is not None:
            return {"kind": "group", "group": match["group"], "label": match["label"],
                    "value": match["value"]}, errors
        errors.append(f"'{baseline}' is not a {singular} with a value in this selection, "
                      "so the default reference was used.")
    if how == "sum":
        value = float(present.mean()) if len(present) else None
        label = f"the average {singular} ({len(present)} {plural})"
        return {"kind": "average", "group": None, "label": label, "value": value}, errors
    value = _aggregate(units, var, how)
    return {"kind": "overall", "group": None, "label": "all selected records combined",
            "value": value}, errors


# ---------------------------------------------------------------------------
# Page and endpoint options
# ---------------------------------------------------------------------------

DEFAULT_VARIABLE = "co2_intensity"
DEFAULT_DIMENSION = "state"
LIMITS = (10, 25, 50, 100)
DEFAULT_LIMIT = 25


def _listed(args, name: str) -> list[str]:
    return [part.strip() for raw in args.getlist(name) for part in str(raw).split(",") if part.strip()]


def parse_statistics_options(
    args, level: str, variables: dict[str, Variable]
) -> dict[str, Any]:
    """``group_by``, ``variable`` (the one shown per group) and ``variables``
    (the ones to describe; all by default). Problems go to ``errors``."""
    errors: list[str] = []
    allowed = FACILITY_STAT_GROUPS if level == "facility" else tuple(STAT_GROUPS)
    group = (args.get("group_by") or "").strip() or None
    if group and group not in allowed:
        errors.append(f"Statistics cannot be grouped by '{group}' at the {level} level, "
                      "so they are not grouped.")
        group = None
    variable = (args.get("variable") or "").strip() or DEFAULT_VARIABLE
    if variable not in variables:
        errors.append(f"'{variable}' is not a variable at the {level} level; "
                      f"showed {DEFAULT_VARIABLE} instead.")
        variable = DEFAULT_VARIABLE
    chosen = [name for name in _listed(args, "variables") if name in variables]
    errors += [f"'{name}' is not a variable at the {level} level, so it was left out."
               for name in _listed(args, "variables") if name not in variables]
    return {
        "group": group,
        "groups": {key: STAT_GROUPS[key][1] for key in allowed},
        "variable": variable,
        "variables": {name: variables[name] for name in chosen} if chosen else variables,
        "errors": errors,
    }


def parse_compare_options(args, variables: dict[str, Variable]) -> dict[str, Any]:
    """``by``, ``metric``, ``baseline``, ``order`` and ``limit`` for a comparison."""
    errors: list[str] = []
    by = (args.get("by") or "").strip() or DEFAULT_DIMENSION
    if by not in DIMENSIONS:
        errors.append(f"Cannot compare by '{by}'; compared {DIMENSIONS[DEFAULT_DIMENSION][2]}.")
        by = DEFAULT_DIMENSION
    metric = (args.get("metric") or "").strip() or DEFAULT_VARIABLE
    if metric not in variables:
        errors.append(f"'{metric}' is not a variable; compared {DEFAULT_VARIABLE} instead.")
        metric = DEFAULT_VARIABLE
    raw_limit = (args.get("limit") or "").strip().lower()
    if raw_limit == "all":
        limit = None
    else:
        limit = int(raw_limit) if raw_limit.isdigit() and int(raw_limit) in LIMITS else DEFAULT_LIMIT
    return {
        "by": by,
        "metric": metric,
        "baseline": (args.get("baseline") or "").strip()[:100] or None,
        "order": "asc" if (args.get("order") or "").lower() == "asc" else "desc",
        "limit": limit,
        "errors": errors,
    }


def shown_groups(groups: list[dict[str, Any]], order: str, limit: Optional[int]) -> list[dict[str, Any]]:
    """Groups highest first (or lowest first), groups without a value last."""
    with_value = [g for g in groups if g["value"] is not None]
    without = [g for g in groups if g["value"] is None]
    if order == "asc":
        with_value = list(reversed(with_value))
    ordered = with_value + without
    return ordered if limit is None else ordered[:limit]
