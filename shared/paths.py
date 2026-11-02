"""路径段校验（ADR-0018 §10）：客户端字符串拼进文件路径前，先过这里。

不做清洗——上传目录按用户看到的文件名落地，改名会让 agent 找不到文件。只拒绝
会改变路径含义的东西：路径分隔符、Windows 保留字符、控制字符、`.`/`..`、
以及以点或空格结尾的名字（Windows 会静默把它改写掉）。
"""

from __future__ import annotations

_UNSAFE = frozenset('/\\:*?"<>|')
_DEFAULT_LIMIT = 200


def safe_segment(value: str, *, limit: int = _DEFAULT_LIMIT) -> str:
    """原样返回，或抛 `ValueError`（调用方翻成 4xx）。"""
    text = str(value)
    if not text or len(text) > limit:
        raise ValueError(f"长度必须在 1–{limit} 之间")
    if any(ch in _UNSAFE for ch in text) or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
        raise ValueError("不能包含路径分隔符、控制字符或 Windows 保留字符")
    if text in {".", ".."} or text.rstrip(" .") != text:
        raise ValueError("不能是 . 或 ..，也不能以点或空格结尾")
    return text
