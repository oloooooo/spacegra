"""领域子 agent：**一个图服务所有空间**，按 run config 里的 space 现读 prompt。

aegra.json 注册::

    "domain_agent": "./graphs/space_agent.py:graph",

调用链：主管 `start_async_task(subagent_type="<space 名>")` → 拦截层
（`graphs/middleware/async_task_guard.py`）把 ``configurable.user_spaces = [space]`` 转发给本图 →
这里用那个 space 去 ``knowledge/<space>/AGENT.md`` 取 system prompt。

所以**增删空间不改本文件**：空间 = 知识空间下的一个文件夹 + 一行 `space` 表记录。
唯一的图只有这一个，``graph_id`` 固定 ``domain_agent``。

xlsx 四件套按空间里有没 `.xlsx` 决定要不要装（纯文档空间不装，省掉工具 schema 与
CodeInterpreterMiddleware）。

本图是工厂图（1 参数 config）：aegra 对工厂图不缓存、按 run 重建，正好承载
"prompt 取决于这次 run 的 space"。
"""

from __future__ import annotations

from typing import Any

import dotenv
from deepagents import FilesystemPermission, create_deep_agent
from langchain_quickjs import CodeInterpreterMiddleware
from langgraph.graph.state import RunnableConfig

from graphs.access.spaces import allowed_spaces
from graphs.access.uploads_backend import filesystem_backend
from graphs.model import model
from graphs.tools import XLSX_TOOLS
from shared.knowledge.spec import has_tables, load_agent_spec

dotenv.load_dotenv()


def _space_of(config: RunnableConfig) -> str:
    """本次 run 绑定的 space（拦截层转发的是单元素列表）；取不到返回空串。"""
    spaces = allowed_spaces(config)
    return next(iter(sorted(spaces)), "") if spaces else ""


def space_backend(config_: RunnableConfig):  # noqa: ANN201 —— backend
    """子 agent 的只读 backend：本地磁盘按 space 隔离（ADR-0005）+ 本会话 `/uploads`。"""
    return filesystem_backend(config_)


def graph(config: RunnableConfig) -> Any:  # noqa: ANN401 — 返回编译后的图
    """建一个只服务 ``config`` 里那个 space 的领域子 agent。"""
    space = _space_of(config)
    _, system_prompt = load_agent_spec(space)
    has_space_tables = has_tables(space)

    middleware: list[Any] = []
    tools: list[Any] = []
    if has_space_tables:
        middleware.append(
            CodeInterpreterMiddleware(
                mode="turn",
                ptc=XLSX_TOOLS,  # pyright: ignore[reportArgumentType]
                timeout=120,
                max_result_chars=10000,
            )
        )
    # 完成时的回写由服务端发件箱分发者负责（ADR-0010）：
    # 子 run 转终态 -> finalize_run 写发件箱行 -> 分发者在父线程空闲时开一轮唤醒 run。
    # 子图不再需要任何通知中间件。

    backend = space_backend(config)
    return create_deep_agent(
        model=model,
        system_prompt=system_prompt,
        tools=tools,
        middleware=middleware,
        # 子 agent 同样按 space 隔离：spaces 来自拦截层经 run config 转发的 user_spaces。
        backend=backend,
        permissions=[FilesystemPermission(operations=["write"], paths=["/"], mode="deny")],
    )
