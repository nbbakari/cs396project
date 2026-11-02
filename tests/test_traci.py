"""TRACI factors: reading EPA's workbook, matching only CAMPD's pollutants
exactly, storing them, the CLI, the JSON endpoint and the seed migration."""

from pathlib import Path

import openpyxl
import pytest
from sqlalchemy import create_engine, text

from app import BASE_DIR, create_app, db
from app.services.traci import TraciError, factors_for, read_workbook, save_factors

ACID = "Acidification Air (kg SO2 eq / kg substance)"
GWP = "Global Climate Air (kg CO2 eq / kg substance)"
PM = "HH Particulate Air (PM2.5 eq / kg substance)"
SMOG = "Photochemical Oxidation Formation Air (kg O3 eq / kg substance)"
HEADER = ["CAS #", "Formatted CAS #", "Substance Name", ACID,
          "Ecotox. CF [CTUeco/kg], Em.airU, freshwater", GWP, PM, SMOG]

# The same layout and values as EPA's traci_2_2.xlsx for these substances.
SUBSTANCES = [
    ["124389", "124389", "CARBON DIOXIDE", "n/a", 7.5, 1, "n/a", "n/a"],
    ["7446095", "7446095", "SULFUR DIOXIDE", 1, "n/a", "n/a", 0.06111111111111111, "n/a"],
    ["x", "x", "NITROGEN OXIDES", 0.7, "n/a", "n/a", 0.007222222222222222, 24.793589743589738],
    # Listed separately in TRACI, with a different smog factor: never used for NOx.
    ["10102440", "10102440", "NITROGEN DIOXIDE", 0.7, "n/a", "n/a", 0.0072, 16.845333333333333],
    ["74828", "74828", "METHANE", "n/a", "n/a", 25, "n/a", 0.0144],
]


def make_workbook(path, substances=SUBSTANCES, title="TRACI 2.2"):
    workbook = openpyxl.Workbook()
    workbook.active.title = "TRACI_2_2"
    workbook.active["A1"] = title
    sheet = workbook.create_sheet("Substances")
    sheet.append(HEADER)
    for row in substances:
        sheet.append(row)
    workbook.save(path)
    return path


@pytest.fixture
def workbook(tmp_path):
    return make_workbook(tmp_path / "traci_2_2.xlsx")


def test_reads_only_exact_air_factors_for_campd_pollutants(workbook):
    factors = {(f["pollutant_name"], f["impact_category"]): f for f in read_workbook(workbook)}
    assert set(factors) == {
        ("CARBON DIOXIDE", "Global Climate"),
        ("SULFUR DIOXIDE", "Acidification"),
        ("SULFUR DIOXIDE", "HH Particulate"),
        ("NITROGEN OXIDES", "Acidification"),
        ("NITROGEN OXIDES", "HH Particulate"),
        ("NITROGEN OXIDES", "Photochemical Oxidation Formation"),
    }
    smog = factors[("NITROGEN OXIDES", "Photochemical Oxidation Formation")]
    assert smog["factor_value"] == pytest.approx(24.793589743589738, rel=1e-12)  # not NO2
    assert smog["campd_metric"] == "nox_mass" and smog["cas_number"] is None
    assert smog["factor_unit"] == "kg O3 eq / kg substance"
    gwp = factors[("CARBON DIOXIDE", "Global Climate")]
    assert (gwp["version"], gwp["medium"], gwp["cas_number"]) == ("2.2", "air", "124389")
    assert SMOG in smog["source"]


def test_version_comes_from_the_workbook_or_the_caller(tmp_path):
    path = make_workbook(tmp_path / "t.xlsx", title="Tool for the Reduction ...")
    assert read_workbook(path)[0]["version"] == "2.2"  # from the sheet name TRACI_2_2
    assert read_workbook(path, version="2.1")[0]["version"] == "2.1"


def test_wrong_cas_number_is_refused(tmp_path):
    rows = [list(r) for r in SUBSTANCES]
    rows[0][0] = "999"
    with pytest.raises(TraciError, match="CAS"):
        read_workbook(make_workbook(tmp_path / "t.xlsx", rows))


def test_missing_substance_is_refused(tmp_path):
    with pytest.raises(TraciError, match="NITROGEN OXIDES"):
        read_workbook(make_workbook(tmp_path / "t.xlsx", SUBSTANCES[:2]))


def test_not_a_workbook_is_refused(tmp_path):
    path = tmp_path / "t.xlsx"
    path.write_text("not excel")
    with pytest.raises(TraciError):
        read_workbook(path)


def test_saving_twice_changes_nothing(app, workbook):
    factors = read_workbook(workbook)
    assert save_factors(factors) == (6, 0)
    assert save_factors(factors) == (0, 0)
    factors[0]["factor_value"] = 2.0
    assert save_factors(factors) == (0, 1)
    assert len(factors_for("2.2")) == 6


def test_cli_load_and_list(app, workbook):
    runner = app.test_cli_runner()
    result = runner.invoke(args=["traci", "load", str(workbook)])
    assert result.exit_code == 0, result.output
    assert "6 factors read, 6 added, 0 updated" in result.output
    listing = runner.invoke(args=["traci", "list"]).output
    assert "NITROGEN OXIDES" in listing and "24.79358974" in listing


def test_factors_endpoint(client, app, workbook):
    assert client.get("/api/traci/factors").status_code == 404
    save_factors(read_workbook(workbook))
    body = client.get("/api/traci/factors").get_json()
    assert body["version"] == "2.2" and len(body["factors"]) == 6
    labels = {f["impact_label"] for f in body["factors"]}
    assert "Global warming" in labels and "Smog formation" in labels


def upgrade_to(url, revision="head"):
    from flask_migrate import upgrade

    other = create_app({"SQLALCHEMY_DATABASE_URI": url, "AUTO_MIGRATE": False})
    with other.app_context():
        upgrade(directory=str(Path(BASE_DIR) / "migrations"), revision=revision)
        db.engine.dispose()


def test_migration_seeds_traci_2_2_and_flags_old_blank_metrics(app, tmp_path):
    """Upgrade a database with Phase 1 data to the Phase 2 schema."""
    url = f"sqlite:///{tmp_path / 'old.db'}"
    upgrade_to(url, "b7d2e4f6a813")  # the last Phase 1 schema
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO datasets (id, dataset_name, data_source, reporting_year, "
            "retrieval_date, raw_records, accepted_records) "
            "VALUES (1, 'd', 'CAMPD API', 2022, '2022-01-01', 2, 2)"))
        conn.execute(text("INSERT INTO facilities (epa_facility_id, facility_name, state_code) "
                          "VALUES (7, 'P', 'KY')"))
        for key, unit in ((1, "1"), (2, "CT1")):
            conn.execute(text(
                "INSERT INTO units (internal_unit_key, facility_id, epa_unit_id, unit_type, "
                "primary_fuel) VALUES (:k, 7, :u, 'Stoker', 'Coal')"), {"k": key, "u": unit})
            conn.execute(text(
                "INSERT INTO annual_records (id, unit_id, dataset_id, reporting_year, "
                "operating_time, gross_load, heat_input, co2_mass, so2_mass, nox_mass) "
                "VALUES (:k, :k, 1, 2022, 10, 0, 5, 0, 0, 1)"), {"k": key})
        conn.execute(text(
            "INSERT INTO data_quality_issues (dataset_id, source_row, severity, issue_type, "
            "message, facility_id, epa_unit_id, reporting_year) VALUES (1, 3, 'warning', "
            "'defaulted_to_zero', 'Blank Gross load (MWh), CO2 mass (short tons) recorded as 0.', "
            "'7', 'CT1', '2022')"))
    engine.dispose()

    upgrade_to(url)
    engine = create_engine(url)
    with engine.connect() as conn:
        flags = dict(conn.execute(text("SELECT id, missing_metrics FROM annual_records")).all())
        factors = conn.execute(text(
            "SELECT pollutant_name, impact_category, factor_value FROM traci_factors "
            "WHERE version = '2.2'")).all()
    engine.dispose()
    assert flags == {1: None, 2: "gross_load,co2_mass"}
    assert len(factors) == 6
    assert ("NITROGEN OXIDES", "Photochemical Oxidation Formation", 24.793589743589738) in factors


def test_builder_page_lists_the_factors(client, app, workbook):
    save_factors(read_workbook(workbook))
    html = client.get("/analysis").get_data(as_text=True)
    assert "EPA TRACI 2.2" in html
    assert "NITROGEN OXIDES" in html and "Smog formation" in html
