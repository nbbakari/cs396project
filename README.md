# epaData

A Flask web application for retrieving, storing, and managing emissions data
published by the U.S. Environmental Protection Agency (EPA).

The application ingests unit-level emissions and operating data from the EPA's
**Clean Air Markets Program Data (CAMPD)** service and normalises it into a
relational database, so that facility, unit, and annual emissions figures can be
queried consistently across reporting years.

## Project status — Phase 1

Phase 1 is scoped to **data management and retrieval**: establishing the database
schema, the ingestion pipeline, and the provenance tracking that later phases
build on. Analysis, visualisation, and the public-facing interface are out of
scope for this phase.

Phase 1 delivers:

- A normalised SQLAlchemy schema covering datasets, facilities, units, and annual records
- Versioned schema migrations via Flask-Migrate / Alembic
- Retrieval of CAMPD extracts (API and bulk CSV/Excel files)
- Ingestion provenance — every annual record is traceable to the dataset it came from
- Data integrity constraints preventing duplicate annual entries per unit

## Data model

| Model | Table | Purpose |
| --- | --- | --- |
| `Dataset` | `datasets` | One ingestion run: source, reporting year, retrieval date, and accepted/rejected record counts |
| `Facility` | `facilities` | A plant, keyed by its EPA-assigned facility ID, with location and source category |
| `Unit` | `units` | A generating unit belonging to a facility, with fuel types and service dates |
| `AnnualRecord` | `annual_records` | Annual operating and emissions totals (CO₂, SO₂, NOₓ) for one unit |

```
Dataset  1 ──── * AnnualRecord * ──── 1  Unit  * ──── 1  Facility
```

A unique constraint on `(unit_id, reporting_year)` guarantees that a unit cannot
have two annual records for the same year.

## Project structure

```
epaData/
├── app/
│   ├── __init__.py        # Application factory, extension setup
│   └── models.py          # SQLAlchemy models
├── data/                  # Local database and downloaded source files
├── migrations/            # Alembic migration scripts
├── tests/                 # Pytest suite
├── .env.example           # Template for local environment variables
├── requirements.txt
└── run.py                 # Development entry point
```

## Prerequisites

- **Python 3.10 or later** (developed and tested on Python 3.12)
- `pip` and `venv` — both ship with the standard CPython distribution
- Git
- A **CAMPD API key** — free, see [Environment variables](#environment-variables)
- Access to the project's **Supabase PostgreSQL** database (connection string and password)

> Python 3.10 is the minimum because the codebase uses PEP 604 union type
> syntax (`dict | None`).

## Installation and setup

### 1. Clone the repository

```bash
git clone https://github.com/nbbakari/cs396project.git
cd cs396project
```

### 2. Create and activate a virtual environment

**macOS / Linux**

```bash
python3 -m venv .venv
source .venv/bin/activate
```

**Windows (PowerShell)**

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
```

Your shell prompt should now be prefixed with `(.venv)`. Run every command below
with the environment active; use `deactivate` to exit it.

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

## Environment variables

Configuration is read from a `.env` file in the project root, which is loaded at
startup by `python-dotenv`. Copy the tracked template to create your own:

**macOS / Linux**

```bash
cp .env.example .env
```

**Windows (PowerShell)**

```powershell
Copy-Item .env.example .env
```

Then open `.env` and set the values:

| Variable | Required | Description |
| --- | --- | --- |
| `FLASK_APP` | Yes | Entry point for the Flask CLI. Leave as `run.py`. |
| `FLASK_DEBUG` | No | Set to `1` for the auto-reloading development server. |
| `SECRET_KEY` | Yes | Flask session signing key. Use any long random string. |
| `CAMPD_API_KEY` | Yes | Your EPA CAMPD API key (see below). |
| `DATABASE_URL` | Yes | PostgreSQL connection string for the Supabase database. The app refuses to start without it. |
| `CAMPD_API_BASE_URL` | No | CAMPD API root. Defaults to `https://api.epa.gov/easey`. |

### Database connection

`DATABASE_URL` points at the project's Supabase PostgreSQL instance via the
session pooler:

```dotenv
DATABASE_URL=postgresql://postgres.qzoamxjwkisdvhzjyqfx:[YOUR-PASSWORD]@aws-0-us-east-1.pooler.supabase.com:5432/postgres
```

Replace `[YOUR-PASSWORD]` — brackets included — with the database password from
the Supabase dashboard (**Project Settings → Database**). Edit this in your local
`.env` only. `.env.example` is committed to the repository, so a password written
there would be published to GitHub on your next push.

Port `5432` is the pooler's session mode, which is what migrations need. Port
`6543` is transaction mode and does not support the prepared statements
SQLAlchemy issues by default.

#### Password encoding

If the password contains any of `@ : / ? # [ ] %`, it must be percent-encoded or
the URL will be misparsed — typically surfacing as
`ValueError: invalid literal for int() with base 10`. Generate an encoded value
with:

```bash
python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1], safe=''))" 'your password'
```

Paste the result in place of `[YOUR-PASSWORD]`. Passwords made only of letters
and digits need no encoding.

### Obtaining a CAMPD API key

1. Visit the EPA **CAM API Portal**: <https://www.epa.gov/power-sector/cam-api-portal>
2. Register for an API key using your email address — the key is free and issued immediately.
3. Paste the key into `.env`:

   ```dotenv
   CAMPD_API_KEY=your-actual-key-here
   ```

`.env` is listed in `.gitignore` and must **never** be committed. Only
`.env.example`, which holds placeholders, belongs in version control.

## Database initialisation

With the virtual environment active and `.env` in place, apply the migrations to
build the schema:

```bash
flask db upgrade
```

This creates all four tables in the Supabase database at the latest revision. No
local PostgreSQL install is needed — the connection is made to Supabase directly.

After changing a model in `app/models.py`, generate and apply a new migration:

```bash
flask db migrate -m "describe your change"
flask db upgrade
```

Always review the generated script in `migrations/versions/` before applying it —
Alembic's autogenerate is a starting point, not a guarantee.

Useful additional commands:

```bash
flask db current    # show the revision the database is on
flask db history    # list all revisions
flask db downgrade  # roll back one revision
```

## Running the development server

```bash
flask run
```

The application starts at <http://127.0.0.1:5000/>, which returns a JSON health
check:

```json
{ "application": "epaData", "status": "ok" }
```

With `FLASK_DEBUG=1` set in `.env`, the server reloads automatically on code
changes. Use `flask run --port 5001` if port 5000 is already in use — on macOS it
is often occupied by AirPlay Receiver.

Alternatively, run the entry point directly:

```bash
python run.py
```

### Interactive shell

`flask shell` opens a Python shell with the application context and all models
already imported:

```bash
flask shell
>>> Facility.query.count()
```

## Running the tests

```bash
pytest
```

The suite is empty as of Phase 1 — `pytest` will report `no tests ran` until the
first test module lands in `tests/`. Tests use `pytest-flask`, which expects an
`app` fixture in `tests/conftest.py`; the application factory accepts a config
override for this, so a fixture can point it at an in-memory database:

```python
create_app({"SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
```

## Technology stack

| Component | Library |
| --- | --- |
| Web framework | Flask |
| ORM | Flask-SQLAlchemy (SQLAlchemy 2.0 declarative syntax) |
| Migrations | Flask-Migrate (Alembic) |
| Configuration | python-dotenv |
| Data processing | pandas, openpyxl |
| Testing | pytest, pytest-flask |
| Database | PostgreSQL (Supabase) |

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `Error: Could not locate a Flask application` | `FLASK_APP` is unset — confirm `.env` exists and contains `FLASK_APP=run.py`. |
| `Error: Can't locate revision identified by ...` | The database's `alembic_version` row references a revision not in `migrations/versions/`. Pull the latest migrations before running `flask db upgrade`. |
| `Target database is not up to date` on `flask db migrate` | Run `flask db upgrade` first, then retry the migration. |
| `ModuleNotFoundError: No module named 'flask'` | The virtual environment is not active, or dependencies are not installed. |
| Port 5000 already in use | Run `flask run --port 5001`. |
| `RuntimeError: DATABASE_URL is not set` | `.env` is missing or has no `DATABASE_URL`. Copy `.env.example` to `.env` and fill it in. |
| `ModuleNotFoundError: No module named 'psycopg2'` | The PostgreSQL driver is missing. Run `pip install -r requirements.txt`. |
| `ValueError: invalid literal for int() with base 10` | The password in `DATABASE_URL` contains characters that need percent-encoding — see [Password encoding](#password-encoding). |
| `password authentication failed for user` | `[YOUR-PASSWORD]` was never replaced, or the password is wrong. Reset it in the Supabase dashboard under **Project Settings → Database**. |

## Data source and attribution

Emissions data is published by the U.S. Environmental Protection Agency through
the Clean Air Markets Program Data service: <https://campd.epa.gov/>

---

Built for CS396 coursework.
