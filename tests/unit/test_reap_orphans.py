"""孤儿前缀的形状（issue #4）：s3 侧是 `uploads/<owner>/<thread>/`。

回归重点：live 集合少了 `uploads/` 这一段时永远不命中，`--apply` 会把所有上传删掉。

    shortcut: 只验证形状，不跑 `plan()`（要 DB + S3），upgrade when 有 e2e 环境。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _reaper():
    spec = importlib.util.spec_from_file_location("reap_upload_orphans", REPO / "scripts" / "reap_upload_orphans.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module  # dataclass 要靠 __module__ 找回模块
    spec.loader.exec_module(module)
    return module


ROWS = [("u1", "t1"), ("张三-9d6966", "t2")]


def test_s3_prefixes_from_the_lister_match_live() -> None:
    """`_iter_prefixes` 切出来的前缀形状必须和 plan() 的 live 集合一致，否则全部被当孤儿。"""
    reaper = _reaper()
    key = reaper.blobs.upload_key("u1", "t1", "note.md")
    prefix = "/".join(key.split("/")[:3]) + "/"
    live = {reaper.blobs.upload_prefix(owner, thread) for owner, thread in ROWS}

    assert prefix == "uploads/u1/t1/"
    assert prefix in live
