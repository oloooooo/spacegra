"""路径段校验（ADR-0018 §10）：客户端字符串拼进文件路径前，先过这里。

不做清洗——上传目录按用户看到的文件名落地，改名会让 agent 找不到文件。只拒绝
会改变路径含义的东西：路径分隔符、Windows 保留字符、控制字符、`.`/`..`、
以点或空格结尾的名字（Windows 会静默把它改写掉），以及 Windows 保留设备名
（`CON`、`NUL` 这类名字在 Windows 上根本 open 不了，带扩展名的 `CON.txt` 一样）。

临时/备份前缀（`.tmp-<uuid>`、`.bak-<uuid>`）不在这里拒绝：本模块的 `local_path`
自己也要拼这两类名字。它们只对"用户给的文件名"有害，判据在
`custom/service/uploads.py::safe_name`。
"""

from __future__ import annotations

_UNSAFE = frozenset('/\\:*?"<>|')
_DEFAULT_LIMIT = 200
#: Windows 设备名（大小写不敏感，`CON.txt` 也算）
_RESERVED = frozenset(
    {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
)


def safe_segment(value: str, *, limit: int = _DEFAULT_LIMIT) -> str:
    """原样返回，或抛 `ValueError`（调用方翻成 4xx）。"""
    text = str(value)
    if not text or len(text) > limit:
        raise ValueError(f"长度必须在 1–{limit} 之间")
    if any(ch in _UNSAFE for ch in text) or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
        raise ValueError("不能包含路径分隔符、控制字符或 Windows 保留字符")
    if text in {".", ".."} or text.rstrip(" .") != text:
        raise ValueError("不能是 . 或 ..，也不能以点或空格结尾")
    if text.split(".")[0].rstrip(" ").lower() in _RESERVED:
        raise ValueError("是 Windows 保留设备名（CON/PRN/AUX/NUL/COM1-9/LPT1-9）")
    return text
