"""孤儿前缀的两套形状（issue #4）：本地是 `<owner>/<thread>/`，s3 是 `uploads/<owner>/<thread>/`。

回归重点：两套形状共用一份 live 集合时 s3 分支永远不命中，`--apply` 会把所有上传删掉。
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


def test_live_prefixes_have_both_shapes() -> None:
    local, s3 = _reaper()._live_prefixes(ROWS)

    assert local == {"u1/t1/", "张三-9d6966/t2/"}
    assert s3 == {"uploads/u1/t1/", "uploads/张三-9d6966/t2/"}


def test_s3_prefixes_from_the_lister_match_live() -> None:
    """`_iter_prefixes` 切出来的前缀形状必须和 live 集合一致，否则全部被当孤儿。"""
    reaper = _reaper()
    key = reaper.blobs.upload_key("u1", "t1", "note.md")
    prefix = "/".join(key.split("/")[:3]) + "/"

    assert prefix in reaper._live_prefixes(ROWS)[1]
    assert prefix not in reaper._live_prefixes(ROWS)[0]
