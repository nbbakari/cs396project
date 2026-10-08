"""The API-key settings and the shared (team) database."""

import pytest
from sqlalchemy import create_engine, func, select, text

from app import create_app, db
from app.models import AnnualRecord, DataProvenance, DataQualityIssue, Dataset, Facility
from app.routes import settings as settings_routes
from app.services.campd import CampdError
from app.services.shared import SharedDataError, pull_shared, push_shared

GOOD_KEY = "AbCdEf0123456789AbCdEf0123456789AbCdWXYZ"


@pytest.fixture
def no_key(app, monkeypatch):
    # The routes write os.environ; monkeypatch restores it after each test.
    monkeypatch.delenv("CAMPD_API_KEY", raising=False)
    app.config["CAMPD_API_KEY"] = None
    return app


# --- API key -----------------------------------------------------------------


def test_retrieval_page_prompts_for_a_key(client, no_key):
    html = client.get("/retrieve").get_data(as_text=True)
    assert "Connect your CAMPD API key" in html
    assert "cam-api-portal#request-api-key" in html
    assert 'name="api_key"' in html


def test_saving_a_key_writes_env_and_never_shows_it(client, no_key, monkeypatch, tmp_path):
    monkeypatch.setattr(settings_routes, "verify_api_key", lambda key: None)
    response = client.post(
        "/settings/api-key", data={"api_key": f"  {GOOD_KEY} ", "next": "/retrieve"}
    )
    assert response.headers["Location"].endswith("/retrieve")
    assert (tmp_path / ".env").read_text().strip() == f"CAMPD_API_KEY={GOOD_KEY}"
    assert no_key.config["CAMPD_API_KEY"] == GOOD_KEY

    for url in ("/retrieve", "/settings"):
        html = client.get(url).get_data(as_text=True)
        assert GOOD_KEY not in html
        assert "WXYZ" in html  # the last four characters, masked


def test_key_rejected_by_epa_is_not_saved(client, no_key, monkeypatch, tmp_path):
    def reject(key):
        raise CampdError("The CAMPD API returned HTTP 403: API_KEY_INVALID", status=403)

    monkeypatch.setattr(settings_routes, "verify_api_key", reject)
    client.post("/settings/api-key", data={"api_key": GOOD_KEY})
    assert not (tmp_path / ".env").exists()
    assert no_key.config["CAMPD_API_KEY"] is None


def test_malformed_key_is_not_saved(client, no_key, tmp_path):
    client.post("/settings/api-key", data={"api_key": "my key!"})
    assert not (tmp_path / ".env").exists()


def test_next_cannot_leave_the_site(client, no_key, monkeypatch):
    monkeypatch.setattr(settings_routes, "verify_api_key", lambda key: None)
    response = client.post(
        "/settings/api-key", data={"api_key": GOOD_KEY, "next": "//evil.example/"}
    )
    assert response.headers["Location"].endswith("/settings")


def test_removing_the_key(client, app, tmp_path, monkeypatch):
    monkeypatch.delenv("CAMPD_API_KEY", raising=False)
    (tmp_path / ".env").write_text(f"CAMPD_API_KEY={GOOD_KEY}\nOTHER=1\n")
    client.post("/settings/api-key/remove")
    assert (tmp_path / ".env").read_text() == "OTHER=1\n"
    assert app.config["CAMPD_API_KEY"] is None


# --- shared database ---------------------------------------------------------


def _counts():
    return {
        model.__tablename__: db.session.scalar(select(func.count()).select_from(model))
        for model in (Dataset, Facility, AnnualRecord, DataProvenance, DataQualityIssue)
    }


def test_push_then_pull_round_trip(app, sample_dataset, tmp_path):
    shared_url = f"sqlite:///{tmp_path / 'shared.db'}"
    expected = _counts()

    summary = push_shared(shared_url)
    assert summary["annual_records"] == (0, expected["annual_records"])
    # Pushing again adds nothing.
    again = push_shared(shared_url)
    assert all(before == after for before, after in again.values())

    teammate = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": "sqlite://",
            "UPLOAD_FOLDER": str(tmp_path / "teammate-uploads"),
            "ENV_FILE": str(tmp_path / "teammate.env"),
            "SHARED_DATABASE_URL": shared_url,
        }
    )
    with teammate.app_context():
        db.create_all()
        pull_shared(shared_url)
        assert _counts() == expected
        # A second pull would mix ID sets, so it is refused.
        with pytest.raises(SharedDataError):
            pull_shared(shared_url)
        db.session.remove()
        db.drop_all()


def test_push_refreshes_facility_attributes(app, sample_dataset, tmp_path):
    shared_url = f"sqlite:///{tmp_path / 'shared.db'}"
    push_shared(shared_url)
    db.session.get(Facility, 500).county = "Fayette County"
    db.session.commit()
    push_shared(shared_url)
    engine = create_engine(shared_url)
    with engine.connect() as connection:
        county = connection.execute(
            text("SELECT county FROM facilities WHERE epa_facility_id = 500")
        ).scalar()
    engine.dispose()
    assert county == "Fayette County"


def test_dashboard_offers_shared_data_and_loads_it(client, app, sample_dataset, tmp_path):
    shared_url = f"sqlite:///{tmp_path / 'shared.db'}"
    push_shared(shared_url)
    # Empty this database, as on a teammate's fresh checkout.
    client.post(f"/dataset/{sample_dataset.id}/delete")
    app.config["SHARED_DATABASE_URL"] = shared_url

    html = client.get("/").get_data(as_text=True)
    assert "Load shared data" in html
    client.post("/settings/shared-data/load")
    assert db.session.scalar(select(func.count(AnnualRecord.id))) == 3


def test_old_env_database_url_becomes_the_shared_source(monkeypatch):
    from app import _database_urls

    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pw@host/db")
    monkeypatch.delenv("SHARED_DATABASE_URL", raising=False)
    local, shared = _database_urls()
    assert local.startswith("sqlite:///")
    assert shared == "postgresql://user:pw@host/db"
