"""comments — комментарии к видео (плоские, без правки)

Revision ID: 685d115152a1
Revises: feae87d73a60
Create Date: 2026-11-10 04:18:00.734896
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "685d115152a1"
down_revision: str | None = "feae87d73a60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "comments",
        sa.Column("video_id", sa.UUID(), nullable=False),
        sa.Column("author_id", sa.UUID(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.CheckConstraint(
            "char_length(btrim(body)) BETWEEN 1 AND 2000", name=op.f("ck_comments_body_length")
        ),
        sa.ForeignKeyConstraint(
            ["author_id"],
            ["users.id"],
            name=op.f("fk_comments_author_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["video_id"],
            ["videos.id"],
            name=op.f("fk_comments_video_id_videos"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_comments")),
    )
    op.create_index("ix_comments_author", "comments", ["author_id"], unique=False)
    op.create_index(
        "ix_comments_video_created",
        "comments",
        ["video_id", sa.literal_column("created_at DESC"), sa.literal_column("id DESC")],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_comments_video_created", table_name="comments")
    op.drop_index("ix_comments_author", table_name="comments")
    op.drop_table("comments")
