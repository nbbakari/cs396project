"""Landing page and dashboard pages for epaData."""

from flask import Blueprint, flash, redirect, render_template, url_for
from sqlalchemy import func

from app import db
from app.models import AnnualRecord, Dataset, Facility, Unit

main_bp = Blueprint("main", __name__)


@main_bp.route("/")
def index():
    """Render the dashboard landing page: coverage stats + recent datasets."""
    facility_count = db.session.query(func.count(Facility.epa_facility_id)).scalar() or 0
    unit_count = db.session.query(func.count(Unit.internal_unit_key)).scalar() or 0
    record_count = db.session.query(func.count(AnnualRecord.id)).scalar() or 0
    state_count = (
        db.session.query(func.count(func.distinct(Facility.state_code)))
        .filter(Facility.state_code != "")
        .scalar()
        or 0
    )
    year_min, year_max = db.session.query(
        func.min(AnnualRecord.reporting_year), func.max(AnnualRecord.reporting_year)
    ).one()

    coverage = {
        "facility_count": facility_count,
        "unit_count": unit_count,
        "record_count": record_count,
        "state_count": state_count,
        "year_min": year_min,
        "year_max": year_max,
    }

    datasets = Dataset.query.order_by(Dataset.retrieval_date.desc()).limit(10).all()

    return render_template("index.html", coverage=coverage, datasets=datasets)


@main_bp.route("/explorer")
def explorer():
    """Render the data explorer, which draws its data from the JSON API."""
    return render_template("explorer.html")

@main_bp.route("/dataset/<int:dataset_id>/delete", methods=["POST"])
def delete_dataset(dataset_id):
    """Delete an imported dataset and its annual records.

    Facility and Unit rows are left in place, since other datasets may still
    reference them -- only records that trace back to this specific import
    are removed (via the Dataset -> AnnualRecord cascade in the model).
    """
    dataset = Dataset.query.get_or_404(dataset_id)
    name = dataset.dataset_name
    count = dataset.accepted_records
    db.session.delete(dataset)
    db.session.commit()
    flash(f"Deleted dataset '{name}' and its {count:,} record(s).", "info")
    return redirect(url_for("main.index"))
