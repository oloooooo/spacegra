"""session_object 加 target_agent（上传时算好的投递目标）

ADR-0018：文件落对象存储后，由谁处理（哪个空间 agent / 通用 xlsx agent / 主 agent）
在上传那一刻就定下来，写进这一列；读路径不再用"当前空间集"重算，避免同一份文件
因为上传者权限变化而被换个 agent 处理。

Revision ID: a7b8c9d0e1f2
Revises: c1d2e3f4a5b6
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7b8c9d0e1f2"
down_revision: str | Sequence[str] | None = "c1d2e3f4a5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "session_object",
        sa.Column(
            "target_agent",
            sa.String(length=100),
            server_default="",
            nullable=False,
            comment="上传时 route_upload() 算好的投递目标（空间名 / xlsx / main）",
        ),
    )


def downgrade() -> None:
    op.drop_column("session_object", "target_agent")
