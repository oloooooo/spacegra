"""Mirage（对象存储 VFS）上的只读 backend：每 run 一个 Workspace，每个 agent 一个 session。

为什么是 Mirage：知识空间与上传文件都在对象存储（S3 协议）里，Mirage 把它们当虚拟文件系统，
`ls/read/grep/glob` 全部经 VFS 走对象存储，不需要本地镜像。代价与取舍见 ADR-0018。

三个不能改的约定：

- **一个 run 一个 Workspace**：Workspace 构造只要 ~1ms，贵的是每个新 S3VFS 的第一次操作
  （~160ms，botocore 每次重扫服务模型）。所以 S3 客户端进程级共用一份（`_CLIENTS`），
  Workspace 每 run 现建，由 `run_scope` 挂在图的 `async with` 上关掉。
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

import logging
from collections.abc import Collection, Mapping
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
from mirage.accessor.pool import LoopClientCache
from mirage.accessor.s3 import S3Accessor
from mirage.agents.langchain import LangchainWorkspace
from mirage.types import MountMode
from mirage.vfs.s3 import S3VFS, S3Config

from shared import config, lifecycle
from shared.session import blobs, upload_rule
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


# ---------- 共用的 S3 客户端 ----------
#
# 每个新 S3VFS 都新建一个 aioboto3 client，而 client 的**第一次操作**要 ~160ms（botocore
# 重扫自己的服务模型目录，Windows 上尤其贵）。client 与租户无关——前缀在 accessor 的 config
# 里——所以整进程共用一份：每个 run 只剩 Workspace/session 的 ~1ms。

_CLIENTS: LoopClientCache[Any] = LoopClientCache("s3")


class _SharedAccessor(S3Accessor):
    """client 走进程级缓存；`close()` 什么都不做——关它的时机是进程退出。"""

    async def cached_client(self, factory: Any) -> Any:
        return await _CLIENTS.get(factory)

    async def close(self) -> None:
        return None


class _SharedVFS(S3VFS):
    """`S3VFS` 的 accessor 换成共用 client 的那份（key_prefix 仍各用各的）。"""

    def __init__(self, cfg: S3Config) -> None:
        super().__init__(cfg)
        self.accessor = _SharedAccessor(cfg)


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

    两个 mount 各一个 `S3VFS`，共用进程级的 S3 客户端；`Workspace.close()` 关时不会
    连带关掉那份共用的 client。
    """
    if not owner or not thread_id:
        raise ValueError("owner 与 thread_id 都不能为空：它们是上传目录的隔离键")
    uploads.validate()  # s3fs 只看得见对象存储里的 uploads/，介质配错就抬错（ADR-0018 §9）

    scope = f"{identity_digest(owner)}-{thread_id}"
    mounts = {
        "/": _SharedVFS(_s3_config(OKF_PREFIX)),
        UPLOADS_MOUNT: _SharedVFS(_s3_config(f"{UPLOADS_PREFIX}/{owner}/{thread_id}")),
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
    try:
        await workspace.close()
    except Exception:  # noqa: BLE001 —— 收尾失败不该让已成功的请求变 500
        logger.warning("关闭 Mirage Workspace 失败", exc_info=True)


# ---------- run 的生命周期 ----------
#
# Workspace 持有 aiohttp/Redis 连接（1 个文件缓存 + 每 mount 1 个索引 store），只有 `close()`
# 会释放。`src/` 的 run 执行器没给图代码收尾钩子，但它把工厂的返回值交给 `async with`
# （`generate_graph` 认 async context manager）。所以工厂返回下面这个代理：进 run 时交出图，
# run 结束时关 Workspace。少了这层，每 run 会漏一份文件缓存（有界池删掉之后，那就是按 run
# 增长的泄漏，比 #5 更糟）。


class _RunScope:
    """图的包装：其余成员原样透传，只在 `async with` 退出时关 Workspace。"""

    def __init__(self, graph: Any, workspace: Workspace) -> None:
        self._graph = graph
        self._workspace = workspace

    def __getattr__(self, item: str) -> Any:
        return getattr(self._graph, item)

    async def __aenter__(self) -> Any:
        return self._graph

    async def __aexit__(self, *exc_info: Any) -> None:
        await close_workspace(self._workspace)


def run_scope(graph: Any, backend: Any) -> Any:
    """把 backend 里那个 Workspace 绑到 run 的生命周期上；本地磁盘 backend 原样返回。"""
    workspace = getattr(backend, "_workspace", None)
    return _RunScope(graph, workspace) if isinstance(workspace, Workspace) else graph


async def close_all_workspaces() -> None:
    """服务退出时关掉共用的 S3 客户端（Workspace 已在各自 run 结束时关掉）。

    不关也不会丢数据（都是只读），但会在日志里刷 `Unclosed client session`。
    挂到 FastAPI 的 lifespan shutdown。
    """
    try:
        await _CLIENTS.close()
    except Exception:  # noqa: BLE001 —— 收尾失败不该让退出流程失败
        logger.warning("关闭共用的 S3 客户端失败", exc_info=True)


def for_agent(workspace: Workspace, agent: str) -> MirageBackend:
    """给某个 agent 开个 session 并包成 backend（同名 session 重复创建是幂等的）。"""
    try:
        workspace.create_session(agent, profile=agent)
    except Exception:  # noqa: BLE001 —— 同一 Workspace 上重复调用会撞上已存在的 session
        logger.debug("session 已存在，直接复用", extra={"agent": agent})
    return MirageBackend(workspace, sandbox_id=f"mirage-{agent}", session_id=agent)


async def download_bytes(run_config: Any, path: str) -> bytes | None:
    """按本 run 的身份/线程把虚拟路径的文件取回来（给读不了 HTTP 的本地工具用）。

    作用域与图完全一致：同一个 Workspace + 同一套 profile，所以工具能拿到的东西不会超过
    agent 自己能 `read` 的范围。agent 名从 run config 反推：空间子 run 的 ``user_spaces``
    只有它自己那一个；通用表格子 agent 是空列表。
    """
    from graphs.access.spaces import allowed_spaces, upload_scope
    from shared.session.upload_rule import XLSX_AGENT

    spaces = allowed_spaces(run_config)
    names = sorted(spaces) if spaces else []
    agent = names[0] if names else XLSX_AGENT
    agents: dict[str, tuple[str, ...]] = {agent: tuple(names)}

    owner, thread_id = upload_scope(run_config)
    workspace = build_workspace(owner=owner, thread_id=thread_id, agents=agents)
    try:
        resp = await for_agent(workspace, agent).adownload_files([path])
        item = resp[0] if resp else None
        if item is None:
            return None
        if item.error:
            logger.warning("download_failed", path=path, error=item.error)
            return None
        return item.content
    finally:
        await close_workspace(workspace)


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
        return await self._inner.adownload_files(paths)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return [FileUploadResponse(path=path, error=_WRITE_DENIED) for path, _ in files]

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return [FileUploadResponse(path=path, error=_WRITE_DENIED) for path, _ in files]

    async def als(self, path: str) -> LsResult:  # noqa: D102 —— 见类 docstring
        try:
            result = await self._inner.als(path)
        except TypeError:
            # hide 掉的前缀：Mirage 内部对 None 做迭代
            return LsResult(entries=[])
        if result is None or result.entries is None:
            return LsResult(entries=[], error=None if result is None else result.error)
        return result

    async def aread(self, path: str, offset: int = 0, limit: int = 2000) -> ReadResult:  # noqa: D102
        result = await self._inner.aread(path, offset=offset, limit=limit)
        if result is None:
            return ReadResult(error=f"找不到文件: {path}")
        if result.file_data is None and not result.error:
            return ReadResult(error=f"找不到文件: {path}")
        return result

    async def aglob(self, pattern: str, path: str | None = None) -> GlobResult:  # noqa: D102
        try:
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


# 进程退出时关共用的 S3 客户端：HTTP 侧（`custom/app.py`）只调 `shared.lifecycle.close_all()`，
# 不直接 import 这个模块（`custom` 不许 import `graphs`）。
lifecycle.register(close_all_workspaces)
