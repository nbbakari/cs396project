"""Unit owner/operator and mercury (Hg) controls

CAMPD reports a unit's owners and operators and its mercury control
equipment alongside the fields already stored; both were previously dropped.

Revision ID: b7d2e4f6a813
Revises: a1c3e5f7b902
Create Date: 2026-10-11

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b7d2e4f6a813'
down_revision = 'a1c3e5f7b902'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('units', schema=None) as batch_op:
        batch_op.add_column(sa.Column('owner_operator', sa.Text(), nullable=True))

    with op.batch_alter_table('annual_records', schema=None) as batch_op:
        batch_op.add_column(sa.Column('hg_control', sa.String(length=150), nullable=True))


def downgrade():
    with op.batch_alter_table('annual_records', schema=None) as batch_op:
        batch_op.drop_column('hg_control')

    with op.batch_alter_table('units', schema=None) as batch_op:
        batch_op.drop_column('owner_operator')
