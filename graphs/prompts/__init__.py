"""prompts 包：prompt 存 .md 文件，这里只负责加载与渲染。

文件布局（与包同目录）::

    graphs/prompts/
    └── main.md        主 agent（supervisor）system prompt

领域子 agent 的 prompt 不在这里——它们跟着知识空间走（``knowledge/<space>/AGENT.md``），
见 ``shared/knowledge/spec.py``。一个空间一个文件，增删空间不用改代码。

用法::

    from graphs.prompts import get_prompt, render_prompt, list_prompts

    system_prompt = get_prompt("main")            # 同步读文件（带缓存），图 import 时用
    system_prompt = render_prompt("main", ...)    # PromptTemplate 渲染，模板变量用 {name} 占位

加载是同步的：图在 import 时同步组装（create_deep_agent(system_prompt=...) 在
模块顶层），异步 loader 在那里用不上；要异步场景可自行包 asyncio.to_thread。
"""

from functools import cache, lru_cache
from pathlib import Path

from langchain_core.prompts import PromptTemplate

_PROMPT_DIR = Path(__file__).resolve().parent


@cache
def load_prompt(prompt_file_name: str) -> str:
    """读 ``<name>.md``（同步 + 进程级缓存）。文件不存在时报出可用列表。"""
    path = _PROMPT_DIR / f"{prompt_file_name}.md"
    if not path.is_file():
        available = sorted(p.stem for p in _PROMPT_DIR.glob("*.md"))
        raise KeyError(f"未知 prompt：{prompt_file_name!r}，可用：{available}")
    return path.read_text(encoding="utf-8")


def list_prompts() -> list[str]:
    """返回所有可用的 prompt 名字（即 *.md 的文件名）。"""
    return sorted(p.stem for p in _PROMPT_DIR.glob("*.md"))


def get_prompt(name: str) -> str:
    """按名字取 prompt 原文。"""
    return load_prompt(name)


def render_prompt(name: str, **kwargs: object) -> str:
    """取 prompt 并做模板渲染；不传变量时原样返回（大括号不碰）。

    变量用 ``PromptTemplate.from_template`` 自动推断；若想显式声明（漏写即报错，
    意图更明确），可换成::

        PromptTemplate(
            template=load_prompt(name),
            input_variables=["pre_content", "next_content"],
        )
    """
    template = load_prompt(name)
    if not kwargs:
        return template
    return PromptTemplate.from_template(template).format(**kwargs)


# 模块级常量：图 import 时直接取，调用方不必关心加载方式。
MAIN_SYSTEM_PROMPT = load_prompt("main")
#: 通用表格子 agent（`graphs/xlsx_agent.py`）：只看得见 /uploads。
XLSX_SYSTEM_PROMPT = load_prompt("xlsx")

__all__ = [
    "MAIN_SYSTEM_PROMPT",
    "XLSX_SYSTEM_PROMPT",
    "get_prompt",
    "list_prompts",
    "load_prompt",
    "render_prompt",
]
