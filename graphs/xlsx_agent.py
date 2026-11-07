"""通用 xlsx 子 agent：**只看得见 `/uploads`**，吃所有认不出空间前缀的上传表格。

aegra.json 注册::

    "xlsx_agent": "./graphs/xlsx_agent.py:graph",

为什么要单独一个图（ADR-0018）：

- 主 agent 看得见所有上传文件，但**不做表格分析**（它手里没有 xlsx 工具），命中失败/越权的
  表格一律派给这里；
- 它不能是领域 agent：领域 agent 的 prompt 来自 ``knowledge/<space>/AGENT.md``，而且它的
  backend 只 show 自己那个空间——那样就看不见 `/uploads`。

可见范围：`filesystem_backend` 的本会话 `/uploads` 路由——它只挂着一个会话的上传目录，
知识空间走默认 backend（按 `allowed_spaces` 决定）。
"""

from __future__ import annotations

from typing import Any

import dotenv
from deepagents import FilesystemPermission, create_deep_agent
from langchain_quickjs import CodeInterpreterMiddleware
from langgraph.graph.state import RunnableConfig

from graphs.access.uploads_backend import filesystem_backend
from graphs.model import model
from graphs.prompts import XLSX_SYSTEM_PROMPT
from graphs.tools import XLSX_TOOLS

dotenv.load_dotenv()


def xlsx_backend(config_: RunnableConfig):  # noqa: ANN201 —— backend
    """只看 `/uploads` 的 backend：本会话的上传目录在本地盘上。"""
    return filesystem_backend(config_)


def graph(config_: RunnableConfig) -> Any:  # noqa: ANN401 — 返回编译后的图
    """建通用表格子 agent。"""
    backend = xlsx_backend(config_)
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
        backend=backend,
        permissions=[FilesystemPermission(operations=["write"], paths=["/"], mode="deny")],
    )
