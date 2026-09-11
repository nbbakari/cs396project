"""Landing page and dashboard pages for epaData."""

from flask import Blueprint, render_template

main_bp = Blueprint("main", __name__)


@main_bp.route("/")
def index():
    """Render the dashboard landing page."""
    return render_template("index.html")


@main_bp.route("/explorer")
def explorer():
    """Render the data explorer, which draws its data from the JSON API."""
    return render_template("explorer.html")
