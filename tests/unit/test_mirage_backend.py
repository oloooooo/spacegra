"""`graphs/access/mirage_backend.py` 的免 S3 单测。

只锁**纯逻辑**和**归一化契约**：profile 形状、没配对象存储时的报错、Mirage 的
"hide 掉" 表现（`ls` 抛 TypeError / 返回 None、`read` 返回 None）必须变成空列表和明确错误。
真 S3 的联调放 `tests/e2e`（需要 SeaweedFS），不在这里。
"""

from __future__ import annotations

import asyncio

import pytest
from deepagents.backends.protocol import FileDownloadResponse

from graphs.access.mirage_backend import (
    SHARED_ROOT_FILES,
    UPLOADS_MOUNT,
    MirageBackend,
    MirageConfigError,
    _s3_config,
    agent_profiles,
)
from shared.session.upload_rule import MAIN_AGENT, XLSX_AGENT


class TestAgentProfiles:
    def test_main_gets_authorized_spaces_uploads_and_root_files(self) -> None:
        profiles = agent_profiles({MAIN_AGENT: ("a", "b")})
        assert profiles[MAIN_AGENT] == {
            "paths": {"hide": ["/"], "show": ["/a", "/b", UPLOADS_MOUNT, *SHARED_ROOT_FILES]}
        }

    def test_xlsx_agent_sees_only_uploads(self) -> None:
        profiles = agent_profiles({XLSX_AGENT: ()})
        assert profiles[XLSX_AGENT] == {"paths": {"hide": ["/"], "show": [UPLOADS_MOUNT]}}

    def test_space_agent_sees_its_space_and_uploads(self) -> None:
        profiles = agent_profiles({"cbam-reporting": ("cbam-reporting",)})
        assert profiles["cbam-reporting"] == {"paths": {"hide": ["/"], "show": ["/cbam-reporting", UPLOADS_MOUNT]}}

    def test_space_agent_with_several_spaces(self) -> None:
        profiles = agent_profiles({"multi": ("a", "b")})
        assert profiles["multi"]["paths"]["show"] == ["/a", "/b", UPLOADS_MOUNT]

    def test_only_show_entries_never_paths_under_hide(self) -> None:
        # hide 只允许 "/"，具体授权一律用 show（Mirage 的 inline permissions 不许 show）
        for profile in agent_profiles({MAIN_AGENT: (), XLSX_AGENT: (), "cbam-reporting": ("cbam-reporting",)}).values():
            paths = profile["paths"]
            if "hide" in paths and "/" in paths["hide"]:
                assert "show" in paths


class TestS3Config:
    def test_missing_endpoint_raises_with_clear_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("shared.config.get", lambda key: "" if key == "s3.endpoint" else "x")
        with pytest.raises(MirageConfigError, match="s3.endpoint"):
            _s3_config("okf")


class _FakeInner:
    """替身：模拟 Mirage `LangchainWorkspace` 被 hide 时的三种表现。"""

    def __init__(self, *, ls_raises: bool = False, none_result: bool = False) -> None:
        self.ls_raises = ls_raises
        self.none_result = none_result

    async def als(self, path: str):  # noqa: ANN201
        if self.ls_raises:
            raise TypeError("'NoneType' object is not iterable")
        return None

    async def aread(self, path: str, offset: int = 0, limit: int = 2000):  # noqa: ANN201
        return None

    async def aglob(self, pattern: str, path: str | None = None):  # noqa: ANN201
        raise TypeError("'NoneType' object is not iterable")

    async def agrep(self, pattern: str, path: str | None = None, glob: str | None = None, max_count: int | None = None):  # noqa: ANN201, E501
        raise TypeError("'NoneType' object is not iterable")

    async def aexecute(self, command: str) -> str:  # pragma: no cover - 不该被调到
        return "boom"

    async def adownload_files(self, paths: list[str]):  # noqa: ANN201
        return [FileDownloadResponse(path=p, content=b"bytes") for p in paths]

    def download_files(self, paths: list[str]):  # noqa: ANN201
        return [FileDownloadResponse(path=p, content=b"bytes") for p in paths]


class TestNormalisation:
    def _backend(self) -> MirageBackend:  # noqa: D401
        backend = MirageBackend.__new__(MirageBackend)  # 跳过真 Workspace 的构造
        backend._inner = _FakeInner()
        backend._session_id = "s"
        return backend

    def test_download_files_delegates(self) -> None:
        """基类的 `download_files` 是具体方法（默认抛 NotImplementedError），必须自己实现。

        不实现的话 `__getattr__` 透传根本轮不到（属性在基类上已存在），xlsx 工具取文件时
        会看到裸 `NotImplementedError`（2026-11-01 实测踩到）。
        """
        backend = self._backend()
        assert backend.download_files(["/uploads/a.xlsx"])[0].content == b"bytes"
        assert asyncio.run(backend.adownload_files(["/uploads/a.xlsx"]))[0].content == b"bytes"

    def test_upload_files_denied(self) -> None:
        backend = self._backend()
        assert backend.upload_files([("/uploads/a.xlsx", b"x")])[0].error
        assert asyncio.run(backend.aupload_files([("/uploads/a.xlsx", b"x")]))[0].error

    async def test_ls_swallowed_type_error_and_none(self) -> None:
        backend = self._backend()
        assert (await backend.als("/x")).entries == []
        backend._inner = _FakeInner()
        assert (await backend.als("/x")).entries == []

    async def test_read_none_becomes_not_found(self) -> None:
        result = await self._backend().aread("/secret/index.md")
        assert result.file_data is None
        assert result.error is not None and "/secret/index.md" in result.error

    async def test_glob_and_grep_never_raise(self) -> None:
        backend = self._backend()
        assert (await backend.aglob("**/*.md", "/")).matches == []
        assert (await backend.agrep("x", path="/", glob=None, max_count=None)).matches == []

    async def test_writes_are_denied_with_actionable_message(self) -> None:
        backend = self._backend()
        write = await backend.awrite("/x.md", "hi")
        edit = await backend.aedit("/x.md", "a", "b")
        assert write.error and "/custom/uploads" in write.error
        assert edit.error and "/custom/uploads" in edit.error

    def test_sandbox_members_are_not_leaked(self) -> None:
        backend = self._backend()
        for name in ("execute", "aexecute", "id"):
            assert not hasattr(backend, name), name

    def test_other_members_still_delegate(self) -> None:
        assert self._backend()._inner is not None
