"""删掉 session_object 上与主键前导列重复的索引（issue #13）

`ix_session_object_owner` 只索引 `owner_identity`，而主键 `(owner_identity, thread_id, name)`
的唯一索引已经覆盖它；配额与列表查询都带 `thread_id`，走主键更划算。留着只增加写放大。

表很小（本机 18 行），普通 `DROP INDEX` 是毫秒级；`CONCURRENTLY` 在 alembic 的事务里跑不了，
要并发建/删必须 `op.execute("COMMIT")` 破事务，代价与收益不成比例，这里不用。

Revision ID: d9e0f1a2b3c4
Revises: b8c9d0e1f2a3
"""

from collections.abc import Sequence

from alembic import op

revision: str = "d9e0f1a2b3c4"
down_revision: str | Sequence[str] | None = "b8c9d0e1f2a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_session_object_owner", table_name="session_object")


def downgrade() -> None:
    op.create_index("ix_session_object_owner", "session_object", ["owner_identity"], unique=False)
