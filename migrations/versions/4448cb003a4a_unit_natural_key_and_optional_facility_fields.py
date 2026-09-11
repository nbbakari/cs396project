"""Unit natural key and optional facility fields

Adds a unique constraint on (facility_id, epa_unit_id) so ingestion can look a
unit up by its natural key instead of duplicating it on every re-upload, and
relaxes the facility columns that CAMPD does not always populate.

Revision ID: 4448cb003a4a
Revises: 7b41b09d1309
Create Date: 2026-09-06

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '4448cb003a4a'
down_revision = '7b41b09d1309'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('units', schema=None) as batch_op:
        batch_op.create_unique_constraint(
            'uq_facility_epa_unit', ['facility_id', 'epa_unit_id']
        )

    with op.batch_alter_table('facilities', schema=None) as batch_op:
        batch_op.alter_column('county',
               existing_type=sa.String(length=100),
               nullable=True)
        batch_op.alter_column('latitude',
               existing_type=sa.Float(),
               nullable=True)
        batch_op.alter_column('longitude',
               existing_type=sa.Float(),
               nullable=True)
        batch_op.alter_column('source_category',
               existing_type=sa.String(length=150),
               nullable=True)


def downgrade():
    # Rows ingested while the columns were nullable may hold NULLs, which would
    # block the NOT NULL restore; backfill before downgrading.
    with op.batch_alter_table('facilities', schema=None) as batch_op:
        batch_op.alter_column('source_category',
               existing_type=sa.String(length=150),
               nullable=False)
        batch_op.alter_column('longitude',
               existing_type=sa.Float(),
               nullable=False)
        batch_op.alter_column('latitude',
               existing_type=sa.Float(),
               nullable=False)
        batch_op.alter_column('county',
               existing_type=sa.String(length=100),
               nullable=False)

    with op.batch_alter_table('units', schema=None) as batch_op:
        batch_op.drop_constraint('uq_facility_epa_unit', type_='unique')
