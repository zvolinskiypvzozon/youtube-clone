"""playlists and playlist items — плейлисты канала (день 6, шаг 6)

Revision ID: 020f33bca74b
Revises: 3c6f18f885c5
Create Date: 2026-11-10 13:13:04.383142
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "020f33bca74b"
down_revision: str | None = "3c6f18f885c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "playlists",
        sa.Column("channel_id", sa.UUID(), nullable=False),
        sa.Column("title", sa.String(length=150), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "visibility",
            sa.Enum("public", "unlisted", "private", name="playlist_visibility"),
            server_default="private",
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(btrim(title)) BETWEEN 1 AND 150", name=op.f("ck_playlists_title_length")
        ),
        sa.CheckConstraint(
            "char_length(description) <= 5000", name=op.f("ck_playlists_description_length")
        ),
        sa.ForeignKeyConstraint(
            ["channel_id"],
            ["channels.id"],
            name=op.f("fk_playlists_channel_id_channels"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_playlists")),
    )
    op.create_index(
        "ix_playlists_channel_created",
        "playlists",
        ["channel_id", sa.literal_column("created_at DESC"), sa.literal_column("id DESC")],
        unique=False,
    )
    op.create_table(
        "playlist_items",
        sa.Column("playlist_id", sa.UUID(), nullable=False),
        sa.Column("video_id", sa.UUID(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column(
            "added_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("position >= 1", name=op.f("ck_playlist_items_position_positive")),
        sa.ForeignKeyConstraint(
            ["playlist_id"],
            ["playlists.id"],
            name=op.f("fk_playlist_items_playlist_id_playlists"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["video_id"],
            ["videos.id"],
            name=op.f("fk_playlist_items_video_id_videos"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("playlist_id", "video_id", name=op.f("pk_playlist_items")),
        sa.UniqueConstraint("playlist_id", "position", name="uq_playlist_items_playlist_position"),
    )
    op.create_index("ix_playlist_items_video", "playlist_items", ["video_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_playlist_items_video", table_name="playlist_items")
    op.drop_table("playlist_items")
    op.drop_index("ix_playlists_channel_created", table_name="playlists")
    op.drop_table("playlists")
    # Тип ENUM drop_table не удаляет.
    op.execute("DROP TYPE IF EXISTS playlist_visibility")
