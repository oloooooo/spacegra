"""session_object.thread_id → thread(thread_id)，删会话即删台账行（ADR-0018）

读路径不查台账，但**删会话**必须让台账跟着走，否则同名上传会在新会话里撞到旧行
（主键是 `(owner, thread, name)`，不会真撞，但孤儿行会一直累加、配额也会算错）。

对象存储里的字节不在这里删：`thread` 删除后对象成为孤儿，
由 `scripts/reap_upload_orphans.py` 按"前缀没有台账行 + 对象超过 N 小时"清掉
（不在删除事务里做网络 IO，删会话不该因为 S3 慢而超时）。

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b8c9d0e1f2a3"
down_revision: str | Sequence[str] | None = "a7b8c9d0e1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 老数据里可能有指向已删线程的行；FK 建之前先清掉，否则 add_constraint 直接失败。
    #
    # 注意：这一句是不可恢复的——只删 `sha256/size/target_agent` 这些台账信息，字节由
    # reaper 单独清。被删的都是"线程已经不存在"的行，也就是 FK 会级联删掉的那些，所以
    # 没有活数据损失（issue #9 实测：887 线程 / 18 条台账 / 0 孤儿）；但回滚只能删 FK，
    # 补不回这些行。
    op.execute(
        """
        DELETE FROM session_object o
        WHERE NOT EXISTS (SELECT 1 FROM thread t WHERE t.thread_id = o.thread_id)
        """
    )
    op.create_foreign_key(
        "fk_session_object_thread",
        "session_object",
        "thread",
        ["thread_id"],
        ["thread_id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint("fk_session_object_thread", "session_object", type_="foreignkey")
