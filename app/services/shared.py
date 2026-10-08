"""The team's shared database (Supabase PostgreSQL).

Each copy of the app keeps its data in its own local SQLite file, which is
the project database. The shared database holds the team's common starting
data:

* :func:`push_shared` publishes this machine's database to it. It only
  inserts rows the shared copy lacks (and refreshes facility and unit
  attributes), so it never deletes anyone's data and can be re-run.
* :func:`pull_shared` seeds an *empty* local database from it.

After seeding, everything a user retrieves or uploads stays in their local
SQLite file. Original uploaded files and staged retrievals are never shared.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Iterable, Optional
from urllib.parse import parse_qsl, urlsplit

from sqlalchemy import Table, create_engine, func, inspect, select, text
from sqlalchemy.engine import Engine

from app import db
from app.models import (
    AnnualRecord,
    DataProvenance,
    DataQualityIssue,
    Dataset,
    Facility,
    Unit,
)
from app.services.ingest import SOURCE_NAMES

# In foreign-key order. UploadedFile is deliberately absent: the files it
# points to exist only on the machine that received them.
SHARED_MODELS = (Dataset, Facility, Unit, AnnualRecord, DataProvenance, DataQualityIssue)

# Attributes on these rows improve over time (county, coordinates, latest
# fuel), so a push refreshes them instead of skipping existing rows.
REFRESHED_MODELS = {Facility, Unit}

BATCH_SIZE = 2000

Progress = Optional[Callable[[str], None]]


class SharedDataError(RuntimeError):
    """A push or pull could not run; the message is safe to show to users."""


def local_has_data() -> bool:
    return bool(db.session.scalar(select(func.count(Dataset.id))))


def _engine(url: str) -> Engine:
    options: dict[str, Any] = {"pool_pre_ping": True}
    if url.startswith("postgresql"):
        # psycopg2 otherwise sends an upsert batch one row per round trip,
        # which turns ~50,000 rows into an hour against a remote server.
        options.update(executemany_mode="values_plus_batch", executemany_batch_page_size=500)
    return create_engine(url, **options)


def _batches(rows: Iterable[Any]) -> Iterable[list[dict[str, Any]]]:
    batch: list[dict[str, Any]] = []
    for row in rows:
        batch.append(dict(row))
        if len(batch) >= BATCH_SIZE:
            yield batch
            batch = []
    if batch:
        yield batch


def _say(progress: Progress, message: str) -> None:
    if progress:
        progress(message)


# ---------------------------------------------------------------------------
# Pull: shared -> empty local database
# ---------------------------------------------------------------------------


def pull_shared(url: str, progress: Progress = None) -> dict[str, int]:
    """Copy the shared database into this (empty) local database.

    IDs are kept, so dataset numbers match the shared copy. Returns the
    number of rows copied per table.
    """
    if local_has_data():
        raise SharedDataError(
            "This computer's database already has data, so shared data was not "
            "loaded (it would mix two sets of IDs). Delete data/epadata.db and "
            "restart the app to start again from the shared data."
        )

    source = _engine(url)
    counts: dict[str, int] = {}
    try:
        inspector = inspect(source)
        source_tables = set(inspector.get_table_names())
        if "annual_records" not in source_tables:
            raise SharedDataError("The shared database has no epaData tables yet.")

        with source.connect() as connection:
            for model in SHARED_MODELS:
                table: Table = model.__table__
                if table.name not in source_tables:
                    continue
                present = {c["name"] for c in inspector.get_columns(table.name)}
                columns = [c for c in table.columns if c.name in present]
                result = connection.execution_options(yield_per=BATCH_SIZE).execute(
                    select(*columns).order_by(*table.primary_key.columns)
                )
                copied = 0
                for batch in _batches(result.mappings()):
                    db.session.execute(table.insert(), batch)
                    copied += len(batch)
                counts[table.name] = copied
                _say(progress, f"  {table.name}: {copied:,} rows")

        if "data_provenance" not in source_tables:
            # A shared database still on the pre-provenance schema.
            _backfill_provenance()
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    finally:
        source.dispose()
    return counts


def _backfill_provenance() -> None:
    """Create provenance rows for datasets copied from the original schema."""
    years_by_dataset: dict[int, list[int]] = {}
    for dataset_id, year in db.session.execute(
        select(AnnualRecord.dataset_id, AnnualRecord.reporting_year).distinct()
    ):
        years_by_dataset.setdefault(dataset_id, []).append(year)

    for dataset in db.session.scalars(select(Dataset)):
        is_api = dataset.data_source == "CAMPD API"
        source_url = query = None
        if is_api and (dataset.original_filename or "").startswith("http"):
            parts = urlsplit(dataset.original_filename)
            source_url = f"{parts.scheme}://{parts.netloc}{parts.path}"
            query = dict(parse_qsl(parts.query))
        years = sorted(years_by_dataset.get(dataset.id, [dataset.reporting_year]))
        db.session.add(
            DataProvenance(
                dataset_id=dataset.id,
                source_type="api" if is_api else "upload",
                source_name=SOURCE_NAMES["api" if is_api else "upload"],
                source_url=source_url,
                query_params=json.dumps(query) if query else None,
                original_filename=dataset.original_filename,
                retrieved_at=dataset.retrieval_date,
                reporting_years=", ".join(str(y) for y in years),
                raw_records=dataset.raw_records,
                accepted_records=dataset.accepted_records,
                rejected_records=max(0, dataset.raw_records - dataset.accepted_records),
                warning_records=0,
                notes=(
                    "Imported before row-level quality tracking. Rejected rows "
                    "were counted but not kept, and the original file was not "
                    f"stored. Original notes: {dataset.notes or 'none'}"
                ),
            )
        )


# ---------------------------------------------------------------------------
# Push: local -> shared database
# ---------------------------------------------------------------------------


def migrate_shared(url: str) -> None:
    """Bring the shared database's schema up to this code's latest migration."""
    from flask_migrate import upgrade

    from app import BASE_DIR, create_app

    shared_app = create_app({"SQLALCHEMY_DATABASE_URI": url, "AUTO_MIGRATE": False})
    with shared_app.app_context():
        upgrade(directory=str(Path(BASE_DIR) / "migrations"))
        db.engine.dispose()


def _upsert(target: Engine, table: Table, refresh: bool):
    """An INSERT that skips existing rows, or updates them when refreshing."""
    if target.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif target.dialect.name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        raise SharedDataError(f"Unsupported shared database type: {target.dialect.name}")

    statement = insert(table)
    if refresh:
        keys = [c.name for c in table.primary_key.columns]
        return statement.on_conflict_do_update(
            index_elements=keys,
            set_={c.name: statement.excluded[c.name] for c in table.columns if c.name not in keys},
        )
    return statement.on_conflict_do_nothing()


def _reset_sequences(connection: Any) -> None:
    """Move PostgreSQL id sequences past the explicitly inserted ids."""
    for model in SHARED_MODELS:
        table = model.__table__
        for column in table.primary_key.columns:
            if column.autoincrement is False:
                continue
            connection.execute(
                text(
                    "SELECT setval(pg_get_serial_sequence(:table, :column), "
                    f"COALESCE((SELECT MAX({column.name}) FROM {table.name}), 0) + 1, false)"
                ),
                {"table": table.name, "column": column.name},
            )


def push_shared(url: str, progress: Progress = None) -> dict[str, tuple[int, int]]:
    """Publish this machine's database to the shared database.

    Returns ``{table: (rows before, rows after)}`` for the shared database.
    """
    _say(progress, "Updating the shared database schema ...")
    migrate_shared(url)

    target = _engine(url)
    summary: dict[str, tuple[int, int]] = {}
    try:
        with target.begin() as connection:
            for model in SHARED_MODELS:
                table: Table = model.__table__
                before = connection.execute(select(func.count()).select_from(table)).scalar_one()
                statement = _upsert(target, table, refresh=model in REFRESHED_MODELS)
                rows = db.session.execute(
                    select(table).order_by(*table.primary_key.columns).execution_options(
                        yield_per=BATCH_SIZE
                    )
                ).mappings()
                for batch in _batches(rows):
                    connection.execute(statement, batch)
                after = connection.execute(select(func.count()).select_from(table)).scalar_one()
                summary[table.name] = (before, after)
                _say(progress, f"  {table.name}: {before:,} -> {after:,} rows")
            if target.dialect.name == "postgresql":
                _reset_sequences(connection)
    finally:
        target.dispose()
    return summary
