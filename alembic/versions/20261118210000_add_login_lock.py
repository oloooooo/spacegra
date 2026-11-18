"""user_tables 加登录失败锁定三列（ADR-0019）。

计数与锁定时间存库而不是进程内存：多副本共享同一份，重启不丢。
阈值 5 次 / 锁定 300 秒写在 `custom/db/models.py` 的常量里。

Revision ID: d4e5f6a7b8c9
Revises: c1d2e3f4a5b6
Create Date: 2026-11-18 21:00:00.000000

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "d4e5f6a7b8c9"
down_revision = "c1d2e3f4a5b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_tables",
        sa.Column(
            "login_failed_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="连续登录失败次数",
        ),
    )
    op.add_column(
        "user_tables",
        sa.Column("last_failed_login", sa.DateTime(timezone=True), comment="最后一次登录失败时间"),
    )
    op.add_column(
        "user_tables",
        sa.Column("login_locked_until", sa.DateTime(timezone=True), comment="登录锁定到期时间"),
    )


def downgrade() -> None:
    op.drop_column("user_tables", "login_locked_until")
    op.drop_column("user_tables", "last_failed_login")
    op.drop_column("user_tables", "login_failed_count")
