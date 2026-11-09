"""ix_videos_channel_created — «Мои видео»

Revision ID: d39fd7e9e8b8
Revises: c7a041934a27
Create Date: 2026-11-09 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d39fd7e9e8b8"
down_revision: str | None = "c7a041934a27"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Все видео канала в любом статусе, новые загрузки сверху (GET /api/me/videos).
    op.create_index(
        "ix_videos_channel_created",
        "videos",
        ["channel_id", sa.literal_column("created_at DESC"), sa.literal_column("id DESC")],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_videos_channel_created", table_name="videos")
