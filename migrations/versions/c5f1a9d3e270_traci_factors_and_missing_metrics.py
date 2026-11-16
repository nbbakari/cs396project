"""TRACI factors and missing-metric flags

Phase 2 analysis needs two things the Phase 1 schema lacks:

* ``traci_factors``: EPA TRACI characterization factors, seeded with the
  TRACI 2.2 air-emission factors that match CAMPD's CO2, SO2 and NOx. The
  values were read with app/services/traci.py from EPA's workbook
  https://www.epa.gov/system/files/documents/2024-01/traci_2_2.xlsx
  (TRACI 2.2, updated 2022-03-02; sha256 80383dec907d9c5de07aef521c2eda3f
  d156aaa3223e5f5e55fa0bc0d3725334).
* ``annual_records.missing_metrics``: which metrics were blank in the source
  row and stored as 0. Existing records are filled in from their
  "defaulted_to_zero" data-quality warnings.

Revision ID: c5f1a9d3e270
Revises: b7d2e4f6a813
Create Date: 2026-11-01

"""
import re
from collections import defaultdict
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c5f1a9d3e270'
down_revision = 'b7d2e4f6a813'
branch_labels = None
depends_on = None


def _source(column):
    return f'traci_2_2.xlsx, sheet Substances, column "{column}"'


_GWP = "Global Climate Air (kg CO2 eq / kg substance)"
_ACID = "Acidification Air (kg SO2 eq / kg substance)"
_PM = "HH Particulate Air (PM2.5 eq / kg substance)"
_SMOG = "Photochemical Oxidation Formation Air (kg O3 eq / kg substance)"

# (pollutant, CAS, CAMPD column, category, factor, unit, source column)
TRACI_2_2_AIR_FACTORS = [
    ("CARBON DIOXIDE", "124389", "co2_mass", "Global Climate", 1.0, "kg CO2 eq / kg substance", _GWP),
    ("SULFUR DIOXIDE", "7446095", "so2_mass", "Acidification", 1.0, "kg SO2 eq / kg substance", _ACID),
    ("SULFUR DIOXIDE", "7446095", "so2_mass", "HH Particulate", 0.06111111111111111, "PM2.5 eq / kg substance", _PM),
    ("NITROGEN OXIDES", None, "nox_mass", "Acidification", 0.7, "kg SO2 eq / kg substance", _ACID),
    ("NITROGEN OXIDES", None, "nox_mass", "HH Particulate", 0.007222222222222222, "PM2.5 eq / kg substance", _PM),
    ("NITROGEN OXIDES", None, "nox_mass", "Photochemical Oxidation Formation", 24.793589743589738, "kg O3 eq / kg substance", _SMOG),
]

# The labels app/services/validation.py writes into "defaulted_to_zero"
# messages: "Blank Gross load (MWh), CO2 mass (short tons) recorded as 0."
_METRIC_LABELS = {
    "Operating time (hours)": "operating_time",
    "Gross load (MWh)": "gross_load",
    "Heat input (MMBtu)": "heat_input",
    "CO2 mass (short tons)": "co2_mass",
    "SO2 mass (short tons)": "so2_mass",
    "NOx mass (short tons)": "nox_mass",
}
_METRIC_ORDER = list(_METRIC_LABELS.values())
_MESSAGE = re.compile(r"^Blank (?P<labels>.+) recorded as 0\.$")


def _key(value):
    """Issue keys are the file's raw text: "7", "7.0" and 7 are the same key."""
    text = str(value if value is not None else "").strip()
    return text[: text.index(".")] if re.fullmatch(r"\d+\.0+", text) else text


def _backfill_missing_metrics():
    bind = op.get_bind()
    blanks = {}
    for dataset_id, facility, unit, year, message in bind.execute(sa.text(
        "SELECT dataset_id, facility_id, epa_unit_id, reporting_year, message "
        "FROM data_quality_issues "
        "WHERE issue_type = 'defaulted_to_zero' AND severity = 'warning'"
    )):
        match = _MESSAGE.match((message or "").strip())
        if not match:
            continue
        found = {_METRIC_LABELS.get(label.strip()) for label in match["labels"].split(", ")}
        metrics = [m for m in _METRIC_ORDER if m in found]
        if metrics:
            blanks[(dataset_id, _key(facility), _key(unit), _key(year))] = ",".join(metrics)
    if not blanks:
        return

    ids_by_value = defaultdict(list)
    for record_id, dataset_id, facility, unit, year in bind.execute(sa.text(
        "SELECT r.id, r.dataset_id, u.facility_id, u.epa_unit_id, r.reporting_year "
        "FROM annual_records r JOIN units u ON u.internal_unit_key = r.unit_id"
    )):
        value = blanks.get((dataset_id, _key(facility), _key(unit), _key(year)))
        if value:
            ids_by_value[value].append(record_id)

    # One UPDATE per distinct value and chunk keeps this fast on a remote server.
    update = sa.text(
        "UPDATE annual_records SET missing_metrics = :value WHERE id IN :ids"
    ).bindparams(sa.bindparam("ids", expanding=True))
    for value, ids in ids_by_value.items():
        for start in range(0, len(ids), 1000):
            bind.execute(update, {"value": value, "ids": ids[start:start + 1000]})


def upgrade():
    traci = op.create_table(
        'traci_factors',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('version', sa.String(length=20), nullable=False),
        sa.Column('pollutant_name', sa.String(length=150), nullable=False),
        sa.Column('cas_number', sa.String(length=20), nullable=True),
        sa.Column('campd_metric', sa.String(length=20), nullable=False),
        sa.Column('medium', sa.String(length=20), nullable=False),
        sa.Column('impact_category', sa.String(length=100), nullable=False),
        sa.Column('factor_value', sa.Float(), nullable=False),
        sa.Column('factor_unit', sa.String(length=60), nullable=False),
        sa.Column('source', sa.Text(), nullable=True),
        sa.Column('imported_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('version', 'pollutant_name', 'medium', 'impact_category',
                            name='uq_traci_factor'),
    )
    with op.batch_alter_table('traci_factors', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_traci_factors_version'), ['version'], unique=False)
        batch_op.create_index(batch_op.f('ix_traci_factors_campd_metric'), ['campd_metric'], unique=False)

    now = datetime.now(timezone.utc)
    op.bulk_insert(traci, [
        {
            "version": "2.2",
            "pollutant_name": name,
            "cas_number": cas,
            "campd_metric": metric,
            "medium": "air",
            "impact_category": category,
            "factor_value": value,
            "factor_unit": unit,
            "source": _source(column),
            "imported_at": now,
        }
        for name, cas, metric, category, value, unit, column in TRACI_2_2_AIR_FACTORS
    ])

    with op.batch_alter_table('annual_records', schema=None) as batch_op:
        batch_op.add_column(sa.Column('missing_metrics', sa.String(length=100), nullable=True))
    _backfill_missing_metrics()


def downgrade():
    with op.batch_alter_table('annual_records', schema=None) as batch_op:
        batch_op.drop_column('missing_metrics')

    with op.batch_alter_table('traci_factors', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_traci_factors_campd_metric'))
        batch_op.drop_index(batch_op.f('ix_traci_factors_version'))

    op.drop_table('traci_factors')
