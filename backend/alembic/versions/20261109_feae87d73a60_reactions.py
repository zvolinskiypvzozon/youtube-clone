"""reactions — лайки и дизлайки

Revision ID: feae87d73a60
Revises: d39fd7e9e8b8
Create Date: 2026-11-09 16:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "feae87d73a60"
down_revision: str | None = "d39fd7e9e8b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reactions",
        sa.Column("video_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.Enum("like", "dislike", name="reaction_kind"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_reactions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["video_id"],
            ["videos.id"],
            name=op.f("fk_reactions_video_id_videos"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("video_id", "user_id", name=op.f("pk_reactions")),
    )
    op.create_index(
        "ix_reactions_user_kind_created",
        "reactions",
        ["user_id", "kind", sa.literal_column("created_at DESC")],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_reactions_user_kind_created", table_name="reactions")
    op.drop_table("reactions")
    # Тип ENUM drop_table не удаляет.
    op.execute("DROP TYPE IF EXISTS reaction_kind")
