"""`/uploads` 的本地 backend（ADR-0018 §9）：路由、只读、会话隔离。"""

from __future__ import annotations

from pathlib import Path

import pytest
from deepagents.backends import CompositeBackend

from graphs.access import uploads_backend as ub
from shared import config as app_config
from shared.session import uploads


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """okf.backend=local + uploads 落临时目录。返回 knowledge 根。"""

    def fake_get(key: str, *args: object):
        values: dict[str, object] = {
            "okf.backend": "local",
            "uploads.dir": str(tmp_path / "uploads"),
            "uploads.store": "fs",
        }
        if key in values:
            return values[key]
        return args[0] if args else None

    monkeypatch.setattr(app_config, "get", fake_get)
    space = tmp_path / "knowledge" / "space-a"
    space.mkdir(parents=True)
    (space / "index.md").write_text("hello space", encoding="utf-8")
    monkeypatch.setattr(ub, "knowledge_root", lambda: tmp_path / "knowledge")
    return tmp_path / "knowledge"


def cfg(thread: str = "t1", spaces: list[str] | None = None) -> dict:
    return {
        "configurable": {
            "thread_id": thread,
            "langgraph_auth_user": {"identity": "u1"},
            **({"user_spaces": spaces} if spaces is not None else {}),
        }
    }


def test_backend_is_composite_with_uploads_route(env: Path) -> None:
    backend = ub.filesystem_backend(cfg(spaces=["space-a"]))
    assert isinstance(backend, CompositeBackend)
    assert "/uploads/" in backend.routes


def test_uploads_read_comes_from_thread_dir(env: Path) -> None:
    uploads.put_stream(__import__("io").BytesIO(b"# table"), owner="u1", thread_id="t1", name="a.md", max_bytes=1024)
    backend = ub.filesystem_backend(cfg(spaces=["space-a"]))
    result = backend.read("/uploads/a.md")
    assert result.error is None
    assert result.file_data is not None
    assert result.file_data["content"] == "# table"


def test_other_thread_cannot_read(env: Path) -> None:
    uploads.put_stream(__import__("io").BytesIO(b"secret"), owner="u1", thread_id="t2", name="a.md", max_bytes=1024)
    backend = ub.filesystem_backend(cfg(thread="t1", spaces=["space-a"]))
    assert backend.read("/uploads/a.md").error is not None


def test_uploads_are_write_denied(env: Path) -> None:
    backend = ub.filesystem_backend(cfg(spaces=["space-a"]))
    result = backend.write("/uploads/new.md", "x")
    assert result.error is not None
    assert not uploads.local_path("u1", "t1", "new.md").exists()


def test_root_ls_shows_uploads_and_space(env: Path) -> None:
    backend = ub.filesystem_backend(cfg(spaces=["space-a"]))
    ls = backend.ls("/")
    assert ls.error is None
    paths = {entry["path"] for entry in ls.entries or []}
    assert "/uploads/" in paths or "/uploads" in paths


class TestUploadPath:
    def test_resolves_name(self, env: Path) -> None:
        uploads.put_stream(__import__("io").BytesIO(b"x"), owner="u1", thread_id="t1", name="t.xlsx", max_bytes=64)
        assert ub.upload_path(cfg(), "/uploads/t.xlsx") == uploads.local_path("u1", "t1", "t.xlsx")

    @pytest.mark.parametrize("path", ["/uploads/../x.xlsx", "/uploads/sub/x.xlsx", "/uploads/..", "/uploads/"])
    def test_rejects_traversal(self, env: Path, path: str) -> None:
        assert ub.upload_path(cfg(), path) is None

    def test_missing_file(self, env: Path) -> None:
        assert ub.upload_path(cfg(), "/uploads/none.xlsx") is None


def test_mirage_mode_skips_local_route(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        app_config,
        "get",
        lambda key, *a: (
            "s3fs" if key == "okf.backend" else ("/" + str(env.parent / "uploads") if key == "uploads.dir" else a[0])
        ),
    )
    assert ub.is_mirage_mode() is True
