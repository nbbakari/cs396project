# epaData

**EPA Data Management, Evaluation, and Visualization System** — a Flask web
application for retrieving, validating, storing, searching, comparing and
downloading U.S. power-sector emissions data published by the Environmental
Protection Agency.

Data comes from the EPA **Clean Air Markets Program Data (CAMPD)** service,
either retrieved directly from the CAMPD API or uploaded as CSV/Excel extracts,
and is normalised into a relational SQLite database. Every record is traceable
to the retrieval or upload it came from, and every source row that was not
imported is kept in a data-quality report.

Built for CS396 coursework.

## Contents

- [Project status](#project-status)
- [Features](#features)
- [Quick start](#quick-start)
- [Reproducing the results](#reproducing-the-results)
- [Team setup](#team-setup)
- [Environment variables](#environment-variables)
- [Loading data](#loading-data)
- [Pages](#pages)
- [Search reference](#search-reference)
  - [Description-based search](#description-based-search)
- [Validation rules](#validation-rules)
- [Data model](#data-model)
- [Units, assumptions and limitations](#units-assumptions-and-limitations)
- [JSON API](#json-api)
- [Running the tests](#running-the-tests)
- [Project structure](#project-structure)
- [Troubleshooting](#troubleshooting)
- [Data sources and attribution](#data-sources-and-attribution)

## Project status

| Phase | Scope | Status |
| --- | --- | --- |
| **1. Data management** | Retrieval, upload, validation, storage, search, display, download | **Complete** |
| 2. Evaluation | TRACI characterization factors, indicators, weighting scenarios, multi-indicator scoring | Planned |

## Features

| Requirement | How epaData meets it |
| --- | --- |
| **Data retrieval** | Retrieval page calls the CAMPD API on the server with filters for reporting year, state, facility, fuel type, unit type and control technology. Facility attributes (county, coordinates, source category, owner/operator, operation and retirement dates) are fetched and merged. The API key lives only in the server environment. |
| **Retrieval provenance** | Each retrieval stores the endpoint, query parameters, date, record counts and reporting years, plus the raw response as a CSV file. |
| **File upload** | Drag-and-drop or file picker for `.csv` and `.xlsx`; extension and size (32 MB) checked; file read with pandas; columns mapped to the schema; preview and validation report; **approve or cancel**; original file kept. |
| **No silent discards** | Every rejected or modified row is recorded with its source row number, field, reason and the original row, and is downloadable as a report. |
| **Database** | SQLite via SQLAlchemy 2.0 and Alembic migrations, with foreign keys enforced, unique constraints on facility-unit and unit-year, CHECK constraints on impossible values, and indexes on searched columns. |
| **Search** | 13 basic filters, 6 range filters, multi-criteria combinations, top-N/bottom-N, facility-level totals, group-and-rank (e.g. top emitter per state), facility/unit comparison and historical (multi-year) search. Results are paginated, sortable, linked to detail pages, downloadable, and fully described by the URL. |
| **Description-based search** | Type a request such as *"Find coal units in Kentucky with high CO2 emissions"* in the explorer. A rule-based interpreter (no external service) turns it into ordinary search filters, shows how each phrase was read and which words it did not use, and fills in the filter form so the search can be adjusted. See [Description-based search](#description-based-search). |
| **Display** | Dashboard, retrieval, upload/review, explorer, compare, facility, unit, dataset and download pages. |
| **Download** | CSV (and Excel) for complete datasets, search results, selected facilities or units, valid records, invalid-record reports, warnings and provenance. |

## Quick start

Requires **Python 3.10+** (developed on 3.12) and Git.

```bash
git clone https://github.com/nbbakari/cs396project.git
cd cs396project

python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt

cp .env.example .env                 # Windows: Copy-Item .env.example .env

flask run                            # http://127.0.0.1:5000/
```

The local database (`data/epadata.db`) is created and migrated automatically
on the first page load (`flask db upgrade` does the same by hand). It starts
empty: load the team's shared data from the dashboard, or add your own from
the **Data Retrieval** and **Upload** pages ([Loading data](#loading-data)).

Use `flask run --port 5001` if port 5000 is taken (on macOS it is often used by
AirPlay Receiver).

## Reproducing the results

These steps need no CAMPD API key and no access to the team's shared
database: the data comes from the files in [`sample_data/`](sample_data/).
They were checked on Ubuntu 24.04 with Python 3.12 on a fresh virtual
environment and an empty database.

**System requirements:** Linux, macOS or Windows; Python 3.10 or newer; about
300 MB of disk for the virtual environment; internet access only for
`pip install` (and for the optional live retrieval in step 6).

1. **Get the code.** Unzip the submission (or `git clone
   https://github.com/nbbakari/cs396project.git`) and open a terminal in the
   project folder, the one containing `run.py`.
2. **Set up the environment.**

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate        # Windows: .venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   cp .env.example .env             # Windows: Copy-Item .env.example .env
   ```

   `.env` needs no changes for these steps. Its `SHARED_DATABASE_URL` line is
   a placeholder and is only used by the optional team features.
3. **Run the automated tests.** `python -m pytest`. Expected: `94 passed`.
4. **Start the app.** `flask run`, then open <http://127.0.0.1:5000/>. The
   database `data/epadata.db` is created on the first page load and starts
   empty.
5. **Load the sample data.** Open **File Intake & Validation**, drop in each
   file below, check the review page, and press **Approve import**:

   | File | Expected on the review page and after approval |
   | --- | --- |
   | `sample_data/campd_annual_emissions_2024.csv` | 4,024 rows, 4,024 imported, 0 rejected, 773 imported with a warning (units that did not run, whose blank metrics are recorded as 0) |
   | `sample_data/campd_annual_emissions_KY_2015-2020.csv` | 408 rows for 2015, 2018, 2019 and 2020, all imported, 61 with a warning |
   | `sample_data/sample_with_errors.csv` | 10 rows, 3 imported (2 with a warning), 7 rejected: one each of `missing_key`, `negative_value` and `duplicate_in_file`, two `invalid_value` and two `out_of_range` |

   The dashboard then shows 4,435 records for 1,333 facilities and 4,054
   units. Each dataset page shows its provenance and its data-quality report,
   and **Export & Downloads** offers the valid records, the invalid-record
   report and the provenance as CSV.
6. **Try the searches.** In **Data Explorer**:

   | Try | Expected |
   | --- | --- |
   | Type *Find coal units in Kentucky with high CO2 emissions* in the description box | Read as primary fuel "coal", state KY and CO₂ ≥ 2,620,000 short tons (the top quarter); 49 unit-years, led by Trimble County unit 2 in 2015 (5,327,182 tons) |
   | Example search *Top 10 facilities by CO₂ (2024)* | 10 facility totals for 2024, highest CO₂ first |
   | Example search *Top CO₂ facility in each state (2024)* | one facility per state |
   | Click a facility, then a unit | Location, owner, controls, the yearly history table and chart |

7. **Optional: live retrieval.** Open **Data Retrieval**, follow the link to
   request a free CAMPD API key (it arrives by email), paste it into the box,
   and retrieve a year that is not loaded yet (for example 2023). A national
   year takes 5 to 60 seconds and goes to the same review page.

To start again from an empty database, stop the app and delete
`data/epadata.db`.

## Team setup

The team keeps one **shared database** on Supabase with the common data
(2015–2025, about 47,000 records). Each person's app works on its **own local
SQLite file**: it is seeded from the shared database once, and after that
everything you retrieve or upload stays on your computer.

For a teammate starting from a fresh clone:

1. `git pull`, create the virtual environment and `pip install -r requirements.txt`.
2. In `.env`, set `SHARED_DATABASE_URL` to the Supabase connection string the
   team shares. (An old `DATABASE_URL=postgresql://...` line also works; the app
   reads it as the shared database and still stores its own data in SQLite.)
3. `flask run`, open <http://127.0.0.1:5000/> and press **Load shared data** on
   the dashboard (about a minute). Or run `flask data pull-shared`.
4. To retrieve from the EPA, open **Data Retrieval**: it links to the EPA's
   key request form and has a box to paste your key. No code or `.env` editing.

To publish your local data to the team (for example after loading a new
year), run `flask data push-shared`. It brings the shared schema up to date
and inserts only what the shared copy lacks, refreshing facility and unit
details; it never deletes. Uploaded files and data still awaiting review are
never published.

To start over from the shared data, stop the app, delete `data/epadata.db`
and start it again.

## Environment variables

Configuration is read from `.env` in the project root by `python-dotenv`.
`.env` is git-ignored and must never be committed.

| Variable | Required | Description |
| --- | --- | --- |
| `FLASK_APP` | Yes | Leave as `run.py`. |
| `FLASK_DEBUG` | No | `1` for the auto-reloading development server. |
| `SECRET_KEY` | Yes | Flask session signing key; any long random string. |
| `CAMPD_API_KEY` | For retrieval | Your EPA CAMPD API key. Usually set from the **Settings** page, which writes it here. Used only server-side, in a request header. |
| `SHARED_DATABASE_URL` | For team data | The team's Supabase connection string, used to seed an empty local database and by `flask data push-shared`. |
| `DATABASE_URL` | No | A different SQLite file for the app's own data. A PostgreSQL URL here is treated as `SHARED_DATABASE_URL` (older `.env` files). |
| `UPLOAD_FOLDER` | No | Where original files and API responses are kept. Default `data/uploads`. |
| `MAX_UPLOAD_MB` | No | Upload size limit in MB. Default `32`. |
| `CAMPD_API_BASE_URL` | No | Defaults to `https://api.epa.gov/easey`. |

**Getting a CAMPD API key:** open **Data Retrieval** (or **Settings**) in the
app. It links to the EPA's
[key request form](https://www.epa.gov/power-sector/cam-api-portal#request-api-key);
the key is free and arrives by email within a minute. Paste it into the box:
the app checks it with the EPA and saves it as `CAMPD_API_KEY` in this
computer's `.env`. Pages only ever show its last four characters; the key is
never sent to the browser, written to a URL or log, stored in the database, or
committed to Git.

## Loading data

### In the browser

1. **Data Retrieval** — choose a year and optional filters, then *Retrieve*.
   A full national year takes 10–60 seconds.
2. **File Intake & Validation** — drag in a CAMPD CSV/XLSX export.

Both lead to the **review page**: summary counts, column mapping, every issue
found, and a preview. Approve to import, or discard. Approved imports appear
under **Export & Downloads** with their provenance and quality report.

### From the command line

CLI imports skip the review page but use the same validation, so their
quality reports are complete.

```bash
flask campd fetch 2023 --state KY                 # one query
flask campd fetch 2023 --fuel Coal --unit-type "Tangentially-fired"
flask campd load-years 2015 2025                  # every year in a range
flask campd attributes 2016                       # fill blank county/location/dates
```

The CAMPD API allows about one request per second per key; the client spaces
its calls and retries rate-limited requests automatically.

### From the shared database

```bash
flask data pull-shared      # seed an empty local database (same as the dashboard button)
flask data push-shared      # publish this database to the shared one (asks to confirm)
```

Dataset IDs are preserved, so dataset numbers match across the team. A shared
database still on the original schema is also accepted; provenance records are
then created from what that schema stored.

## Pages

| Page | URL | Contents |
| --- | --- | --- |
| Dashboard | `/` | Introduction, coverage (facilities, units, records, states, years, rejected rows), per-year summary, data sources, navigation, recent imports |
| Data retrieval | `/retrieve` | CAMPD filters, retrieval status, pending reviews, retrieval history with query parameters and counts |
| Upload | `/upload` | Drag-and-drop upload, supported formats, required columns, validation rules |
| Review | `/review/<id>` | Validation report, column mapping, issues, preview, approve/discard |
| Data explorer | `/explorer` | Plain-English search box, search form, example searches, query description, sortable paginated results, selection, chart |
| Compare | `/compare` | Up to 8 units or facilities year by year on any metric, with chart and table |
| Facility | `/facility/<id>` | Location, units, annual totals, chart |
| Unit | `/unit/<key>` | Identification, fuel, operating dates, history chart, every annual record with controls, programs and source dataset |
| Dataset | `/dataset/<id>` | Provenance, counts, downloads, paginated data-quality report |
| Downloads | `/downloads` | Search, selection, dataset, report and provenance downloads |
| Settings | `/settings` | CAMPD API key (check, save, replace, remove), local database, loading shared data |

## Search reference

Every explorer search is a URL, so it can be bookmarked, shared or downloaded.
The **Download CSV** button on the explorer and `/download/records` accept the
same parameters and return exactly the rows the search shows.

**Basic filters.** Text filters ignore case. "Contains" filters also accept
`none` (no value), `any` (has a value) and `not <text>` (does not contain the
text, including blanks; e.g. `nox_control=not selective catalytic`).

| Parameter | Matches |
| --- | --- |
| `facility_id` | EPA facility ID(s), comma-separated |
| `facility_name`, `county` | contains |
| `unit_id` | EPA unit ID, exact |
| `state` | state code(s), comma-separated |
| `year` | year(s), comma-separated; or `year_min` / `year_max` |
| `primary_fuel`, `secondary_fuel`, `unit_type` | contains |
| `owner`, `so2_control`, `nox_control`, `pm_control`, `hg_control`, `program` | contains |
| `unit`, `dataset` | internal unit key(s) / dataset ID(s) |

**Ranges** (inclusive; use `_min` and/or `_max`): `op_time` (hours),
`gross_load` (MWh), `heat_input` (MMBtu), `co2`, `so2`, `nox` (short tons).

**Result shape**

| Parameter | Effect |
| --- | --- |
| `level=facility` | One row per facility-year, metrics summed over its units. Range filters then apply to the totals (operating time still filters units). |
| `sort`, `order` | Column and `asc`/`desc`. |
| `limit` | Top-N (`order=desc`) or bottom-N (`order=asc`). |
| `group_by`, `per_group` | Rank within each `state`, `year`, `primary_fuel`, `unit_type` or `facility`. |
| `page`, `per_page` | Pagination (10/25/50/100). |

**Examples** (also one click away under *Example searches* in the explorer):

| Question | Query string |
| --- | --- |
| Coal units in KY in 2025 with CO₂ > 500,000 tons | `state=KY&year=2025&primary_fuel=coal&co2_min=500000` |
| Top 10 facilities by CO₂ | `level=facility&year=2025&sort=co2_mass&limit=10` |
| Top 20 units by gross generation | `year=2025&sort=gross_load&limit=20` |
| Lowest-SO₂ units that ran > 5,000 h | `year=2025&op_time_min=5000&sort=so2_mass&order=asc&limit=10` |
| Top CO₂ facility in each state | `level=facility&year=2025&group_by=state&sort=co2_mass` |
| Highest-generation natural-gas units | `primary_fuel=natural gas&year=2025&sort=gross_load&limit=10` |
| One unit from 2015 to 2025 | `facility_id=6002&unit_id=1&year_min=2015&year_max=2025&sort=reporting_year&order=asc` |

The explorer also shows each search as a readable query, for example:

```
WHERE state_code = 'KY'
  AND reporting_year = 2025
  AND primary_fuel CONTAINS 'coal'
  AND co2_mass >= 500,000
ORDER BY co2_mass DESC
```

### Description-based search

The box at the top of the explorer accepts a plain-English request. The
explorer reads it as search parameters, redirects to that search's URL (which
keeps the description as `q`), and lists how each phrase was read, for example:

| Phrase | Read as |
| --- | --- |
| `coal` | Primary fuel contains "coal" |
| `Kentucky` | State is Kentucky (KY) |
| `high CO2 emissions` | CO₂ ≥ 2,680,000 short tons: the top quarter of matching unit-years |

The interpreter is rule-based (`app/services/describe.py`), so it runs offline,
gives the same answer every time, and every condition it adds is visible and
editable in the filter form. It recognises:

| Kind of phrase | Examples | Becomes |
| --- | --- | --- |
| States | `Kentucky`, `West Virginia`, `KY`, `IN and OH` | `state`; lower-case codes that are words (`in`, `or`, `me`) are left alone |
| Places and names | `Mercer County`, `named "Gaston"`, `facility 3`, `unit 1`, `owned by Duke Energy`, a leftover word that matches up to 25 facility names (`Barry`) | `county`, `facility_name`, `facility_id`, `unit_id`, `owner` |
| Fuels and unit types | `coal`, `natural gas`, `oil as backup fuel`, `combined cycle`, `peakers`, `boilers` | `primary_fuel`, `secondary_fuel`, `unit_type` |
| Controls and programs | `with SCR`, `without scrubbers`, `low NOx burners`, `baghouse`, `mercury controls`, `uncontrolled`, `RGGI`, `Acid Rain Program` | control filters (`any`, `none`, `not …`), `program` |
| Years | `2023`, `between 2015 and 2020`, `since 2020`, `before 2018`, `last year`, `last 3 years` | `year`, `year_min`, `year_max` (relative years count back from the latest year on file) |
| Quantities | `more than 5,000 hours`, `SO2 below 500 tons`, `CO2 over 1.5 million tons`, `between 100,000 and 1,000,000 MWh`, `heat input > 5,000,000 MMBtu` | range filters; the unit (tons, MWh, MMBtu, hours) or a named measure picks the column |
| Rankings | `top 10`, `highest NOx`, `10 largest CO2 emitters`, `lowest SO2`, `in each state`, `per fuel type`, `by gross load` | `sort`, `order`, `limit`, `group_by`, `per_group`; "highest" without a number returns 10 rows |
| Relative levels | `high CO2`, `low SO2 and NOx`, `high-emitting`, `large units` | a range at the top (or bottom) quarter of the rows the other conditions match, computed from the data |
| Size and level | `largest`, `big` (gross load); `plants`, `facilities` | facility totals (`level=facility`) unless units are mentioned |

Assumptions it states on the page when it makes them: "emissions" with no
pollutant named means CO₂; "lowest …" rankings and "low …" levels only count
units that operated, so idle units with zero emissions don't fill the list;
results can be ranked by one measure at a time. Mercury emissions are not in
the annual data, and words such as "cleanest" or "retired" have no matching
filter; these produce a short explanation instead of a guess.

## Validation rules

Implemented in `app/services/validation.py`. Row numbers count the header as row 1.

**Rejected** (not imported; listed in the report with the original row):

| Issue type | Rule |
| --- | --- |
| `missing_key` | Facility ID, unit ID or year is blank |
| `invalid_value` | Key is not a whole number, or a metric is not a number |
| `out_of_range` | Year before 1995 (start of CAMPD reporting) or in the future; operating time above 8,784 h |
| `negative_value` | Operating time, gross/steam load, heat input or CO₂/SO₂/NOₓ mass below 0 |
| `duplicate_in_file` | Same facility, unit and year as a later row (the later row is kept) |
| `already_stored` | That unit-year is already in the database (the stored record is kept) |

**Imported with a warning:**

| Issue type | Rule |
| --- | --- |
| `defaulted_to_zero` | A required metric was blank and recorded as 0 |
| `invalid_value` / `out_of_range` | Invalid state code, coordinate or date — left blank |
| `truncated` | Text longer than its database column |

A missing **required column** (facility ID, unit ID or year) blocks the whole
file; a missing metric column is shown on the review page before approval.

## Data model

```
Dataset 1──* AnnualRecord *──1 Unit *──1 Facility
   ├──1 DataProvenance
   ├──1 UploadedFile
   └──* DataQualityIssue
```

| Table | Purpose | Key constraints |
| --- | --- | --- |
| `datasets` | One import: name, source, reporting year, date, file name, raw/accepted counts, notes | |
| `facilities` | EPA facility ID, name, state, county, latitude, longitude, source category | PK = EPA facility ID |
| `units` | Internal key, facility, EPA unit ID, type, primary/secondary fuel, owner/operator, operating and retirement dates | unique (facility, EPA unit ID) |
| `annual_records` | One unit-year: operating time, gross/steam load, heat input, CO₂/SO₂/NOₓ mass, SO₂/NOₓ/PM/Hg controls, program codes | unique (unit, year); CHECK non-negative, year range, hours ≤ 8,784 |
| `uploaded_files` | The file as received (or saved API response): stored name, size, SHA-256, status `staged`/`imported`/`discarded` | |
| `data_provenance` | Per dataset: source, endpoint, query parameters, file hash, years, raw/accepted/rejected/warning counts | unique dataset |
| `data_quality_issues` | Per problem row: source row, severity, type, field, message, raw keys, original row (JSON) | index (dataset, severity) |

Foreign keys cascade on delete. SQLite does not enforce foreign keys by
default; the app turns enforcement on for every connection. Deleting a dataset
removes its records, report and stored file, then any units and facilities
left with no records.

Indexed for search: facility name, state and county; unit ID, type and primary
fuel; reporting year, operating time, gross load, heat input and CO₂/SO₂/NOₓ mass.

Schema changes are managed with Flask-Migrate:

```bash
flask db migrate -m "describe the change"   # review the script it writes!
flask db upgrade
flask db check                              # confirms models and migrations match
```

## Units, assumptions and limitations

**Units (as reported by CAMPD):** operating time in hours; gross load in MWh;
steam load in 1000 lb; heat input in MMBtu; CO₂, SO₂ and NOₓ mass in short tons.
Downloads put the unit in each column name (e.g. `co2_mass_short_tons`).

**Assumptions**

- CAMPD *apportioned annual* emissions are used: one row per unit per year.
- A blank required metric is recorded as 0 and flagged. CAMPD leaves metrics
  blank for units that did not operate, and leaves CO₂/SO₂ blank for units
  that report only NOₓ (e.g. ozone-season programs); in those cases 0 means
  "not reported", not "measured zero". The warning in the quality report marks them.
- A unit's type and fuels are those of the **latest** reporting year imported;
  per-year controls and program codes are kept on each annual record.
- Facility-level totals sum the units' values for a year. Operating time is
  not summed (it is per unit); in comparisons, facility operating time is the
  average across units.
- Retirement dates are parsed from CAMPD's `operatingStatus` text
  (e.g. "Operating (Retired 06/01/2020)").

**Limitations**

- Hourly data, mercury (Hg) and emission rates are not stored in Phase 1.
- Historical fuel switches (e.g. coal-to-gas) are not tracked per year on the unit.
- Retrieval runs during the web request (no background job queue); a full
  national year takes up to about a minute.
- Datasets imported before row-level quality tracking (copied from the earlier
  PostgreSQL database) have counts but no per-row issues or stored original file.
- There is no user authentication; the app is intended for local, single-team
  use. Anyone who can open the app can change its saved API key.
- The shared database is a starting point, not a live sync: data a teammate
  adds locally reaches others only when it is pushed and they reload.

## JSON API

All responses are JSON, including errors (`{"error": ..., "detail": ...}`).

| Endpoint | Description |
| --- | --- |
| `GET /api/emissions/records` | Explorer search as JSON; accepts every [search parameter](#search-reference); `400` lists invalid parameters |
| `GET /api/emissions/top-facilities?year=&limit=` | Facilities ranked by total CO₂ for one year (latest by default) |
| `GET /api/emissions/years` | Reporting years with data, newest first |
| `GET /api/emissions/interpret?q=` | Reads a plain-English description as search parameters; returns the parameters, how each phrase was read, unused words, and `explorer_url` / `records_url` for the same search |

See `CONNECT.md` for response shapes and front-end integration notes.

## Running the tests

```bash
pytest
```

The suite (94 tests) runs against an in-memory SQLite database and a temporary
upload folder, and mocks the CAMPD API, so it needs no network or `.env`. It
covers every validation rule, ingestion and re-import, database constraints,
the search builder (filters, ranges, top-N, facility totals, group-and-rank,
history), description-based search, the upload → review → approve/cancel
flow, retrieval, the API-key settings, a push/pull round trip through a shared
database, every page, and every download.

## Project structure

```
epaData/
├── app/
│   ├── __init__.py          # app factory, SQLite foreign keys, blueprints
│   ├── models.py            # SQLAlchemy models (7 tables)
│   ├── cli.py               # flask campd ..., flask data ...
│   ├── templating.py        # Jinja filters and URL helpers
│   ├── routes/
│   │   ├── main.py          # dashboard, explorer, compare, detail pages
│   │   ├── retrieval.py     # CAMPD retrieval page
│   │   ├── upload.py        # upload, review, approve, cancel
│   │   ├── download.py      # downloads page and CSV/Excel exports
│   │   ├── settings.py      # API key and shared-data settings
│   │   └── api.py           # JSON endpoints
│   ├── services/
│   │   ├── validation.py    # reading, column mapping, validation rules
│   │   ├── ingest.py        # staging, import, provenance, enrichment
│   │   ├── campd.py         # CAMPD API client
│   │   ├── search.py        # explorer query builder
│   │   ├── describe.py      # description-based search (plain English -> filters)
│   │   ├── shared.py        # push to / pull from the shared database
│   │   └── exports.py       # CSV / Excel responses
│   ├── templates/           # Jinja templates (Bootstrap 5)
│   └── static/              # CSS, JavaScript (Chart.js charts)
├── data/                    # epadata.db and uploads/ (git-ignored)
├── migrations/              # Alembic migrations
├── sample_data/             # CAMPD extracts and a validation demo file for trying the app
├── tests/                   # pytest suite
├── sync_campd.py            # legacy wrapper for `flask campd fetch`
├── requirements.txt
└── run.py
```

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `Could not locate a Flask application` | `.env` is missing or lacks `FLASK_APP=run.py`. |
| `no such table: ...` | Run `flask db upgrade`. |
| `Can't locate revision identified by ...` | Pull the latest `migrations/versions/`, then `flask db upgrade`. |
| Retrieval page asks for an API key | Paste your key into the box on that page (or set `CAMPD_API_KEY` in `.env`). |
| "The EPA rejected that key" | Copy the whole key from the EPA's email; it is 40 letters and digits. |
| "already has data, so shared data was not loaded" | Stop the app, delete `data/epadata.db`, start it again, and load. |
| Could not load the shared data | Check `SHARED_DATABASE_URL` (password percent-encoded if it has `@ : / ? #`). |
| `CAMPD API returned HTTP 429` | Rate limit; wait a minute and retry. |
| `CAMPD API returned HTTP 403` | The API key is wrong or inactive. |
| Upload says "larger than the upload limit" | Raise `MAX_UPLOAD_MB`, or split the file. |
| `ModuleNotFoundError` | Activate the virtual environment and `pip install -r requirements.txt`. |
| Charts don't appear | They load Chart.js from a CDN; check the browser's network connection. |

## Data sources and attribution

- U.S. EPA Clean Air Markets Program Data: <https://campd.epa.gov/>
  (API: <https://www.epa.gov/power-sector/cam-api-portal>)
- Planned (Phase 2 and later): EPA TRACI, eGRID, NEI, WebFIRE / AP-42.
