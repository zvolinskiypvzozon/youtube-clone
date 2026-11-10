"""partial index of open reports per reporter — потолок открытых жалоб (день 6, шаг 2)

Revision ID: 538a0286b0f3
Revises: ef599b78e7c1
Create Date: 2026-11-10 18:31:16.529874
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "538a0286b0f3"
down_revision: str | None = "ef599b78e7c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_reports_reporter_open",
        "reports",
        ["reporter_id"],
        unique=False,
        postgresql_where=sa.text("status = 'open'"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_reports_reporter_open",
        table_name="reports",
        postgresql_where=sa.text("status = 'open'"),
    )
