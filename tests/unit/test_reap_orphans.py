"""孤儿前缀的两套形状（issue #4）：本地是 `<owner>/<thread>/`，s3 是 `uploads/<owner>/<thread>/`。

回归重点：两套形状共用一份 live 集合时 s3 分支永远不命中，`--apply` 会把所有上传删掉。
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

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


def test_local_orphans_match_exactly_not_by_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """`u1/t1/` 不能保护 `u1/t10/`：live 是集合精确匹，不是 startswith。"""
    reaper = _reaper()
    cutoff = datetime.now(UTC) - timedelta(days=1)
    old = cutoff - timedelta(days=1)
    fake = {
        "u1/t1/": [{"LastModified": old, "Size": 1}],
        "u1/t10/": [{"LastModified": old, "Size": 2}],
    }
    monkeypatch.setattr(reaper, "_iter_local_prefixes", lambda: fake)

    prefixes, objects, size = reaper._local_orphans({"u1/t1/"}, cutoff)

    assert prefixes == ["u1/t10/"]
    assert (objects, size) == (1, 2)


def test_local_orphans_keep_recent_prefixes(monkeypatch: pytest.MonkeyPatch) -> None:
    """还有新文件的目录不动：可能有上传正在写。"""
    reaper = _reaper()
    cutoff = datetime.now(UTC) - timedelta(hours=1)
    fake = {
        "u1/t1/": [{"LastModified": datetime.now(UTC), "Size": 1}],
        "u2/t2/": [{"LastModified": datetime.now(UTC) - timedelta(days=3), "Size": 5}],
    }
    monkeypatch.setattr(reaper, "_iter_local_prefixes", lambda: fake)

    prefixes, objects, size = reaper._local_orphans(set(), cutoff)

    assert prefixes == ["u2/t2/"]
    assert (objects, size) == (1, 5)


def test_s3_prefixes_from_the_lister_match_live() -> None:
    """`_iter_prefixes` 切出来的前缀形状必须和 live 集合一致，否则全部被当孤儿。"""
    reaper = _reaper()
    key = reaper.blobs.upload_key("u1", "t1", "note.md")
    prefix = "/".join(key.split("/")[:3]) + "/"

    assert prefix in reaper._live_prefixes(ROWS)[1]
    assert prefix not in reaper._live_prefixes(ROWS)[0]
