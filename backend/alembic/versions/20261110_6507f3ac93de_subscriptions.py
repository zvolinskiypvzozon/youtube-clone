"""subscriptions — подписки на каналы

Revision ID: 6507f3ac93de
Revises: 685d115152a1
Create Date: 2026-11-10 05:01:36.769640
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6507f3ac93de"
down_revision: str | None = "685d115152a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "subscriptions",
        sa.Column("channel_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["channel_id"],
            ["channels.id"],
            name=op.f("fk_subscriptions_channel_id_channels"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_subscriptions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("channel_id", "user_id", name=op.f("pk_subscriptions")),
    )
    op.create_index(
        "ix_subscriptions_user_created",
        "subscriptions",
        ["user_id", sa.literal_column("created_at DESC"), sa.literal_column("channel_id DESC")],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_subscriptions_user_created", table_name="subscriptions")
    op.drop_table("subscriptions")
