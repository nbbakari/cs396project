"""Fetch and import annual emissions from the EPA CAMPD API.

Kept for the original command-line interface; it is a thin wrapper around
`flask campd fetch`, which does the same through the app's validation
pipeline (rejected rows are recorded in the dataset's data-quality report).

    python sync_campd.py 2021 --state CA
"""

import argparse

from app import create_app
from app.cli import _fetch_and_import

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Fetch and ingest annual emissions data from EPA CAMPD API."
    )
    parser.add_argument("year", type=int, help="Reporting year to fetch (e.g., 2021)")
    parser.add_argument("--state", type=str, help="Optional: 2-letter state code (e.g., CA)")
    args = parser.parse_args()

    with create_app().app_context():
        _fetch_and_import(args.year, state=args.state)
