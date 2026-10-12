"""Persist source discovery checks and operator overrides.

Revision ID: 8dcd4fd9b429
Revises: 50b3e35e6e2b
"""

from alembic import op
import sqlalchemy as sa

from greek_news.db import UTCDateTime


revision = "8dcd4fd9b429"
down_revision = "50b3e35e6e2b"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("sources", sa.Column("operator_disabled", sa.Boolean(), nullable=False,
                                       server_default=sa.false()))
    op.add_column("sources", sa.Column("last_discovery_check", UTCDateTime(), nullable=True))


def downgrade():
    op.drop_column("sources", "last_discovery_check")
    op.drop_column("sources", "operator_disabled")