"""Data-explorer query builder.

A search is described entirely by URL query parameters, so any result set can
be bookmarked, shared and re-run. The explorer page, the JSON API and the CSV
download all go through :func:`parse_search` and :func:`build_statement`, so
they always return the same rows for the same URL.

Parameters
----------
Basic filters (text filters match case-insensitively; "contains" filters also
accept ``none`` for "no value", ``any`` for "has a value", and ``not <text>``
for "does not contain <text>", which includes rows with no value):

    facility_id     one or more EPA facility IDs, comma-separated   (equals)
    facility_name   (contains)          county          (contains)
    unit_id         EPA unit ID         (equals)
    unit            internal unit keys, repeatable (used for selections)
    dataset         one or more dataset IDs (records imported by that run)
    state           one or more two-letter codes, comma-separated
    year            one or more years, comma-separated
    year_min / year_max
    primary_fuel, secondary_fuel, unit_type, owner, so2_control,
    nox_control, pm_control, hg_control, program                     (contains)

Range filters, inclusive, each with ``_min`` and/or ``_max``:

    op_time (hours)  gross_load (MWh)  heat_input (MMBtu)
    co2, so2, nox (short tons)

Result shape:

    level      "unit" (one row per unit-year, default) or "facility"
               (one row per facility-year, metrics summed over its units;
               range filters then apply to the facility totals)
    sort, order    column and "asc"/"desc"
    limit      top-N / bottom-N: return only the first N rows
    group_by   rank within groups ("state", "primary_fuel", "unit_type",
               "year", "facility"); with per_group (default 1) keeps the
               first N rows of each group, e.g. the top CO2 emitter per state
    page, per_page
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

from sqlalchemy import Select, and_, func, or_, select
from werkzeug.datastructures import MultiDict

from app import db
from app.models import AnnualRecord, Dataset, Facility, Unit

DEFAULT_PER_PAGE = 25
MAX_PER_PAGE = 100
PER_PAGE_CHOICES = (10, 25, 50, 100)
MAX_LIMIT = 1000
MAX_PER_GROUP = 50

LEVELS = ("unit", "facility")

# param -> (column, label used in the query description, match mode)
TEXT_FILTERS = {
    "facility_name": (Facility.facility_name, "facility_name", "contains"),
    "county": (Facility.county, "county", "contains"),
    "unit_id": (Unit.epa_unit_id, "epa_unit_id", "equals"),
    "primary_fuel": (Unit.primary_fuel, "primary_fuel", "contains"),
    "secondary_fuel": (Unit.secondary_fuel, "secondary_fuel", "contains"),
    "unit_type": (Unit.unit_type, "unit_type", "contains"),
    "owner": (Unit.owner_operator, "owner_operator", "contains"),
    "so2_control": (AnnualRecord.so2_control, "so2_control", "contains"),
    "nox_control": (AnnualRecord.nox_control, "nox_control", "contains"),
    "pm_control": (AnnualRecord.pm_control, "pm_control", "contains"),
    "hg_control": (AnnualRecord.hg_control, "hg_control", "contains"),
    "program": (AnnualRecord.program_code, "program_code", "contains"),
}

# param prefix -> (record column, result key, label, unit of measure)
RANGE_FILTERS = {
    "op_time": (AnnualRecord.operating_time, "operating_time", "Operating time", "h"),
    "gross_load": (AnnualRecord.gross_load, "gross_load", "Gross load", "MWh"),
    "heat_input": (AnnualRecord.heat_input, "heat_input", "Heat input", "MMBtu"),
    "co2": (AnnualRecord.co2_mass, "co2_mass", "CO2", "short tons"),
    "so2": (AnnualRecord.so2_mass, "so2_mass", "SO2", "short tons"),
    "nox": (AnnualRecord.nox_mass, "nox_mass", "NOx", "short tons"),
}

METRICS = ("operating_time", "gross_load", "heat_input", "co2_mass", "so2_mass", "nox_mass")

METRIC_LABELS = {
    "operating_time": "Operating time (h)",
    "gross_load": "Gross load (MWh)",
    "heat_input": "Heat input (MMBtu)",
    "co2_mass": "CO2 (short tons)",
    "so2_mass": "SO2 (short tons)",
    "nox_mass": "NOx (short tons)",
}

UNIT_SORTS = {
    "facility_name": Facility.facility_name,
    "state_code": Facility.state_code,
    "county": Facility.county,
    "epa_unit_id": Unit.epa_unit_id,
    "unit_type": Unit.unit_type,
    "primary_fuel": Unit.primary_fuel,
    "reporting_year": AnnualRecord.reporting_year,
    "operating_time": AnnualRecord.operating_time,
    "gross_load": AnnualRecord.gross_load,
    "heat_input": AnnualRecord.heat_input,
    "co2_mass": AnnualRecord.co2_mass,
    "so2_mass": AnnualRecord.so2_mass,
    "nox_mass": AnnualRecord.nox_mass,
}

# Facility-level metrics are sums over the facility's units for one year.
# Operating time is per unit, so it is not summed (its range filter still
# applies to the individual units).
FACILITY_AGGREGATES = {
    "unit_count": func.count(func.distinct(Unit.internal_unit_key)),
    "gross_load": func.sum(AnnualRecord.gross_load),
    "heat_input": func.sum(AnnualRecord.heat_input),
    "co2_mass": func.sum(AnnualRecord.co2_mass),
    "so2_mass": func.sum(AnnualRecord.so2_mass),
    "nox_mass": func.sum(AnnualRecord.nox_mass),
}

FACILITY_SORTS = {
    "facility_name": Facility.facility_name,
    "state_code": Facility.state_code,
    "county": Facility.county,
    "reporting_year": AnnualRecord.reporting_year,
    **FACILITY_AGGREGATES,
}

# group_by value -> (unit-level column, facility-level column or None, label)
GROUPS = {
    "state": (Facility.state_code, Facility.state_code, "state_code"),
    "year": (AnnualRecord.reporting_year, AnnualRecord.reporting_year, "reporting_year"),
    "primary_fuel": (Unit.primary_fuel, None, "primary_fuel"),
    "unit_type": (Unit.unit_type, None, "unit_type"),
    "facility": (Facility.epa_facility_id, None, "facility_id"),
}

UNIT_COLUMNS = (
    Facility.epa_facility_id.label("facility_id"),
    Facility.facility_name,
    Facility.state_code,
    Facility.county,
    Facility.latitude,
    Facility.longitude,
    Facility.source_category,
    Unit.internal_unit_key.label("unit_key"),
    Unit.epa_unit_id,
    Unit.unit_type,
    Unit.primary_fuel,
    Unit.secondary_fuel,
    Unit.owner_operator,
    Unit.operating_date,
    Unit.retirement_date,
    AnnualRecord.id.label("record_id"),
    AnnualRecord.reporting_year,
    AnnualRecord.operating_time,
    AnnualRecord.gross_load,
    AnnualRecord.steam_load,
    AnnualRecord.heat_input,
    AnnualRecord.co2_mass,
    AnnualRecord.so2_mass,
    AnnualRecord.nox_mass,
    AnnualRecord.so2_control,
    AnnualRecord.nox_control,
    AnnualRecord.pm_control,
    AnnualRecord.hg_control,
    AnnualRecord.program_code,
    AnnualRecord.dataset_id,
    Dataset.dataset_name,
    Dataset.data_source,
)

FACILITY_GROUP_COLUMNS = (
    Facility.epa_facility_id,
    Facility.facility_name,
    Facility.state_code,
    Facility.county,
    AnnualRecord.reporting_year,
)

# (result key, CSV header) in export order.
UNIT_EXPORT_COLUMNS = [
    ("facility_id", "facility_id"),
    ("facility_name", "facility_name"),
    ("state_code", "state"),
    ("county", "county"),
    ("latitude", "latitude"),
    ("longitude", "longitude"),
    ("source_category", "source_category"),
    ("epa_unit_id", "unit_id"),
    ("unit_type", "unit_type"),
    ("primary_fuel", "primary_fuel"),
    ("secondary_fuel", "secondary_fuel"),
    ("owner_operator", "owner_operator"),
    ("operating_date", "commercial_operation_date"),
    ("retirement_date", "retirement_date"),
    ("reporting_year", "reporting_year"),
    ("operating_time", "operating_time_hours"),
    ("gross_load", "gross_load_mwh"),
    ("steam_load", "steam_load_1000lb"),
    ("heat_input", "heat_input_mmbtu"),
    ("co2_mass", "co2_mass_short_tons"),
    ("so2_mass", "so2_mass_short_tons"),
    ("nox_mass", "nox_mass_short_tons"),
    ("so2_control", "so2_controls"),
    ("nox_control", "nox_controls"),
    ("pm_control", "pm_controls"),
    ("hg_control", "hg_controls"),
    ("program_code", "program_codes"),
    ("dataset_id", "dataset_id"),
    ("dataset_name", "dataset_name"),
    ("data_source", "data_source"),
]

FACILITY_EXPORT_COLUMNS = [
    ("facility_id", "facility_id"),
    ("facility_name", "facility_name"),
    ("state_code", "state"),
    ("county", "county"),
    ("reporting_year", "reporting_year"),
    ("unit_count", "units_included"),
    ("gross_load", "total_gross_load_mwh"),
    ("heat_input", "total_heat_input_mmbtu"),
    ("co2_mass", "total_co2_mass_short_tons"),
    ("so2_mass", "total_so2_mass_short_tons"),
    ("nox_mass", "total_nox_mass_short_tons"),
]


@dataclass
class Search:
    """A parsed, validated search. Build with :func:`parse_search`."""

    level: str = "unit"
    text: dict[str, str] = field(default_factory=dict)
    states: list[str] = field(default_factory=list)
    facility_ids: list[int] = field(default_factory=list)
    unit_keys: list[int] = field(default_factory=list)
    dataset_ids: list[int] = field(default_factory=list)
    years: list[int] = field(default_factory=list)
    year_min: Optional[int] = None
    year_max: Optional[int] = None
    ranges: dict[str, tuple[Optional[float], Optional[float]]] = field(default_factory=dict)
    sort: str = "reporting_year"
    order: str = "desc"
    limit: Optional[int] = None
    group_by: Optional[str] = None
    per_group: int = 1
    page: int = 1
    per_page: int = DEFAULT_PER_PAGE
    errors: list[str] = field(default_factory=list)

    @property
    def is_facility_level(self) -> bool:
        return self.level == "facility"

    @property
    def rank_metric(self) -> str:
        """The column rankings use: the sort column if it is a metric, else CO2."""
        if self.sort in METRICS or (self.is_facility_level and self.sort in FACILITY_AGGREGATES):
            return self.sort
        return "co2_mass"

    @property
    def export_columns(self) -> list[tuple[str, str]]:
        columns = FACILITY_EXPORT_COLUMNS if self.is_facility_level else UNIT_EXPORT_COLUMNS
        if self.group_by:
            return [("group_rank", "rank_in_group"), *columns]
        return list(columns)

    @property
    def has_filters(self) -> bool:
        return bool(
            self.text or self.states or self.facility_ids or self.unit_keys
            or self.dataset_ids or self.years
            or self.year_min or self.year_max or self.ranges
        )

    def describe(self) -> str:
        """The search as a readable SQL-like statement, for display."""
        where = list(self._describe_where())
        having = list(self._describe_having())
        lines = []
        if self.is_facility_level:
            lines.append("SELECT facility, year, SUM(metrics) ... GROUP BY facility, year")
        if where:
            lines.append("WHERE " + "\n  AND ".join(where))
        if having:
            lines.append("HAVING " + "\n  AND ".join(having))
        if self.group_by:
            direction = "highest" if self.order == "desc" else "lowest"
            lines.append(
                f"RANK {self.per_group} {direction} {self.rank_metric} "
                f"PER {GROUPS[self.group_by][2]}"
            )
        else:
            lines.append(f"ORDER BY {self.sort} {self.order.upper()}")
        if self.limit:
            lines.append(f"LIMIT {self.limit}")
        return "\n".join(lines)

    def _describe_where(self) -> Iterator[str]:
        if self.facility_ids:
            yield _describe_in("facility_id", self.facility_ids)
        if self.unit_keys:
            yield _describe_in("unit_key", self.unit_keys)
        if self.dataset_ids:
            yield _describe_in("dataset_id", self.dataset_ids)
        if self.states:
            yield _describe_in("state_code", [f"'{s}'" for s in self.states])
        if self.years:
            yield _describe_in("reporting_year", self.years)
        if self.year_min is not None and self.year_max is not None:
            yield f"reporting_year BETWEEN {self.year_min} AND {self.year_max}"
        elif self.year_min is not None:
            yield f"reporting_year >= {self.year_min}"
        elif self.year_max is not None:
            yield f"reporting_year <= {self.year_max}"
        for param, value in self.text.items():
            _, column, mode = TEXT_FILTERS[param]
            lowered = value.lower()
            if mode == "equals":
                yield f"{column} = '{value}'"
            elif lowered == "none":
                yield f"{column} IS NULL"
            elif lowered == "any":
                yield f"{column} IS NOT NULL"
            elif _excluded(value):
                yield f"{column} NOT CONTAINS '{_excluded(value)}'"
            else:
                yield f"{column} CONTAINS '{value}'"
        for prefix, (low, high) in self.ranges.items():
            if self.is_facility_level and prefix != "op_time":
                continue
            yield _describe_range(RANGE_FILTERS[prefix][1], low, high)

    def _describe_having(self) -> Iterator[str]:
        if not self.is_facility_level:
            return
        for prefix, (low, high) in self.ranges.items():
            if prefix == "op_time":
                continue
            yield _describe_range(f"SUM({RANGE_FILTERS[prefix][1]})", low, high)


def _describe_in(column: str, values: list[Any]) -> str:
    if len(values) == 1:
        return f"{column} = {values[0]}"
    return f"{column} IN ({', '.join(str(v) for v in values)})"


def _describe_range(column: str, low: Optional[float], high: Optional[float]) -> str:
    if low is not None and high is not None:
        return f"{column} BETWEEN {low:,.0f} AND {high:,.0f}"
    if low is not None:
        return f"{column} >= {low:,.0f}"
    return f"{column} <= {high:,.0f}"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _values(args: MultiDict, name: str) -> list[str]:
    """Every value of a parameter, splitting comma-separated lists."""
    values: list[str] = []
    for raw in args.getlist(name):
        for part in str(raw).split(","):
            part = part.strip()
            if part and part not in values:
                values.append(part)
    return values


def _parse_int(text: str, label: str, errors: list[str]) -> Optional[int]:
    try:
        return int(text.replace(",", "").strip())
    except ValueError:
        errors.append(f"{label} '{text}' is not a whole number, so it was ignored.")
        return None


def _parse_float(text: str, label: str, errors: list[str]) -> Optional[float]:
    try:
        value = float(text.replace(",", "").strip())
    except ValueError:
        errors.append(f"{label} '{text}' is not a number, so it was ignored.")
        return None
    if math.isnan(value) or math.isinf(value):
        errors.append(f"{label} '{text}' is not a finite number, so it was ignored.")
        return None
    return value


def parse_search(args: MultiDict) -> Search:
    """Read and validate a search from query-string arguments.

    Invalid values never raise: each is reported in ``search.errors`` and the
    filter it belonged to is dropped.
    """
    search = Search()
    errors = search.errors

    level = (args.get("level") or "unit").strip().lower()
    search.level = level if level in LEVELS else "unit"

    for param in TEXT_FILTERS:
        value = (args.get(param) or "").strip()
        if value:
            search.text[param] = value[:100]

    search.states = [s.upper() for s in _values(args, "state") if len(s) == 2 and s.isalpha()]
    for bad in (s for s in _values(args, "state") if not (len(s) == 2 and s.isalpha())):
        errors.append(f"State '{bad}' is not a two-letter code, so it was ignored.")

    for raw in _values(args, "facility_id"):
        value = _parse_int(raw, "Facility ID", errors)
        if value is not None:
            search.facility_ids.append(value)
    for raw in _values(args, "unit"):
        value = _parse_int(raw, "Unit key", errors)
        if value is not None:
            search.unit_keys.append(value)
    for raw in _values(args, "dataset"):
        value = _parse_int(raw, "Dataset ID", errors)
        if value is not None:
            search.dataset_ids.append(value)
    for raw in _values(args, "year"):
        value = _parse_int(raw, "Year", errors)
        if value is not None:
            search.years.append(value)

    for name in ("year_min", "year_max"):
        raw = (args.get(name) or "").strip()
        if raw:
            setattr(search, name, _parse_int(raw, name.replace("_", " ").capitalize(), errors))

    for prefix, (_, _, label, _) in RANGE_FILTERS.items():
        low_raw = (args.get(f"{prefix}_min") or "").strip()
        high_raw = (args.get(f"{prefix}_max") or "").strip()
        low = _parse_float(low_raw, f"{label} minimum", errors) if low_raw else None
        high = _parse_float(high_raw, f"{label} maximum", errors) if high_raw else None
        if low is not None and high is not None and low > high:
            errors.append(f"{label} minimum is greater than its maximum; the two were swapped.")
            low, high = high, low
        if low is not None or high is not None:
            search.ranges[prefix] = (low, high)

    sorts = FACILITY_SORTS if search.is_facility_level else UNIT_SORTS
    sort = (args.get("sort") or "").strip()
    if sort and sort not in sorts:
        errors.append(f"Cannot sort by '{sort}'; sorted by reporting year instead.")
    search.sort = sort if sort in sorts else "reporting_year"
    search.order = "asc" if (args.get("order") or "").lower() == "asc" else "desc"

    raw_limit = (args.get("limit") or "").strip()
    if raw_limit:
        limit = _parse_int(raw_limit, "Top-N limit", errors)
        if limit is not None:
            search.limit = max(1, min(limit, MAX_LIMIT))

    group_by = (args.get("group_by") or "").strip()
    if group_by:
        allowed = GROUPS.get(group_by)
        if allowed is None or (search.is_facility_level and allowed[1] is None):
            errors.append(f"Cannot rank within '{group_by}' at this level; ranking ignored.")
        else:
            search.group_by = group_by
            raw_per_group = (args.get("per_group") or "1").strip()
            per_group = _parse_int(raw_per_group, "Results per group", errors) or 1
            search.per_group = max(1, min(per_group, MAX_PER_GROUP))

    raw_page = (args.get("page") or "1").strip()
    search.page = max(1, _parse_int(raw_page, "Page", errors) or 1)
    raw_per_page = (args.get("per_page") or str(DEFAULT_PER_PAGE)).strip()
    per_page = _parse_int(raw_per_page, "Rows per page", errors) or DEFAULT_PER_PAGE
    search.per_page = max(1, min(per_page, MAX_PER_PAGE))
    return search


# ---------------------------------------------------------------------------
# Statement building
# ---------------------------------------------------------------------------


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _excluded(value: str) -> Optional[str]:
    """The text after "not " in a "contains" filter value, if it has one."""
    if value[:4].lower() == "not " and value[4:].strip():
        return value[4:].strip()
    return None


def _row_conditions(search: Search) -> list[Any]:
    """Conditions on individual unit-year rows (the WHERE clause)."""
    conditions: list[Any] = []
    for param, value in search.text.items():
        column, _, mode = TEXT_FILTERS[param]
        lowered = value.lower()
        if mode == "equals":
            conditions.append(func.lower(column) == lowered)
        elif lowered == "none":
            conditions.append(or_(column.is_(None), column == ""))
        elif lowered == "any":
            conditions.append(and_(column.is_not(None), column != ""))
        elif _excluded(value):
            pattern = f"%{_escape_like(_excluded(value))}%"
            conditions.append(or_(column.is_(None), ~column.ilike(pattern, escape="\\")))
        else:
            conditions.append(column.ilike(f"%{_escape_like(value)}%", escape="\\"))

    if search.states:
        conditions.append(Facility.state_code.in_(search.states))
    if search.facility_ids:
        conditions.append(Facility.epa_facility_id.in_(search.facility_ids))
    if search.unit_keys:
        conditions.append(Unit.internal_unit_key.in_(search.unit_keys))
    if search.dataset_ids:
        conditions.append(AnnualRecord.dataset_id.in_(search.dataset_ids))
    if search.years:
        conditions.append(AnnualRecord.reporting_year.in_(search.years))
    if search.year_min is not None:
        conditions.append(AnnualRecord.reporting_year >= search.year_min)
    if search.year_max is not None:
        conditions.append(AnnualRecord.reporting_year <= search.year_max)

    for prefix, (low, high) in search.ranges.items():
        if search.is_facility_level and prefix != "op_time":
            continue
        column = RANGE_FILTERS[prefix][0]
        if low is not None:
            conditions.append(column >= low)
        if high is not None:
            conditions.append(column <= high)
    return conditions


def _having_conditions(search: Search) -> list[Any]:
    conditions: list[Any] = []
    for prefix, (low, high) in search.ranges.items():
        if prefix == "op_time":
            continue
        total = func.sum(RANGE_FILTERS[prefix][0])
        if low is not None:
            conditions.append(total >= low)
        if high is not None:
            conditions.append(total <= high)
    return conditions


def _joined(statement: Select) -> Select:
    return (
        statement.select_from(Facility)
        .join(Unit, Unit.facility_id == Facility.epa_facility_id)
        .join(AnnualRecord, AnnualRecord.unit_id == Unit.internal_unit_key)
    )


def build_statement(search: Search) -> Select:
    """The full, ordered statement for a search, before pagination."""
    descending = search.order == "desc"

    if search.is_facility_level:
        statement = _joined(
            select(
                Facility.epa_facility_id.label("facility_id"),
                Facility.facility_name,
                Facility.state_code,
                Facility.county,
                AnnualRecord.reporting_year,
                *(expr.label(key) for key, expr in FACILITY_AGGREGATES.items()),
            )
        )
        statement = statement.where(*_row_conditions(search)).group_by(*FACILITY_GROUP_COLUMNS)
        having = _having_conditions(search)
        if having:
            statement = statement.having(*having)
        sort_expr = FACILITY_SORTS[search.sort]
        tiebreak = (Facility.epa_facility_id, AnnualRecord.reporting_year)
        rank_expr = FACILITY_AGGREGATES.get(search.rank_metric, FACILITY_AGGREGATES["co2_mass"])
        partition = GROUPS[search.group_by][1] if search.group_by else None
    else:
        statement = _joined(select(*UNIT_COLUMNS)).join(
            Dataset, Dataset.id == AnnualRecord.dataset_id
        )
        statement = statement.where(*_row_conditions(search))
        sort_expr = UNIT_SORTS[search.sort]
        tiebreak = (AnnualRecord.id,)
        rank_expr = UNIT_SORTS[search.rank_metric]
        partition = GROUPS[search.group_by][0] if search.group_by else None

    if partition is not None:
        rank = func.row_number().over(
            partition_by=partition,
            order_by=(rank_expr.desc() if descending else rank_expr.asc(), *tiebreak),
        )
        inner = statement.add_columns(rank.label("group_rank")).subquery()
        group_label = GROUPS[search.group_by][2]
        statement = (
            select(inner)
            .where(inner.c.group_rank <= search.per_group)
            .order_by(inner.c[group_label], inner.c.group_rank)
        )
    else:
        primary = sort_expr.desc() if descending else sort_expr.asc()
        # Put blanks last whichever way text columns are sorted.
        statement = statement.order_by(primary.nulls_last(), *tiebreak)

    if search.limit:
        statement = statement.limit(search.limit)
    return statement


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


@dataclass
class SearchPage:
    rows: list[Any]
    total: int
    page: int
    per_page: int

    @property
    def total_pages(self) -> int:
        return max(1, math.ceil(self.total / self.per_page))

    @property
    def first_index(self) -> int:
        return 0 if not self.total else (self.page - 1) * self.per_page + 1

    @property
    def last_index(self) -> int:
        return min(self.total, self.page * self.per_page)


def run_search(search: Search) -> SearchPage:
    """Execute a search and return one page of rows."""
    statement = build_statement(search)
    total = db.session.execute(
        select(func.count()).select_from(statement.order_by(None).subquery())
    ).scalar_one()

    if search.limit:
        # A top-N result is short by definition; show it on one page.
        per_page = max(search.limit, 1)
        page = 1
        rows = db.session.execute(statement).all()
    else:
        per_page = search.per_page
        last_page = max(1, math.ceil(total / per_page))
        page = min(search.page, last_page)
        rows = db.session.execute(
            statement.offset((page - 1) * per_page).limit(per_page)
        ).all()
    return SearchPage(rows=rows, total=total, page=page, per_page=per_page)


def iter_search_rows(search: Search) -> Iterator[Any]:
    """Every row matching a search, streamed in batches (for downloads)."""
    statement = build_statement(search).execution_options(yield_per=2000)
    yield from db.session.execute(statement)


def row_values(row: Any, columns: list[tuple[str, str]]) -> list[Any]:
    mapping = row._mapping
    return [mapping.get(key) for key, _ in columns]


# ---------------------------------------------------------------------------
# Choices for search forms
# ---------------------------------------------------------------------------


def _distinct(column: Any, separator: Optional[str] = None) -> list[str]:
    """Distinct non-blank values of a column, split on a list separator."""
    values: set[str] = set()
    for (value,) in db.session.execute(select(column).distinct().where(column.is_not(None))):
        parts = str(value).split(separator) if separator else [str(value)]
        values.update(part.strip() for part in parts if part.strip())
    return sorted(values, key=str.lower)


def filter_choices() -> dict[str, list[Any]]:
    """Values actually present in the database, for dropdowns and datalists.

    Control and program fields hold several values in one cell ("Wet
    Scrubber|Baghouse", "ARP, CSNOX"), so they are split into single values,
    which the "contains" filters then match.
    """
    years = db.session.scalars(
        select(AnnualRecord.reporting_year).distinct().order_by(AnnualRecord.reporting_year.desc())
    ).all()
    return {
        "years": list(years),
        "states": _distinct(Facility.state_code),
        "primary_fuels": _distinct(Unit.primary_fuel),
        "secondary_fuels": _distinct(Unit.secondary_fuel),
        "unit_types": _distinct(Unit.unit_type),
        "so2_controls": _distinct(AnnualRecord.so2_control, "|"),
        "nox_controls": _distinct(AnnualRecord.nox_control, "|"),
        "pm_controls": _distinct(AnnualRecord.pm_control, "|"),
        "hg_controls": _distinct(AnnualRecord.hg_control, "|"),
        "programs": _distinct(AnnualRecord.program_code, ","),
    }
