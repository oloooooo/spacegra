"""Mirage（对象存储 VFS）上的只读 backend：每 run 一个 Workspace，每个 agent 一个 session。

为什么是 Mirage：知识空间与上传文件都在对象存储（S3 协议）里，Mirage 把它们当虚拟文件系统，
`ls/read/grep/glob` 全部经 VFS 走对象存储，不需要本地镜像。代价与取舍见 ADR-0018。

三个不能改的约定：

- **一个 run 一个 Workspace**：`Workspace(mounts=..., profiles=...)` 的构造 + 首次 LIST 约
  400ms（探针实测），所以按 run 建一次、run 内复用；`graphs/*.py` 的 `graph(config)` 本来就是
  每请求调用一次，天然符合。
- **一个 agent 一个 session**：隔离只能通过 `create_session(profile=...)` 的 hide/show 表达。
  inline `permissions` **不允许** `show`（Mirage `refuse_show`），所以可见范围必须是具名 profile。
- **授权 = hide 根 + show 子树**：`hide ["/"]` 配 `show ["/空间A", "/uploads"]` 就是一个
  允许列表（未 show 的同级目录整个消失）；但 **hide 与 show 同一个路径**（`hide ["/"]` +
  `show ["/"]`）会把 mount 根藏掉，`ls` 抛 TypeError、`read` 返回 None。根下那几份共享只读
  文件（`/process.md`）要单独 show——show 一个不存在的路径不会报错，所以这类常量很安全。

`MirageBackend` 与 `LangchainWorkspace` 的区别有两处，都为了 deepagents 的契约：
只暴露 `BackendProtocol`（不含 `execute`，对象存储上跑不了命令），以及把"被 hide"的两种
表现（`ls` 抛 TypeError / `read` 返回 None）归一成空列表与明确的找不到错误。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Collection, Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from deepagents.backends.protocol import (
    BackendProtocol,
    EditResult,
    FileDownloadResponse,
    FileUploadResponse,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)
from mirage import Workspace
from mirage.agents.langchain import LangchainWorkspace
from mirage.types import MountMode
from mirage.vfs.s3 import S3VFS, S3Config

from shared import config, lifecycle
from shared.session import blobs, upload_rule, uploads
from shared.session.store import identity_digest

logger = logging.getLogger(__name__)

#: 知识空间在 bucket 里的前缀（`okf/<空间名>/...`）。
OKF_PREFIX = "okf"

#: 上传文件的挂载点。`uploads/<owner>/<thread>/` 映射到 `/uploads`。
UPLOADS_MOUNT = "/uploads"
UPLOADS_PREFIX = "uploads"

#: 知识根下的共享只读文件（ADR-0005 Q3b：所有 agent 都可读）。新增根文件要同步这里。
SHARED_ROOT_FILES = ("/process.md",)

_WRITE_DENIED = "本会话的文件是只读的：上传走 POST /custom/uploads，agent 侧不写对象存储"

#: `SandboxBackendProtocol` 独有的成员，不能透传到 Mirage（见 `MirageBackend.__getattr__`）。
_SANDBOX_ONLY = frozenset({"execute", "aexecute", "id"})


class MirageConfigError(RuntimeError):
    """Mirage 模式的前置条件不满足（没配对象存储）。"""


# ---------- 配置 ----------


def _s3_config(key_prefix: str) -> S3Config:
    endpoint = config.get("s3.endpoint")
    if not endpoint:
        raise MirageConfigError("Mirage 模式要求对象存储，但 config.yaml 的 s3.endpoint 为空")
    access_key, secret_key = blobs.credentials()
    return S3Config(
        bucket=blobs.bucket(),
        endpoint_url=endpoint,
        key_prefix=key_prefix,
        region=config.get("s3.region"),
        path_style=True,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
    )


def _cache_and_index(scope: str) -> dict[str, Any]:
    """进程内缓存 + Redis 索引；没配 Redis 就退回进程内实现（单机开发）。

    `scope` 进 Redis key 前缀，避免不同会话的索引互相污染。
    """
    from mirage.cache.file.config import CacheConfig, RedisCacheConfig
    from mirage.cache.index.config import IndexConfig, RedisIndexConfig

    url = (config.get("mirage.redis_url") or "").strip()
    limit = config.get("mirage.cache_limit")
    ttl = float(config.get("mirage.index_ttl_seconds"))
    if not url:
        return {"cache": CacheConfig(limit=limit), "index": IndexConfig(ttl=ttl)}

    return {
        "cache": RedisCacheConfig(url=url, limit=limit, key_prefix=f"mirage:cache:{scope}:"),
        "index": RedisIndexConfig(url=url, ttl=ttl, key_prefix=f"mirage:index:{scope}:"),
    }


# ---------- Workspace ----------


def agent_profiles(agents: Mapping[str, Collection[str]]) -> dict[str, dict[str, Any]]:
    """agent 名 → 可见范围（Mirage 的具名 profile）。

    `agents` 的值是该 agent 能看的空间名集合；所有 agent 都额外看到 `/uploads`（本会话上传的
    文件），主 agent 还看到 `SHARED_ROOT_FILES`。可见范围用 `hide ["/"] + show [...]` 写成
    允许列表——这也是 Mirage 里唯一能表达"白名单"的写法（inline permissions 不许 show）。
    """
    profiles: dict[str, dict[str, Any]] = {}
    for name, spaces in agents.items():
        show = [*(f"/{space}" for space in spaces), UPLOADS_MOUNT]
        if name == upload_rule.MAIN_AGENT:
            show.extend(SHARED_ROOT_FILES)
        profiles[name] = {"paths": {"hide": ["/"], "show": show}}
    return profiles


def build_workspace(*, owner: str, thread_id: str, agents: Mapping[str, Collection[str]]) -> Workspace:
    """一个 run 的 Workspace：`/`=知识空间，`/uploads`=本会话的上传文件。

    两个 mount 各用一个 `S3VFS`（各一条 aiohttp 连接池），由 `Workspace.close()` 一起关。
    """
    if not owner or not thread_id:
        raise ValueError("owner 与 thread_id 都不能为空：它们是上传目录的隔离键")
    uploads.validate()  # s3fs 只看得见对象存储里的 uploads/，介质配错就抬错（ADR-0018 §9）

    scope = f"{identity_digest(owner)}-{thread_id}"
    mounts = {
        "/": S3VFS(_s3_config(OKF_PREFIX)),
        UPLOADS_MOUNT: S3VFS(_s3_config(f"{UPLOADS_PREFIX}/{owner}/{thread_id}")),
    }
    return Workspace(
        mounts=mounts,
        mode=MountMode.READ,
        profiles=agent_profiles(agents),
        workspace_id=scope,
        **_cache_and_index(scope),
    )


async def close_workspace(workspace: Workspace | None) -> None:
    """关掉 Workspace（内部 aiohttp session 跟着关）。失败只记日志，不打断请求。

    `Workspace.close()` 是异步的：漏掉 await 会报 `Unclosed client session`。
    """
    if workspace is None:
        return
    _drop_agent_backends(workspace)
    try:
        await workspace.close()
    except Exception:  # noqa: BLE001 —— 收尾失败不该让已成功的请求变 500
        logger.warning("关闭 Mirage Workspace 失败", exc_info=True)


def _drop_agent_backends(workspace: Workspace) -> None:
    """Workspace 回收时丢掉它的 backend 缓存（key 用 id，id 会被后续对象复用）。"""
    prefix = f"{id(workspace)}:"
    for key in [k for k in _agent_backends if k.startswith(prefix)]:
        _agent_backends.pop(key, None)


# ---------- 每会话的 Workspace 池 ----------
#
# 构造 + 首次 LIST 约 400ms，同一条线程连着几轮对话没必要每次重建；建一次、按 TTL 回收。
# 用池而不是“run 结束就关”：工厂图拿不到 run 的收尾钩子（aegra 只是 `async with` 一下把图
# 交出去），没地方 await close，硬关只能靠 GC（aiohttp 会报 Unclosed session）。
# key 里带 owner 摘要 + thread_id：不同用户/会话永远不共用。

_POOL_MAX = 8
_POOL_TTL_SECONDS = 300.0
_pool: dict[str, tuple[Workspace, float]] = {}
_agent_backends: dict[str, MirageBackend] = {}
# Workspace 正在处理的调用数（id → 数量）：回收前要等它们跑完，否则读到一半 session 被关
_inflight: dict[int, int] = {}


@contextmanager
def _in_use(workspace: Workspace | None) -> Iterator[None]:
    key = id(workspace)
    _inflight[key] = _inflight.get(key, 0) + 1
    try:
        yield
    finally:
        left = _inflight.get(key, 1) - 1
        if left > 0:
            _inflight[key] = left
        else:
            _inflight.pop(key, None)


def _signature(agents: Mapping[str, Collection[str]]) -> str:
    """可见范围的指纹，进池 key：profile 是构造期参数，范围不同就是另一个 Workspace。"""
    raw = "|".join(f"{name}:{','.join(sorted(spaces))}" for name, spaces in sorted(agents.items()))
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def _schedule_close(workspace: Workspace) -> None:
    """排一次 close，但先等在途调用结束（最多 60s）。

    原先是直接 `create_task(close_workspace(...))`：TTL/容量回收随时可能把正在跑
    `read`/`ls` 的 Workspace 关掉，调用方拿到 "session closed" 而不是数据。

    shortcut: 只等 60s，单次调用跑得比这更久仍会被关，那时候该查的是超时而不是池。
    """

    async def _later() -> None:
        for _ in range(600):
            if not _inflight.get(id(workspace)):
                break
            await asyncio.sleep(0.1)
        await close_workspace(workspace)

    try:
        asyncio.get_running_loop().create_task(_later())
    except RuntimeError:
        logger.debug("没有事件循环，Workspace 交给 GC")


def _sweep(now: float) -> None:
    """回收过期的，再按容量踢最旧的。"""
    for key, (workspace, last_used) in list(_pool.items()):
        if now - last_used > _POOL_TTL_SECONDS:
            _pool.pop(key, None)
            _schedule_close(workspace)
    while len(_pool) > _POOL_MAX:
        oldest = min(_pool, key=lambda k: _pool[k][1])
        workspace, _ = _pool.pop(oldest)
        _schedule_close(workspace)


def workspace_for(*, owner: str, thread_id: str, agents: Mapping[str, Collection[str]]) -> Workspace:
    """取本会话 + 本可见范围的 Workspace（池里命中就复用）。"""
    key = f"{identity_digest(owner)}:{thread_id}:{_signature(agents)}"
    now = time.monotonic()
    entry = _pool.pop(key, None)
    if entry is not None:
        _pool[key] = (entry[0], now)  # 命中：刷新 LRU 顺序
        return entry[0]

    workspace = build_workspace(owner=owner, thread_id=thread_id, agents=agents)
    _pool[key] = (workspace, now)
    _sweep(now)
    return workspace


async def close_all_workspaces() -> None:
    """服务退出时把池里的 Workspace 全关掉（aiohttp session 跟着关）。

    不关也不会丢数据（都是只读），但会在日志里刷 `Unclosed client session`，
    连接池也会一直吊着。挂到 FastAPI 的 lifespan shutdown。
    """
    while _pool:
        _, (workspace, _) = _pool.popitem()
        await close_workspace(workspace)
    _agent_backends.clear()


def for_agent(workspace: Workspace, agent: str) -> MirageBackend:
    """给某个 agent 开个 session 并包成 backend（同名 session 重复创建是幂等的）。"""
    try:
        workspace.create_session(agent, profile=agent)
    except Exception:  # noqa: BLE001 —— 池里复用时会撞上已存在的 session
        logger.debug("session 已存在，直接复用", extra={"agent": agent})
    return MirageBackend(workspace, sandbox_id=f"mirage-{agent}", session_id=agent)


async def download_bytes(run_config: Any, path: str) -> bytes | None:
    """按本 run 的身份/线程把虚拟路径的文件取回来（给读不了 HTTP 的本地工具用）。

    作用域与图完全一致：同一个池化 Workspace + 同一套 profile，所以工具能拿到的东西不会
    超过 agent 自己能 `read` 的范围。agent 名从 run config 反推：空间子 run 的
    ``user_spaces`` 只有它自己那一个；通用表格子 agent 是空列表。
    """
    from graphs.access.spaces import allowed_spaces, upload_scope
    from shared.session.upload_rule import XLSX_AGENT

    spaces = allowed_spaces(run_config)
    names = sorted(spaces) if spaces else []
    agent = names[0] if names else XLSX_AGENT
    agents: dict[str, tuple[str, ...]] = {agent: tuple(names)}

    owner, thread_id = upload_scope(run_config)
    workspace = workspace_for(owner=owner, thread_id=thread_id, agents=agents)
    backend = _agent_backend(workspace, agent)
    resp = await backend.adownload_files([path])
    item = resp[0] if resp else None
    if item is None:
        return None
    if item.error:
        logger.warning("download_failed", path=path, error=item.error)
        return None
    return item.content


def _agent_backend(workspace: Workspace, agent: str) -> MirageBackend:
    """同一个 Workspace 上的 agent backend 只造一次（工具每次调用都要取文件）。"""
    key = f"{id(workspace)}:{agent}"
    backend = _agent_backends.get(key)
    if backend is None:
        backend = for_agent(workspace, agent)
        _agent_backends[key] = backend
    return backend


# ---------- Backend ----------


class MirageBackend(BackendProtocol):
    """把 `LangchainWorkspace` 收敛到 deepagents 的 `BackendProtocol`。

    - 只继承 `BackendProtocol`：对象存储上没法执行命令，所以不暴露 `execute` 工具。
    - `ls`/`glob`/`grep` 遇到"被 hide"（Mirage 抛 TypeError 或返回 None）→ 空结果。
    - `read` 遇到"被 hide"（`file_data is None` 且无 error）→ 明确的找不到错误。
    - 写操作一律拒：上传只能走 HTTP 接口，agent 侧永远只读。
    """

    def __init__(self, workspace: Workspace, *, sandbox_id: str, session_id: str) -> None:
        self._workspace = workspace
        self._inner = LangchainWorkspace(workspace, sandbox_id=sandbox_id, session_id=session_id)
        self._session_id = session_id

    def __getattr__(self, item: str) -> Any:
        # 没显式适配的方法（download_files/upload_files 等）直接透传。
        # `execute`/`aexecute`/`id` 不透传：对象存储上跑不了命令，deepagents 靠
        # `isinstance(backend, SandboxBackendProtocol)` 决定要不要给模型一个 shell 工具，
        # 漏出这些属性会让它以为能执行（`hasattr` 也会变 True）。
        if item in _SANDBOX_ONLY:
            raise AttributeError(item)
        return getattr(self._inner, item)

    @property
    def session_id(self) -> str:
        return self._session_id

    # `download_files` / `upload_files` 必须显式实现：基类里有**具体**方法（不是抽象方法），
    # 它们默认抛 NotImplementedError，`__getattr__` 根本轮不到（属性在基类上已存在）。
    # 症状：xlsx 工具取文件时看到裸 `NotImplementedError`。

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return self._inner.download_files(paths)

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        with _in_use(getattr(self, "_workspace", None)):
            return await self._inner.adownload_files(paths)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return [FileUploadResponse(path=path, error=_WRITE_DENIED) for path, _ in files]

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return [FileUploadResponse(path=path, error=_WRITE_DENIED) for path, _ in files]

    async def als(self, path: str) -> LsResult:  # noqa: D102 —— 见类 docstring
        try:
            with _in_use(getattr(self, "_workspace", None)):
                result = await self._inner.als(path)
        except TypeError:
            # hide 掉的前缀：Mirage 内部对 None 做迭代
            return LsResult(entries=[])
        if result is None or result.entries is None:
            return LsResult(entries=[], error=None if result is None else result.error)
        return result

    async def aread(self, path: str, offset: int = 0, limit: int = 2000) -> ReadResult:  # noqa: D102
        with _in_use(getattr(self, "_workspace", None)):
            result = await self._inner.aread(path, offset=offset, limit=limit)
        if result is None:
            return ReadResult(error=f"找不到文件: {path}")
        if result.file_data is None and not result.error:
            return ReadResult(error=f"找不到文件: {path}")
        return result

    async def aglob(self, pattern: str, path: str | None = None) -> GlobResult:  # noqa: D102
        try:
            with _in_use(getattr(self, "_workspace", None)):
                result = await self._inner.aglob(pattern, path=path)
        except TypeError:
            return GlobResult(matches=[])
        if result is None or result.matches is None:
            return GlobResult(matches=[])
        return result

    async def agrep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        max_count: int | None = None,
    ) -> GrepResult:  # noqa: D102
        try:
            with _in_use(getattr(self, "_workspace", None)):
                result = await self._inner.agrep(pattern, path=path, glob=glob, max_count=max_count)
        except TypeError:
            return GrepResult(matches=[])
        if result is None or result.matches is None:
            return GrepResult(matches=[])
        return result

    async def awrite(self, path: str, content: str) -> WriteResult:  # noqa: D102
        return WriteResult(error=_WRITE_DENIED, path=None)

    async def aedit(self, path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:  # noqa: D102
        return EditResult(error=_WRITE_DENIED, path=None, occurrences=None)


# 进程退出时关池：HTTP 侧（`custom/app.py`）只调 `shared.lifecycle.close_all()`，
# 不直接 import 这个模块（`custom` 不许 import `graphs`）。
lifecycle.register(close_all_workspaces)
