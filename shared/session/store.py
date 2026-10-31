"""会话文件在 LangGraph Store 里的命名空间与读写（ADR-0013）。

三处共用（main 已移除上传入口，底座保留给后续分支）：上传接口、主管中间件、删会话清理。
放这里而不是 `graphs/`：HTTP 路由会 import 它，别把整张图拖进启动路径。

两条不能改的约定：

- **key 带前导斜杠**。`CompositeBackend` 把 `/uploads/x.md` 剥成 `/x.md` 交给
  `StoreBackend`，后者直接拿这个字符串当 store key（`store.get(ns, file_path)`）。
  存成 `x.md` 会导致 `/uploads/x.md` 读不到。
- **命名空间里的身份做 hash**。`StoreBackend._validate_namespace` 只收
  `[A-Za-z0-9\\-_.@+:~]`，而 `user_tables.identity` 允许中文；直接用原文只有中文账号会炸。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

# 只是 store 的 batch op 类型（轻量模块），不是整张图
from langgraph.store.base import PutOp

from shared import config

# 命名空间前缀。第二期的空间表用别的前缀（如 "space_tables"），别混。
NAMESPACE_PREFIX = "uploads"

# 会话文件路径在 agent 眼里的根（CompositeBackend 的 route）
VIRTUAL_ROOT = "/uploads"


# 孤儿保留期：语义是「最后读取后多久」，不是绝对寿命（ADR-0013）
UPLOAD_TTL_MINUTES = int(config.get("session_files.upload_ttl_minutes"))
# 接口拒收线，与「是否放进上下文」的 32KB 阈值无关
MAX_UPLOAD_MB = int(config.get("session_files.max_upload_mb"))
# 每个会话最多挂多少个文件：TTL 只管孤儿、thread 配额只管会话数，
# 没有这道闸的话单会话可以无限堆（每份 ≤10MB）。
MAX_FILES_PER_SESSION = int(config.get("session_files.max_files_per_session"))


def identity_digest(identity: str) -> str:
    """身份的短摘要（Store 命名空间与对象存储前缀共用，两处必须一致）。"""
    return hashlib.sha256((identity or "").encode("utf-8")).hexdigest()[:16]


def session_namespace(identity: str, thread_id: str) -> tuple[str, ...]:
    """会话文件的 store 命名空间：(uploads, h(身份), 主管 thread_id)。"""
    return (NAMESPACE_PREFIX, identity_digest(identity), thread_id)


def session_key(name: str) -> str:
    """文件名 → store key（= agent 眼里的 /uploads 下路径），带前导斜杠。"""
    return "/" + name.lstrip("/")


def virtual_path(name: str) -> str:
    """文件名 → agent 眼里的完整路径，如 /uploads/报告.md。"""
    return f"{VIRTUAL_ROOT}{session_key(name)}"


def _file_data(text: str) -> dict[str, Any]:
    """deepagents FileData 形状（`StoreBackend` 只认这几个键，不 import 它以免拖重启动）。"""
    now = datetime.now(UTC).isoformat()
    return {"content": text, "encoding": "utf-8", "created_at": now, "modified_at": now}


async def put_md(store: Any, identity: str, thread_id: str, name: str, text: str) -> dict[str, Any]:
    """写入/覆盖一个会话文件，带孤儿 TTL。返回给接口响应用的元数据。"""
    ns = session_namespace(identity, thread_id)
    key = session_key(name)
    value = _file_data(text)
    await store.aput(ns, key, value, ttl=UPLOAD_TTL_MINUTES)
    return {
        "name": name,
        "path": virtual_path(name),
        "bytes": len(text.encode("utf-8")),
        "uploaded_at": value["created_at"],
        "ttl_minutes": UPLOAD_TTL_MINUTES,
    }


async def list_files(
    store: Any,
    identity: str,
    thread_id: str,
    *,
    limit: int = 20,
    refresh_ttl: bool = True,
) -> tuple[list[dict[str, Any]], bool]:
    """列出会话文件（按 key 排序），返回 (清单, 是否被截断)。

    `refresh_ttl=True`：主管每轮读一次清单即续期，活跃会话的文件不会过期；
    过期只惩罚再没人读的孤儿（ADR-0013）。
    """
    ns = session_namespace(identity, thread_id)
    # 多取一条只为判断截断，不返回
    items = await store.asearch(ns, limit=limit + 1, refresh_ttl=refresh_ttl)
    truncated = len(items) > limit
    rows = []
    for item in items[:limit]:
        value = item.value or {}
        content = value.get("content")
        rows.append(
            {
                "name": str(item.key).lstrip("/"),
                "path": f"{VIRTUAL_ROOT}{item.key}",
                "bytes": len(content.encode("utf-8")) if isinstance(content, str) else 0,
                "created_at": value.get("created_at") or "",
            }
        )
    return rows, truncated


# 单次会话清理最多跑几轮（500 × 20 = 1 万个文件）；防 store 行为异常时无限循环
_MAX_CLEAR_ROUNDS = 20


async def clear(store: Any, identity: str, thread_id: str) -> int:
    """删光这个会话的文件，返回删掉的条数（删会话时调用，幂等）。

    循环到取不出为止：`asearch` 自带 limit，单次删不完就会静默留下文件
    （只能等孤儿保留期搮底）。
    """
    ns = session_namespace(identity, thread_id)
    removed = 0
    for _ in range(_MAX_CLEAR_ROUNDS):
        items = await store.asearch(ns, limit=500)
        if not items:
            return removed
        # 没有 DeleteOp：store 的删除就是 PutOp(value=None)
        await store.abatch([PutOp(ns, str(item.key), None) for item in items])
        removed += len(items)
    # 剩下的留给孤儿保留期（ADR-0013），总比无限循环好
    return removed
