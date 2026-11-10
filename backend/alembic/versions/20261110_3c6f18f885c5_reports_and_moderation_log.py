"""reports, moderation_log — жалобы на видео (со снимком) и журнал действий staff

Revision ID: 3c6f18f885c5
Revises: 6507f3ac93de
Create Date: 2026-11-10 08:49:59.688651
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "3c6f18f885c5"
down_revision: str | None = "6507f3ac93de"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "moderation_log",
        sa.Column("actor_id", sa.UUID(), nullable=True),
        sa.Column(
            "action",
            sa.Enum(
                "video_hide",
                "video_unhide",
                "comment_delete",
                "report_dismiss",
                "user_ban",
                "user_unban",
                name="moderation_action",
            ),
            nullable=False,
        ),
        sa.Column("target_id", sa.UUID(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(reason) <= 500", name=op.f("ck_moderation_log_reason_length")
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
            name=op.f("fk_moderation_log_actor_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_moderation_log")),
    )
    op.create_index(
        "ix_moderation_log_target_created",
        "moderation_log",
        ["target_id", sa.literal_column("created_at DESC")],
        unique=False,
    )
    op.create_table(
        "reports",
        sa.Column("video_id", sa.UUID(), nullable=True),
        sa.Column("reporter_id", sa.UUID(), nullable=True),
        sa.Column(
            "category",
            sa.Enum("spam", "violence", "adult", "copyright", "other", name="report_category"),
            nullable=False,
        ),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("video_title", sa.String(length=100), nullable=False),
        sa.Column("channel_handle", sa.String(length=30), nullable=False),
        sa.Column(
            "status",
            sa.Enum("open", "resolved", "dismissed", name="report_status"),
            server_default="open",
            nullable=False,
        ),
        sa.Column("resolved_by", sa.UUID(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(status = 'open') = (resolved_at IS NULL)", name=op.f("ck_reports_resolved_consistent")
        ),
        sa.CheckConstraint("char_length(comment) <= 500", name=op.f("ck_reports_comment_length")),
        sa.ForeignKeyConstraint(
            ["reporter_id"],
            ["users.id"],
            name=op.f("fk_reports_reporter_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["resolved_by"],
            ["users.id"],
            name=op.f("fk_reports_resolved_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["video_id"],
            ["videos.id"],
            name=op.f("fk_reports_video_id_videos"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reports")),
    )
    op.create_index("ix_reports_reporter", "reports", ["reporter_id"], unique=False)
    op.create_index("ix_reports_video", "reports", ["video_id"], unique=False)
    op.create_index(
        "ix_reports_status_created",
        "reports",
        ["status", sa.literal_column("created_at DESC"), sa.literal_column("id DESC")],
        unique=False,
    )
    op.create_index(
        "uq_reports_open_video_reporter",
        "reports",
        ["video_id", "reporter_id"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_reports_open_video_reporter",
        table_name="reports",
        postgresql_where=sa.text("status = 'open'"),
    )
    op.drop_index("ix_reports_status_created", table_name="reports")
    op.drop_index("ix_reports_video", table_name="reports")
    op.drop_index("ix_reports_reporter", table_name="reports")
    op.drop_table("reports")
    op.drop_index("ix_moderation_log_target_created", table_name="moderation_log")
    op.drop_table("moderation_log")
    # Типы ENUM drop_table не удаляет.
    op.execute("DROP TYPE IF EXISTS report_status")
    op.execute("DROP TYPE IF EXISTS report_category")
    op.execute("DROP TYPE IF EXISTS moderation_action")
