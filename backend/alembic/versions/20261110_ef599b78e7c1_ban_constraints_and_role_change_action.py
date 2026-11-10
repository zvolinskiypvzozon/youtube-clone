"""ban constraints and role_change action — бан и CHECK причины модерации (день 6, шаг 3)

Revision ID: ef599b78e7c1
Revises: 020f33bca74b
Create Date: 2026-11-10 14:29:54.981475
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "ef599b78e7c1"
down_revision: str | None = "020f33bca74b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Значения moderation_action до этой миграции (для downgrade: удалить значение из ENUM
# Postgres не умеет — тип пересоздаётся).
_OLD_ACTIONS = (
    "video_hide",
    "video_unhide",
    "comment_delete",
    "report_dismiss",
    "user_ban",
    "user_unban",
)


def upgrade() -> None:
    op.execute("ALTER TYPE moderation_action ADD VALUE IF NOT EXISTS 'role_change'")

    # Причина модерации ⇔ скрытое видео. Данные приводятся к правилу явно, до CHECK:
    # у не скрытого причина снимается, скрытому без причины — общая причина.
    op.execute("UPDATE videos SET moderation_reason = NULL WHERE moderation = 'ok'")
    op.execute(
        "UPDATE videos SET moderation_reason = 'Скрыто модератором' WHERE moderation = 'hidden' "
        "AND (moderation_reason IS NULL OR btrim(moderation_reason) = '')"
    )
    op.execute(
        "UPDATE videos SET moderation_reason = left(moderation_reason, 500) "
        "WHERE char_length(moderation_reason) > 500"
    )
    op.create_check_constraint(
        op.f("ck_videos_moderation_reason_consistent"),
        "videos",
        "(moderation = 'hidden') = (moderation_reason IS NOT NULL)",
    )
    op.create_check_constraint(
        op.f("ck_videos_moderation_reason_length"),
        "videos",
        "char_length(btrim(moderation_reason)) BETWEEN 1 AND 500",
    )

    # Бан: роль забанившего; забанен ⇔ есть причина и роль. Существующие баны (до API бана
    # их ставили только вручную) считаются наложенными админом.
    op.add_column(
        "users",
        sa.Column(
            "banned_by_role",
            postgresql.ENUM(name="user_role", create_type=False),
            nullable=True,
        ),
    )
    op.execute("UPDATE users SET banned_reason = NULL WHERE NOT is_banned")
    op.execute(
        "UPDATE users SET banned_reason = 'Заблокирован' WHERE is_banned "
        "AND (banned_reason IS NULL OR btrim(banned_reason) = '')"
    )
    op.execute(
        "UPDATE users SET banned_reason = left(banned_reason, 500) "
        "WHERE char_length(banned_reason) > 500"
    )
    op.execute("UPDATE users SET banned_by_role = 'admin' WHERE is_banned")
    op.create_check_constraint(
        op.f("ck_users_ban_consistent"),
        "users",
        "(is_banned AND banned_reason IS NOT NULL AND banned_by_role IS NOT NULL)"
        " OR (NOT is_banned AND banned_reason IS NULL AND banned_by_role IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_users_banned_reason_length"),
        "users",
        "char_length(btrim(banned_reason)) BETWEEN 1 AND 500",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_users_banned_reason_length"), "users", type_="check")
    op.drop_constraint(op.f("ck_users_ban_consistent"), "users", type_="check")
    op.drop_column("users", "banned_by_role")
    op.drop_constraint(op.f("ck_videos_moderation_reason_length"), "videos", type_="check")
    op.drop_constraint(op.f("ck_videos_moderation_reason_consistent"), "videos", type_="check")

    # Значение role_change: записи журнала с ним удаляются, тип пересоздаётся без него.
    op.execute("DELETE FROM moderation_log WHERE action = 'role_change'")
    op.execute("ALTER TYPE moderation_action RENAME TO moderation_action_old")
    values = ", ".join(f"'{v}'" for v in _OLD_ACTIONS)
    op.execute(f"CREATE TYPE moderation_action AS ENUM ({values})")
    op.execute(
        "ALTER TABLE moderation_log ALTER COLUMN action TYPE moderation_action "
        "USING action::text::moderation_action"
    )
    op.execute("DROP TYPE moderation_action_old")
