"""Provenance, data-quality tables, integrity checks and search indexes

Adds the tables Phase 1 needs to keep every import traceable and every
rejected row reportable:

* uploaded_files       -- the original file / API response as received
* data_provenance      -- source, query parameters and counts per dataset
* data_quality_issues  -- every rejected or flagged source row

and indexes the columns the data explorer filters and sorts on, plus CHECK
constraints that keep impossible values out of annual_records.

Revision ID: a1c3e5f7b902
Revises: 4448cb003a4a
Create Date: 2026-10-07

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a1c3e5f7b902'
down_revision = '4448cb003a4a'
branch_labels = None
depends_on = None


# (table, column) pairs indexed for the data explorer's filters and sorts.
SEARCH_INDEXES = [
    ('facilities', 'facility_name'),
    ('facilities', 'state_code'),
    ('facilities', 'county'),
    ('units', 'epa_unit_id'),
    ('units', 'unit_type'),
    ('units', 'primary_fuel'),
    ('annual_records', 'operating_time'),
    ('annual_records', 'gross_load'),
    ('annual_records', 'heat_input'),
    ('annual_records', 'co2_mass'),
    ('annual_records', 'so2_mass'),
    ('annual_records', 'nox_mass'),
]

CHECK_CONSTRAINTS = [
    ('ck_annual_records_year_range', 'reporting_year BETWEEN 1980 AND 2100'),
    ('ck_annual_records_operating_time',
     'operating_time >= 0 AND operating_time <= 8784'),
    ('ck_annual_records_load_nonnegative',
     'gross_load >= 0 AND heat_input >= 0 '
     'AND (steam_load IS NULL OR steam_load >= 0)'),
    ('ck_annual_records_mass_nonnegative',
     'co2_mass >= 0 AND so2_mass >= 0 AND nox_mass >= 0'),
]


def upgrade():
    op.create_table('uploaded_files',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('dataset_id', sa.Integer(), nullable=True),
    sa.Column('source_type', sa.String(length=20), nullable=False),
    sa.Column('original_filename', sa.String(length=255), nullable=False),
    sa.Column('stored_filename', sa.String(length=255), nullable=False),
    sa.Column('file_extension', sa.String(length=10), nullable=False),
    sa.Column('file_size', sa.Integer(), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('uploaded_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('dataset_name', sa.String(length=150), nullable=True),
    sa.Column('source_url', sa.String(length=500), nullable=True),
    sa.Column('query_params', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['dataset_id'], ['datasets.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('stored_filename')
    )
    with op.batch_alter_table('uploaded_files', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_uploaded_files_dataset_id'), ['dataset_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_uploaded_files_sha256'), ['sha256'], unique=False)

    op.create_table('data_provenance',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('dataset_id', sa.Integer(), nullable=False),
    sa.Column('source_type', sa.String(length=20), nullable=False),
    sa.Column('source_name', sa.String(length=255), nullable=False),
    sa.Column('source_url', sa.String(length=500), nullable=True),
    sa.Column('query_params', sa.Text(), nullable=True),
    sa.Column('original_filename', sa.String(length=255), nullable=True),
    sa.Column('file_sha256', sa.String(length=64), nullable=True),
    sa.Column('retrieved_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('reporting_years', sa.String(length=100), nullable=True),
    sa.Column('raw_records', sa.Integer(), nullable=False),
    sa.Column('accepted_records', sa.Integer(), nullable=False),
    sa.Column('rejected_records', sa.Integer(), nullable=False),
    sa.Column('warning_records', sa.Integer(), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['dataset_id'], ['datasets.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('dataset_id')
    )

    op.create_table('data_quality_issues',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('dataset_id', sa.Integer(), nullable=False),
    sa.Column('source_row', sa.Integer(), nullable=True),
    sa.Column('severity', sa.String(length=10), nullable=False),
    sa.Column('issue_type', sa.String(length=40), nullable=False),
    sa.Column('field', sa.String(length=60), nullable=True),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('facility_id', sa.String(length=50), nullable=True),
    sa.Column('epa_unit_id', sa.String(length=50), nullable=True),
    sa.Column('reporting_year', sa.String(length=20), nullable=True),
    sa.Column('raw_values', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['dataset_id'], ['datasets.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('data_quality_issues', schema=None) as batch_op:
        batch_op.create_index('ix_data_quality_issues_dataset_severity', ['dataset_id', 'severity'], unique=False)

    for table, column in SEARCH_INDEXES:
        op.create_index(f'ix_{table}_{column}', table, [column], unique=False)

    with op.batch_alter_table('annual_records', schema=None) as batch_op:
        for name, condition in CHECK_CONSTRAINTS:
            batch_op.create_check_constraint(name, condition)


def downgrade():
    with op.batch_alter_table('annual_records', schema=None) as batch_op:
        for name, _ in reversed(CHECK_CONSTRAINTS):
            batch_op.drop_constraint(name, type_='check')

    for table, column in reversed(SEARCH_INDEXES):
        op.drop_index(f'ix_{table}_{column}', table_name=table)

    with op.batch_alter_table('data_quality_issues', schema=None) as batch_op:
        batch_op.drop_index('ix_data_quality_issues_dataset_severity')
    op.drop_table('data_quality_issues')

    op.drop_table('data_provenance')

    with op.batch_alter_table('uploaded_files', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_uploaded_files_sha256'))
        batch_op.drop_index(batch_op.f('ix_uploaded_files_dataset_id'))
    op.drop_table('uploaded_files')
