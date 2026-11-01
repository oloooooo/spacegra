"""通用 xlsx 子 agent：**只看得见 `/uploads`**，吃所有认不出空间前缀的上传表格。

aegra.json 注册::

    "xlsx_agent": "./graphs/xlsx_agent.py:graph",

为什么要单独一个图（ADR-0018）：

- 主 agent 看得见所有上传文件，但**不做表格分析**（它手里没有 xlsx 工具），命中失败/越权的
  表格一律派给这里；
- 它不能是领域 agent：领域 agent 的 prompt 来自 ``knowledge/<space>/AGENT.md``，而且它的
  backend 只 show 自己那个空间——那样就看不见 `/uploads`。

可见范围由 ``MirageBackend`` 的 profile 决定：``agents={XLSX_AGENT: ()}`` → 只 show `/uploads`。
本地磁盘模式（``okf.backend: local``）没有上传目录，这时它退化成"什么都没有"的空 agent：
`ls /` 只会看到知识根下允许的东西——所以本地模式下不挂这个子 agent（见 ``graphs/supervisor.py``）。
"""

from __future__ import annotations

from typing import Any

import dotenv
from deepagents import FilesystemPermission, create_deep_agent
from langchain_quickjs import CodeInterpreterMiddleware
from langgraph.graph.state import RunnableConfig

from graphs.access.backend import SpaceBackend
from graphs.access.mirage_backend import for_agent, workspace_for
from graphs.access.spaces import upload_scope
from graphs.model import model
from graphs.prompts import XLSX_SYSTEM_PROMPT
from graphs.tools import XLSX_TOOLS
from shared import config
from shared.knowledge.spec import knowledge_root
from shared.session.upload_rule import XLSX_AGENT

dotenv.load_dotenv()


def xlsx_backend(config_: RunnableConfig):  # noqa: ANN201 —— 两种 backend 之一
    """只看 `/uploads` 的 backend（Mirage 模式）或本地磁盘的只读 backend。"""
    if config.get("okf.backend") != "s3fs":
        # 本地磁盘：上传目录不存在，按"不设限的只读根"退让（正常情况下这条分支不会被调用）。
        return SpaceBackend(root_dir=knowledge_root())
    owner, thread_id = upload_scope(config_)
    workspace = workspace_for(owner=owner, thread_id=thread_id, agents={XLSX_AGENT: ()})
    return for_agent(workspace, XLSX_AGENT)


def graph(config_: RunnableConfig) -> Any:  # noqa: ANN401 — 返回编译后的图
    """建通用表格子 agent。"""
    return create_deep_agent(
        model=model,
        system_prompt=XLSX_SYSTEM_PROMPT,
        tools=[],
        middleware=[
            CodeInterpreterMiddleware(
                mode="turn",
                ptc=XLSX_TOOLS,  # pyright: ignore[reportArgumentType]
                timeout=120,
                max_result_chars=10000,
            )
        ],
        backend=xlsx_backend(config_),
        permissions=[FilesystemPermission(operations=["write"], paths=["/"], mode="deny")],
    )
