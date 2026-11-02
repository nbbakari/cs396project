"""EPA TRACI characterization factors for the pollutants CAMPD reports.

TRACI (the Tool for the Reduction and Assessment of Chemical and other
environmental Impacts) gives, for each substance, its potential contribution
per kilogram emitted to impact categories such as global climate change and
acidification. Only factors that match a CAMPD pollutant emitted to air are
used:

    CAMPD column   TRACI substance     CAS
    co2_mass       CARBON DIOXIDE      124-38-9
    so2_mass       SULFUR DIOXIDE      7446-09-5
    nox_mass       NITROGEN OXIDES     (TRACI lists it without a CAS number)

Substances are matched on their exact TRACI name, never approximately: NOx is
not matched to NITROGEN DIOXIDE or NITRIC OXIDE, which TRACI lists separately
with different factors.

Factors are read from EPA's workbook (traci_2_2.xlsx, sheet "Substances"),
whose air-emission columns are headed "<Category> Air (<unit>)", e.g.
"Acidification Air (kg SO2 eq / kg substance)". The database migration seeds
the TRACI 2.2 factors; ``flask traci load`` re-imports them from a workbook.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

import openpyxl
from sqlalchemy import select

from app import db
from app.glossary import TRACI_CATEGORIES
from app.models import TraciFactor

SOURCE_URL = "https://www.epa.gov/system/files/documents/2024-01/traci_2_2.xlsx"
SUBSTANCE_SHEET = "Substances"

# CAMPD column -> (exact TRACI substance name, CAS number without dashes or None)
CAMPD_SUBSTANCES: dict[str, tuple[str, Optional[str]]] = {
    "co2_mass": ("CARBON DIOXIDE", "124389"),
    "so2_mass": ("SULFUR DIOXIDE", "7446095"),
    "nox_mass": ("NITROGEN OXIDES", None),
}

# TRACI's category headings for display, e.g. "Acidification (acid rain)".
CATEGORY_LABELS = TRACI_CATEGORIES

_AIR_COLUMN = re.compile(r"^(?P<category>.+?) Air \((?P<unit>[^)]+)\)$")
_VERSION = re.compile(r"TRACI[\s_]+(\d+(?:[._]\d+)*)", re.IGNORECASE)


class TraciError(ValueError):
    """A workbook could not be read as TRACI factors; safe to show to users."""


def _number(value: Any) -> Optional[float]:
    """A factor cell as a float; TRACI writes "n/a" where no factor exists."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _cas_digits(value: Any) -> Optional[str]:
    digits = re.sub(r"\D", "", str(value or ""))
    return digits or None


def workbook_version(workbook: openpyxl.Workbook) -> Optional[str]:
    """The TRACI version named in the workbook's title cell or first sheet."""
    first = workbook.worksheets[0]
    title = next(first.iter_rows(min_row=1, max_row=1, values_only=True), (None,))[0]
    for text in (title, first.title):
        match = _VERSION.search(str(text or ""))
        if match:
            return match.group(1).replace("_", ".")
    return None


def read_workbook(path: str | Path, version: Optional[str] = None) -> list[dict[str, Any]]:
    """Every air-emission factor for CO2, SO2 and NOx in an EPA TRACI workbook.

    Raises TraciError if the workbook is not laid out as EPA publishes it, if
    a substance is missing, or if its CAS number does not match.
    """
    path = Path(path)
    try:
        workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 - openpyxl raises many types on bad files
        raise TraciError(f"Could not open {path.name} as an Excel workbook: {exc}") from exc
    try:
        version = version or workbook_version(workbook)
        if not version:
            raise TraciError("The workbook does not name its TRACI version; pass --version.")
        if SUBSTANCE_SHEET not in workbook.sheetnames:
            raise TraciError(f'The workbook has no "{SUBSTANCE_SHEET}" sheet.')

        rows = workbook[SUBSTANCE_SHEET].iter_rows(values_only=True)
        header = [str(cell).strip() if cell is not None else "" for cell in next(rows, ())]
        try:
            name_column = header.index("Substance Name")
            cas_column = header.index("CAS #")
        except ValueError:
            raise TraciError('The "Substances" sheet has no "Substance Name" or "CAS #" column.')
        air_columns = {
            i: match for i, text in enumerate(header) if (match := _AIR_COLUMN.match(text))
        }
        if not air_columns:
            raise TraciError('The "Substances" sheet has no "<Category> Air (<unit>)" columns.')

        wanted = {name: (metric, cas) for metric, (name, cas) in CAMPD_SUBSTANCES.items()}
        factors: list[dict[str, Any]] = []
        found: set[str] = set()
        for row in rows:
            name = str(row[name_column] or "").strip().upper()
            if name not in wanted:
                continue
            metric, cas = wanted[name]
            if cas and _cas_digits(row[cas_column]) != cas:
                raise TraciError(
                    f"{name} has CAS number {row[cas_column]!r} in the workbook, expected {cas}."
                )
            found.add(name)
            for column, match in air_columns.items():
                value = _number(row[column]) if column < len(row) else None
                if value is None:
                    continue
                factors.append(
                    {
                        "version": version,
                        "pollutant_name": name,
                        "cas_number": cas,
                        "campd_metric": metric,
                        "medium": "air",
                        "impact_category": match["category"].strip(),
                        "factor_value": value,
                        "factor_unit": match["unit"].strip(),
                        "source": f"{path.name}, sheet {SUBSTANCE_SHEET}, column \"{header[column]}\"",
                    }
                )
    finally:
        workbook.close()

    missing = sorted(set(wanted) - found)
    if missing:
        raise TraciError(f"The workbook has no row for {', '.join(missing)}.")
    return factors


def save_factors(factors: list[dict[str, Any]]) -> tuple[int, int]:
    """Insert new factors and update changed ones; returns (inserted, updated).

    A factor is identified by version, substance, medium and impact category,
    so loading the same workbook again changes nothing. The caller commits.
    """
    inserted = updated = 0
    for factor in factors:
        existing = db.session.scalar(
            select(TraciFactor).where(
                TraciFactor.version == factor["version"],
                TraciFactor.pollutant_name == factor["pollutant_name"],
                TraciFactor.medium == factor["medium"],
                TraciFactor.impact_category == factor["impact_category"],
            )
        )
        if existing is None:
            db.session.add(TraciFactor(**factor))
            inserted += 1
            continue
        changed = {k: v for k, v in factor.items() if getattr(existing, k) != v}
        if changed:
            for key, value in changed.items():
                setattr(existing, key, value)
            updated += 1
    return inserted, updated


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version))


def latest_version() -> Optional[str]:
    versions = db.session.scalars(select(TraciFactor.version).distinct()).all()
    return max(versions, key=_version_key) if versions else None


def factors_for(version: Optional[str] = None) -> list[TraciFactor]:
    """The stored factors of one TRACI version (the latest if none is given)."""
    version = version or latest_version()
    if version is None:
        return []
    return list(
        db.session.scalars(
            select(TraciFactor)
            .where(TraciFactor.version == version)
            .order_by(TraciFactor.impact_category, TraciFactor.campd_metric)
        )
    )
