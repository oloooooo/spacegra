"""派生缓存落盘（ADR-0018 §6）：原子写 + 路径布局。

多实例部署时两个 worker 可能同时解析同一张表——缓存文件名由内容哈希决定，
字节完全相同，但**落盘必须是原子的**，否则读者会看到半截 Parquet。
"""

from __future__ import annotations

import threading
from pathlib import Path

from graphs.tools.xlsx import store

PAYLOAD = b"PAR1" + b"x" * 65536 + b"PAR1"


def test_write_atomic_creates_target(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(store, "cache_dir", lambda: tmp_path)
    target = tmp_path / "derived" / "abc" / "Sheet1.parquet"

    assert store.write_atomic(target, PAYLOAD) == target
    assert target.read_bytes() == PAYLOAD
    assert [p.name for p in target.parent.iterdir()] == ["Sheet1.parquet"]


def test_write_atomic_replaces_existing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(store, "cache_dir", lambda: tmp_path)
    target = tmp_path / "out.parquet"
    target.write_bytes(b"old")

    store.write_atomic(target, PAYLOAD)

    assert target.read_bytes() == PAYLOAD


def test_write_atomic_has_no_tmp_leftovers(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(store, "cache_dir", lambda: tmp_path)
    target = tmp_path / "out.parquet"

    for _ in range(3):
        store.write_atomic(target, PAYLOAD)

    assert [p.name for p in tmp_path.iterdir()] == ["out.parquet"]


def test_concurrent_writers_never_expose_partial_file(tmp_path: Path, monkeypatch) -> None:
    """8 个线程同时写同一个 key：读者只能看到"没有"或"完整"。

    这正是多实例共享缓存时的真实形状（Windows 上 `os.replace` 还会回
    PermissionError，由重试 + 「对方已写完就接受」兜住）。
    """
    monkeypatch.setattr(store, "cache_dir", lambda: tmp_path)
    target = tmp_path / "out.parquet"
    stop = threading.Event()
    torn: list[int] = []

    def read_loop() -> None:
        while not stop.is_set():
            try:
                size = target.stat().st_size
            except FileNotFoundError:
                continue
            if size not in (0, len(PAYLOAD)):
                torn.append(size)

    reader = threading.Thread(target=read_loop)
    reader.start()
    writers = [threading.Thread(target=store.write_atomic, args=(target, PAYLOAD)) for _ in range(8)]
    for writer in writers:
        writer.start()
    for writer in writers:
        writer.join()
    stop.set()
    reader.join()

    assert torn == []
    assert target.read_bytes() == PAYLOAD
    assert [p.name for p in tmp_path.iterdir()] == ["out.parquet"]
