from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    false,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from aegra_api.core.orm import Base


class UserTables(Base):
    """账号 / 密码哈希"""

    __tablename__ = "user_tables"

    identity: Mapped[str] = mapped_column(
        String(50), index=True, unique=True, comment="登录账号", primary_key=True
    )
    display_name: Mapped[str] = mapped_column(
        String(10),
        index=True,
        comment="用户名",
    )
    org_id: Mapped[str | None] = mapped_column(
        String(50), comment="组织名", default=None
    )
    email: Mapped[str | None] = mapped_column(
        String(80), comment="邮箱名", default=None
    )

    hashed_password: Mapped[str] = mapped_column(String(100), comment="bcrypt 哈希")
    is_super: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), comment="超级用户，只能直接改库"
    )
    # 只读视图：带权限的关联走 UserSpace，写入请直接操作它
    spaces: Mapped[list[Space]] = relationship(secondary="user_space", viewonly=True)

    def __repr__(self) -> str:
        return f"<User {self.identity!r}>"


class Space(Base):
    """微服务空间：知识空间（KNOWLEDGE_SPACE_DIR）下的一个文件夹。"""

    __tablename__ = "space"

    name: Mapped[str] = mapped_column(
        String(100),
        index=True,
        unique=True,
        comment="微服务空间名称，即知识空间下的文件夹名",
        primary_key=True,
    )
    path: Mapped[str] = mapped_column(
        String(512), comment="工作目录路径，相对项目根（服务启动时的 CWD）"
    )
    # 只读视图，写入走 UserSpace
    users: Mapped[list[UserTables]] = relationship(
        secondary="user_space", viewonly=True
    )

    def __repr__(self) -> str:
        return f"<Space {self.name!r}>"


class UserSpace(Base):
    """用户 <-> 微服务空间 多对多关联，写入直接操作本表。"""

    __tablename__ = "user_space"

    identity: Mapped[str] = mapped_column(
        String(50),
        ForeignKey("user_tables.identity", ondelete="CASCADE"),
        primary_key=True,
    )
    space_name: Mapped[str] = mapped_column(
        String(100),
        ForeignKey("space.name", ondelete="CASCADE"),
        primary_key=True,
    )

    def __repr__(self) -> str:
        return f"<UserSpace {self.identity!r} -> {self.space_name!r}>"

class AsyncTaskOutbox(Base):
    """后台任务结果的发件箱（ADR-0010，Transactional Outbox）。

    一行 = 一次子 run 的终态结果**待投递**。写入点是 `finalize_run`（子 run 转终态的
    同一个事务里插入），主键 (task_id, run_id) 就是幂等键。

    状态机：`pending` →（分发者建唤醒 run）/（父 run 的 hook 写 state）→ `delivered`；
    重试耗尽 → `failed`。**没有任何时间窗口**：“谁先把行从 pending 翻成 delivered，
    谁才有资格投”是一个单行条件 UPDATE，本身就是原子的。
    """

    __tablename__ = "async_task_outbox"

    task_id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="异步任务 id（= 子线程 id）")
    run_id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="子 agent 本次执行的 run id")
    parent_thread_id: Mapped[str] = mapped_column(String(64), comment="主管线程 id")
    agent_name: Mapped[str | None] = mapped_column(String(64), comment="子 agent 名（子线程的 graph_id）")
    terminal_status: Mapped[str] = mapped_column(String(32), comment="子 run 的终态：success / error / timeout")

    status: Mapped[str] = mapped_column(
        String(16), default="pending", server_default="pending", comment="pending / delivered / failed"
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), comment="投递尝试次数")
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), comment="最早何时重试（退避）"
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        default=None,
        comment="分发者租约：拿批时写，崩溃后过期即可被别人接手",
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        # 分发者的领取查询：status + next_attempt_at 过滤，按时间排序
        Index("ix_async_task_outbox_pending", "status", "next_attempt_at"),
        Index("ix_async_task_outbox_parent", "parent_thread_id"),
    )

    def __repr__(self) -> str:
        return f"<AsyncTaskOutbox {self.task_id!r}/{self.run_id!r} {self.status} attempts={self.attempts}>"


class SessionObject(Base):
    """会话文件里二进制的元数据（ADR-0015）。字节在对象存储，行在这里。

    主键 `(上传者, 主管 thread, 文件名)`：同名覆盖是 PUT 语义，不是版本链。
    唯一约束落在 DB 上（不靠"先查后插"），并发同名上传只会有一行活下来。
    """

    __tablename__ = "session_object"

    owner_identity: Mapped[str] = mapped_column(
        String(50),
        ForeignKey("user_tables.identity", ondelete="CASCADE"),
        primary_key=True,
        comment="上传者身份；与 thread_id 一起构成私有隔离前缀",
    )
    thread_id: Mapped[str] = mapped_column(
        String(64), primary_key=True, comment="主管线程 id（= 会话），删会话即删"
    )
    name: Mapped[str] = mapped_column(
        String(200), primary_key=True, comment="上传文件名，形如 <空间名>_<表名>.xlsx"
    )
    space: Mapped[str] = mapped_column(
        String(100), comment="从文件名前缀解析出的空间名，只用于展示，**不是**隔离键"
    )
    target_agent: Mapped[str] = mapped_column(
        String(100),
        server_default="",
        comment=(
            "上传时用 shared.session.upload_rule.route_upload() 算好的投递目标"
            "（空间名 / xlsx / main）；读路径不重算，避免上传者空间变了以后悄悄换人处理。"
            "空值只出现在加这一列之前的老行"
        ),
    )
    object_key: Mapped[str] = mapped_column(String(512), comment="对象存储 key（含 {sha16}）")
    sha256: Mapped[str] = mapped_column(String(64), comment="内容哈希；表索引的缓存键")
    size_bytes: Mapped[int] = mapped_column(BigInteger, comment="字节数；配额按它累加")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        # 配额查询：WHERE owner_identity=… 的 sum(size_bytes) / count(*)
        Index("ix_session_object_owner", "owner_identity"),
        # 删会话时按 (owner, thread) 找行
        Index("ix_session_object_thread", "thread_id"),
    )

    def __repr__(self) -> str:
        return f"<SessionObject {self.owner_identity!r}/{self.thread_id!r}/{self.name!r} {self.size_bytes}B>"
