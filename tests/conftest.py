"""Shared fixtures: an app on an in-memory SQLite database and sample data."""

import io

import pytest

from app import create_app, db
from app.services.ingest import ingest
from app.services.validation import read_table, validate

HEADER = (
    "Facility Name,Facility ID,Unit ID,Year,State,County,Gross Load (MWh),"
    "Heat Input (mmBtu),CO2 Mass (short tons),SO2 Mass (short tons),"
    "NOx Mass (short tons),Operating Time,Primary Fuel Type,Unit Type,SO2 Controls"
)

# Each row is labelled with what validation should do with it. File row
# numbers start at 2 because row 1 is the header.
SAMPLE_ROWS = [
    # row 2: superseded by row 8 (same unit-year) -> rejected duplicate_in_file
    "Plant A,100,1,2022,KY,Mercer County,1000,5000,400000,10,5,6000,Coal,Tangentially-fired,Wet Limestone",
    # row 3: blank metrics -> imported with a defaulted_to_zero warning
    "Plant A,100,2,2022,KY,Mercer County,,,,,,,Pipeline Natural Gas,Combined cycle,",
    # row 4: blank facility ID -> rejected missing_key
    "Plant B,,1,2022,KY,,100,100,100,1,1,100,Coal,Stoker,",
    # row 5: unparseable year -> rejected invalid_value
    "Plant B,200,1,abc,KY,,100,100,100,1,1,100,Coal,Stoker,",
    # row 6: negative gross load -> rejected negative_value
    "Plant B,200,1,2022,KY,,-5,100,100,1,1,100,Coal,Stoker,",
    # row 7: operating time above 8,784 h -> rejected out_of_range
    "Plant C,300,1,2022,WV,,100,100,100,1,1,9000,Coal,Stoker,",
    # row 8: valid, supersedes row 2
    "Plant A,100,1,2022,KY,Mercer County,2000,9000,600000,20,8,7000,Coal,Tangentially-fired,Wet Limestone",
    # row 9: year before CAMPD reporting began -> rejected out_of_range
    "Plant D,400,1,1990,KY,,100,100,100,1,1,100,Coal,Stoker,",
    # row 10: state not a code -> imported with an invalid_value warning
    "Plant E,500,1,2022,Kentucky,,100,100,200,1,1,100,Natural Gas,Combustion turbine,",
    # row 11: non-numeric gross load -> rejected invalid_value
    "Plant F,600,1,2022,KY,,lots,100,100,1,1,100,Coal,Stoker,",
]


def sample_csv(rows=SAMPLE_ROWS) -> bytes:
    return ("\n".join([HEADER, *rows]) + "\n").encode("utf-8")


@pytest.fixture
def app(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": "sqlite://",
            "UPLOAD_FOLDER": str(tmp_path / "uploads"),
            "CAMPD_API_KEY": "test-key",
            "SECRET_KEY": "test",
            # Never touch the developer's real .env or shared database.
            "ENV_FILE": str(tmp_path / ".env"),
            "SHARED_DATABASE_URL": None,
        }
    )
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def sample_dataset(app):
    """The sample CSV, validated and imported."""
    result = validate(read_table(io.BytesIO(sample_csv()), "csv"))
    return ingest(
        result,
        dataset_name="sample",
        data_source="CSV upload",
        source_type="upload",
        original_filename="sample.csv",
    )
