"""Flask CLI commands for loading data without the browser.

    flask campd fetch 2023 --state KY        retrieve and import one query
    flask campd load-years 2015 2025         retrieve and import a span of years
    flask campd attributes 2016              fill blank facility/unit attributes
    flask data pull-shared                   seed an empty database from the team's
                                             shared (Supabase) database
    flask data push-shared                   publish this database to the shared one

CLI imports skip the review page but go through the same validation, so every
rejected row is still recorded in the dataset's data-quality report.
"""

from __future__ import annotations

from typing import Optional

import click
from flask import Flask, current_app
from flask.cli import AppGroup

from app import db
from app.services.campd import CampdError, build_query, fetch_attributes, retrieve_annual
from app.services.ingest import (
    discard_staged,
    enrich_from_attributes,
    import_staged,
    stage_bytes,
)
from app.services.shared import SharedDataError, pull_shared, push_shared

campd_cli = AppGroup("campd", help="Retrieve data from the EPA CAMPD API.")
data_cli = AppGroup("data", help="Database maintenance.")


def _fetch_and_import(
    year: int,
    *,
    state: Optional[str] = None,
    facility_ids: Optional[list[int]] = None,
    fuel_type: Optional[str] = None,
    unit_type: Optional[str] = None,
    control: Optional[str] = None,
    attributes: bool = True,
    name: Optional[str] = None,
) -> None:
    query = build_query(
        year,
        state=state,
        facility_ids=facility_ids,
        fuel_type=fuel_type,
        unit_type=unit_type,
        control_technology=control,
    )
    click.echo(f"Retrieving {query} ...")
    try:
        retrieval = retrieve_annual(query, include_attributes=attributes)
    except CampdError as exc:
        raise click.ClickException(str(exc)) from None
    for warning in retrieval.warnings:
        click.echo(f"  warning: {warning}")
    if retrieval.frame.empty:
        click.echo("  no records matched; nothing imported.")
        return
    click.echo(
        f"  {retrieval.emission_rows:,} emission rows, {retrieval.attribute_rows:,} "
        f"attribute rows in {retrieval.elapsed_seconds:.1f}s"
    )

    label = "-".join(str(v) for v in (year, state, fuel_type, unit_type) if v)
    staged = stage_bytes(
        retrieval.frame.to_csv(index=False).encode("utf-8"),
        original_filename=f"campd-annual-emissions-{label.replace(' ', '_')}.csv",
        extension="csv",
        source_type="api",
        dataset_name=name or f"CAMPD API {label.replace('-', ' / ')}",
        source_url=retrieval.source_url,
        query_params=retrieval.query_params,
    )
    try:
        dataset, result = import_staged(staged)
    except ValueError as exc:
        db.session.rollback()
        discard_staged(staged)
        click.echo(f"  nothing imported: {exc}")
        return
    stats = result.stats
    click.echo(
        f"  dataset #{dataset.id}: imported {stats['accepted_records']:,}, "
        f"rejected {stats['rejected_records']:,}, with warnings {stats['warning_records']:,}"
    )


@campd_cli.command("fetch")
@click.argument("year", type=int)
@click.option("--state", help="Two-letter state code, e.g. KY.")
@click.option("--facility", "facilities", help="Facility IDs, comma-separated.")
@click.option("--fuel", help='Unit fuel type, e.g. "Coal".')
@click.option("--unit-type", help='Unit type, e.g. "Combined cycle".')
@click.option("--control", help='Control technology, e.g. "Wet Limestone".')
@click.option("--no-attributes", is_flag=True, help="Skip the facility-attributes call.")
@click.option("--name", help="Dataset name.")
def fetch_command(year, state, facilities, fuel, unit_type, control, no_attributes, name):
    """Retrieve one year of annual emissions from CAMPD and import it."""
    facility_ids = [int(f) for f in facilities.split(",")] if facilities else None
    _fetch_and_import(
        year,
        state=state,
        facility_ids=facility_ids,
        fuel_type=fuel,
        unit_type=unit_type,
        control=control,
        attributes=not no_attributes,
        name=name,
    )


@campd_cli.command("load-years")
@click.argument("first", type=int)
@click.argument("last", type=int)
@click.option("--state", help="Limit to one state.")
def load_years_command(first, last, state):
    """Retrieve and import every year from FIRST to LAST inclusive."""
    for year in range(first, last + 1):
        _fetch_and_import(year, state=state)


@campd_cli.command("attributes")
@click.argument("year", type=int)
@click.option("--state", help="Limit to one state.")
def attributes_command(year, state):
    """Fill blank county, location, category, owner and dates from CAMPD attributes."""
    try:
        frame = fetch_attributes(build_query(year, state=state))
    except CampdError as exc:
        raise click.ClickException(str(exc)) from None
    facilities, units = enrich_from_attributes(frame)
    click.echo(f"Updated {facilities:,} facilities and {units:,} units from {len(frame):,} rows.")


def _shared_url() -> str:
    url = current_app.config.get("SHARED_DATABASE_URL")
    if not url:
        raise click.ClickException(
            "No shared database is configured; set SHARED_DATABASE_URL in .env."
        )
    return url


@data_cli.command("push-shared")
@click.confirmation_option(
    prompt="Publish this computer's database to the team's shared database?"
)
def push_shared_command():
    """Publish this computer's data to the shared (Supabase) database.

    Brings the shared schema up to date, inserts every dataset, facility,
    unit, record, provenance row and quality issue it lacks, and refreshes
    facility and unit attributes. Nothing is deleted; safe to re-run.
    """
    try:
        push_shared(_shared_url(), progress=click.echo)
    except SharedDataError as exc:
        raise click.ClickException(str(exc)) from None
    click.echo("Done.")


@data_cli.command("pull-shared")
def pull_shared_command():
    """Seed this computer's empty database from the shared database."""
    try:
        pull_shared(_shared_url(), progress=click.echo)
    except SharedDataError as exc:
        raise click.ClickException(str(exc)) from None
    click.echo("Done. The shared data is now stored on this computer.")


def register_cli(app: Flask) -> None:
    app.cli.add_command(campd_cli)
    app.cli.add_command(data_cli)
