"""上传路由规则：一个文件名该交给哪个 agent。

规则只有三条（一期，用户定）：

- ``<空间名>_<表名>.xlsx`` 且空间名在本次会话的授权空间内 → **该空间的子 agent**（名字就是空间名）；
- 其余 ``.xlsx``（无前缀、前缀不是授权空间、**越权前缀**）→ **通用 xlsx 子 agent**；
- 其余文件（md 等）→ **主 agent**。

为什么权限判断不在这里：文件名里的空间名只是**路由提示**，不是权限来源。能不能看那个
空间只由 `graphs/access/spaces.py::allowed_spaces(config)` 决定；越权前缀在这里降级到通用
agent，那个 agent 的 profile 只 show `/uploads`，读不到该空间的知识——权限仍然只有一个来源。

纯函数、无 IO、无配置读取：调用方（上传 API、测试）自己把 `allowed_spaces` 的返回值传进来。
`spaces is None` 表示「不设限」（无鉴权的本地模式），此时仍然走通用 agent：没有空间清单就
没法判断前缀是不是真空间，宁可降级也不要猜。
"""

from __future__ import annotations

#: 主 agent 的名字（`graphs/supervisor.py` 的 `_GRAPH_ID` 之外的进程内名字）。
MAIN_AGENT = "main"

#: 通用 xlsx 子 agent 的名字，兜底所有认不出空间的表格。
XLSX_AGENT = "xlsx"

_XLSX_SUFFIX = ".xlsx"
_BAD_CHARS = ("/", "\\", "\x00")


def is_xlsx(name: str) -> bool:
    """名字是不是 xlsx（大小写不敏感，按 `.xlsx` 结尾判断）。"""
    return name.lower().endswith(_XLSX_SUFFIX)


def match_space(name: str, spaces: frozenset[str] | None) -> str | None:
    """文件名里带的、且**在授权清单内**的空间名；没有则 None。

    按最长匹配：空间名可能自带下划线（`a_b` 与 `a` 同时存在时，`a_b_x.xlsx` 归 `a_b`），
    所以不能按第一个下划线切。
    """
    if not spaces or not name:
        return None
    for space in sorted(spaces, key=len, reverse=True):
        prefix = f"{space}_"
        if name.startswith(prefix) and len(name) > len(prefix):
            return space
    return None


def route_upload(name: str, spaces: frozenset[str] | None) -> str:
    """这个上传该由哪个 agent 处理，返回 agent 名。

    Args:
        name: 上传的文件名（已由调用方清洗，只应是一个纯文件名）。
        spaces: 本次会话的授权空间；None 表示不设限（本地无鉴权模式）。

    Returns:
        `MAIN_AGENT`、某个空间名、或 `XLSX_AGENT`。
    """
    if not is_xlsx(name):
        return MAIN_AGENT
    if any(bad in name for bad in _BAD_CHARS):
        # 走到这里说明调用方漏了清洗：不猜，交给通用 agent（它只看 /uploads，跑不出去）。
        return XLSX_AGENT
    return match_space(name, spaces) or XLSX_AGENT
