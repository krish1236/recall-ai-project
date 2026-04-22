"""webhook remote_addr to text

Revision ID: eaa27fac997e
Revises: 4747273eff12
Create Date: 2026-04-22 16:37:16.784296

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'eaa27fac997e'
down_revision: Union[str, None] = '4747273eff12'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        'webhook_deliveries', 'remote_addr',
        existing_type=postgresql.INET(),
        type_=sa.Text(),
        existing_nullable=True,
        postgresql_using='remote_addr::text',
    )


def downgrade() -> None:
    op.alter_column(
        'webhook_deliveries', 'remote_addr',
        existing_type=sa.Text(),
        type_=postgresql.INET(),
        existing_nullable=True,
        postgresql_using='remote_addr::inet',
    )
