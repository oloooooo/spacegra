"""spaces 访问控制的共享判定：backend、xlsx 工具、async 拦截层共用。

判定规则（ADR-0005，与 CONTEXT.md「用户与空间词表」一致）：

- 主管 run：spaces 权威来源是服务端注入的 ``langgraph_auth_user.user_spaces``；
  客户端 body 伪造的 ``configurable.user_spaces`` 对非服务身份一律忽略。
- 子 agent run：身份是静态服务账号（ADR-0004），spaces 来自拦截层经
  ``configurable["user_spaces"]`` 转发的列表；键缺失 = fail-closed 空集，
  值为 null = 主管本身无鉴权上下文（本地直跑 demo），不设限。
- 完全没有 ``langgraph_auth_user``（本地 ``graph.invoke``，不经服务端）
  返回 None = 无鉴权边界，放行——服务端所有 run 都必带该键（require_auth 强制）。
"""

from __future__ import annotations

from typing import Any

from langgraph.config import get_config

from shared.token import SERVICE_IDENTITY

_MISSING: Any = object()


def _field(obj: Any, key: str, default: Any = None) -> Any:
    """dict 或 pydantic 模型都能取字段（auth_user 两种形态都出现过）。"""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def allowed_spaces(config: Any) -> frozenset[str] | None:
    """从 RunnableConfig 解析允许访问的 spaces；None = 不设限（无鉴权上下文）。"""
    cfg = (config or {}).get("configurable") or {}
    auth_user = cfg.get("langgraph_auth_user")
    if auth_user is None:
        return None  # 本地直跑 demo，不经过服务端，无租户边界
    if _field(auth_user, "identity") == SERVICE_IDENTITY:
        if "user_spaces" not in cfg:
            return frozenset()  # 服务身份但没有转发列表：fail-closed
        raw = cfg["user_spaces"]
        if raw is None:
            return None  # 主管侧无鉴权上下文（本地 demo 透传 null），不设限
        return frozenset(raw)
    raw = _field(auth_user, "user_spaces", _MISSING)
    if raw is _MISSING:
        return frozenset()  # auth handler 没提供 spaces：fail-closed
    return frozenset(raw or ())


def current_identity(config: Any) -> str:
    """取当前运行身份（用于内部驱逐目录按身份隔离）。"""
    cfg = (config or {}).get("configurable") or {}
    auth_user = cfg.get("langgraph_auth_user")
    if auth_user is None:
        return "local"
    return str(_field(auth_user, "identity") or "local")


def upload_scope(config: Any) -> tuple[str, str]:
    """上传目录的隔离键 ``(owner, thread_id)``：对象存储里落在 ``uploads/<owner>/<thread>/``。

    子 agent run 的身份是服务账号、线程是自己的子线程，拿不到父会话的上传目录，所以拦截层
    （``graphs/middleware/async_task_guard.py::_config_for``）把父会话的 owner/thread 经
    ``configurable.upload_owner`` / ``upload_thread`` 转发下来，这里优先读它们。

    没有 thread_id（脚本直跑图）时用 ``no-thread`` 占位：那一轮就是看不到任何上传文件，
    不会误读到别人的目录（owner 仍然不同）。
    """
    cfg = (config or {}).get("configurable") or {}
    owner = str(cfg.get("upload_owner") or current_identity(config))
    thread_id = str(cfg.get("upload_thread") or cfg.get("thread_id") or _NO_THREAD)
    return owner, thread_id


_NO_THREAD = "no-thread"


def spaces_from_config_or_runtime() -> frozenset[str] | None:
    """backend 场景：从 langgraph 上下文取 config 再解析（同 StateBackend 模式）。"""
    try:
        config = get_config()
    except RuntimeError:  # 图外直接调 backend（测试/脚本）：fail-closed 只留根散文件
        return frozenset()
    return allowed_spaces(config)
