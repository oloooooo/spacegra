"""本地混合 backend：把 ``FilesystemBackend`` 的逐条 ``resolve()`` 换成一趟 ``scandir``。

设计依据是一次 A/B 基准（2026-10，见 ``docs/adr/0017-local-backend-scandir.md``）。
``FilesystemBackend.ls``/``glob`` 的成本几乎全部不在遍历上，而在**每一条目录项各做
一两次 ``Path.resolve()``**：Windows 上 resolve 走 ``nt._getfinalpathname``（实测
0.055 ms/次），glob 里每条命中还要 ``is_file()`` + ``stat()`` + ``_to_virtual_path()``
（又一次 resolve）。L 档 10040 文件：裸 ``rglob('*')`` 只要 52 ms，而
``FilesystemBackend.glob`` 要 3.9 s 且被 5 s 预算截断。同一份 work 里 Mirage 的
``find`` 是 2446 ms，所以这段开销是纯浪费，不是「本地磁盘该有的代价」。

本类的做法：

1. **起点只校验一次**：仍走父类 ``_resolve_path()``（virtual_mode 的越界拦截）。
2. **向下遍历只用 ``os.scandir`` + ``entry.is_dir(follow_symlinks=False)``**，
   并且**不跟随符号链接**——命中的每个路径在物理上必然位于起点之下，越界在构造上
   不可能发生，因此不需要逐条 resolve 兜底。
3. **元数据直接取 ``DirEntry.stat(follow_symlinks=False)``**，虚拟路径用相对路径
   拼接，省掉每条一次 ``relative_to()`` + 一次 ``resolve()``。

安全性上与父类的差异（都是收紧，不是放松）：

- 父类会进入符号链接目录，再靠逐条 resolve 把越界结果筛掉；本类**根本不进入**，
  顺带也就没有「链接循环」这一类分支。代价：符号链接的文件/目录在 ``ls``/``glob``
  里不可见。
- 起点校验、``virtual_mode`` 语义、越界报错文案全部沿用父类。

读写策略（按用户 2026-10 决定）：

- 读：按**内容**判定二进制（UTF-8 解码失败或含 NUL），返回明确错误而不是 base64
  ——顺带修掉父类把 ``.xlsx`` 当文本解码后抛 ``UnicodeDecodeError`` 的坑
  （``deepagents/backends/utils.py:_get_file_type`` 只按扩展名分类，xlsx 归为 text）。
- 写：默认**只读**，``write``/``edit``/``delete``/``upload_files`` 一律返回拒绝；
  需要写时显式 ``allow_write=True``（此时委托父类实现）。
- ``grep``：完全沿用父类（ripgrep 子进程已是本机最快路径，0.03 ms/文件）。

``agrep`` 之外的 ``a*`` 方法由 ``BackendProtocol`` 基类默认 ``asyncio.to_thread``
委托同步实现，所以只覆盖同步方法即可。
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path

import structlog
from deepagents.backends import FilesystemBackend
from deepagents.backends.protocol import (
    DeleteResult,
    EditResult,
    FileData,
    FileInfo,
    FileUploadResponse,
    GlobResult,
    LsResult,
    ReadResult,
    WriteResult,
)
from deepagents.backends.utils import (
    InvalidGlobPatternError,
    check_empty_content,
    compile_grep_include_glob,
    slice_read_response,
)

logger = structlog.getLogger(__name__)

#: 与 ``deepagents.backends.filesystem._DEFAULT_GLOB_TIMEOUT`` 对齐，便于结果可比。
DEFAULT_GLOB_TIMEOUT = 5.0

#: 单次 ``read`` 的上限，防把整块大文件读进上下文/内存。
DEFAULT_MAX_READ_BYTES = 32 * 1024 * 1024

_READ_ONLY_MSG = "read-only local backend: {op} of '{path}' rejected"


def _has_nul(head: bytes) -> bool:
    """粗判二进制：前 8 KiB 含 NUL 即认为二进制（git 的启发式）。"""
    return b"\x00" in head


class LocalBackend(FilesystemBackend):
    """``FilesystemBackend`` 的快速版：一趟 scandir、不跟随符号链接、默认只读。"""

    def __init__(
        self,
        root_dir: str | Path,
        *,
        virtual_mode: bool = True,
        max_file_size_mb: int = 10,
        glob_timeout: float = DEFAULT_GLOB_TIMEOUT,
        max_read_bytes: int = DEFAULT_MAX_READ_BYTES,
        allow_write: bool = False,
    ) -> None:
        """
        Args:
            root_dir: 磁盘根目录，agent 视角的 ``/`` 映射到这里。
            virtual_mode: 沿用父类语义；``True`` 时对外只暴露 ``/xxx`` 虚拟路径。
            max_file_size_mb: 沿用父类语义（写侧上限）。
            glob_timeout: 单次 ``glob`` 的墙钟预算（秒），超时置 ``truncated``。
            max_read_bytes: 单次 ``read`` 允许的字节上限。
            allow_write: 是否允许写；默认 ``False`` = 只读后端。
        """
        super().__init__(
            root_dir=root_dir,
            virtual_mode=virtual_mode,
            max_file_size_mb=max_file_size_mb,
        )
        self.glob_timeout = glob_timeout
        self.max_read_bytes = max_read_bytes
        self.allow_write = allow_write

    # ── 路径 ────────────────────────────────────────────────

    def _prefix_of(self, fs_path: Path) -> str | None:
        """目录 → 相对 cwd 的 POSIX 前缀；在校验根之外返回 ``None``。"""
        if fs_path == self.cwd:
            return ""
        try:
            return fs_path.relative_to(self.cwd).as_posix()
        except ValueError:
            return None

    def _display(self, rel: str, is_dir: bool = False) -> str:
        """相对路径 → agent 可见路径（virtual_mode 加前导 ``/``，目录带尾斜杠）。"""
        base = ("/" + rel) if self.virtual_mode else Path(self.cwd, rel).as_posix()
        if is_dir and base != "/":
            return base + "/"
        return base

    # ── ls ──────────────────────────────────────────────────

    def ls(self, path: str) -> LsResult:
        """列出目录直接子项（非递归）；不跟随、不返回符号链接。"""
        try:
            dir_path = self._resolve_path(path)
        except (OSError, RuntimeError, ValueError) as e:
            # ValueError = 越界/`..`（父类不接这类异常，会让它以异常形式漏到中间件）
            return LsResult(error=f"Cannot list '{path}': {e}", entries=None)
        if not dir_path.exists():
            return LsResult(error=f"Path '{path}': path_not_found", entries=None)
        if not dir_path.is_dir():
            return LsResult(error=f"Path '{path}': not_a_directory", entries=None)
        prefix = self._prefix_of(dir_path)
        if prefix is None:
            return LsResult(error=f"Path '{path}': path_not_found", entries=None)

        entries: list[FileInfo] = []
        errors: list[str] = []
        try:
            with os.scandir(dir_path) as scoped:
                for entry in scoped:
                    info = self._entry_info(entry, prefix, errors)
                    if info is not None:
                        entries.append(info)
        except OSError as e:
            logger.warning("ls aborted", path=path, error=str(e))
            errors.append(f"Listing of '{path}' aborted: {e}")

        entries.sort(key=lambda item: item["path"])
        return LsResult(error="\n".join(sorted(errors)) if errors else None, entries=entries)

    def _entry_info(
        self,
        entry: os.DirEntry[str],
        prefix: str,
        errors: list[str],
    ) -> FileInfo | None:
        """单个目录项 → ``FileInfo``；符号链接与不可 stat 的项返回 ``None``。"""
        try:
            if entry.is_symlink():
                return None
            is_dir = entry.is_dir(follow_symlinks=False)
            st = entry.stat(follow_symlinks=False)
        except OSError as e:
            errors.append(f"child error: cannot stat '{entry.name}': {e}")
            return None
        rel = f"{prefix}/{entry.name}" if prefix else entry.name
        return {
            "path": self._display(rel, is_dir=is_dir),
            "is_dir": is_dir,
            "size": 0 if is_dir else int(st.st_size),
            "modified_at": datetime.fromtimestamp(st.st_mtime).isoformat(),  # noqa: DTZ006  # 本地时间戳无时区
        }

    # ── glob ────────────────────────────────────────────────

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        """一趟深度优先遍历匹配文件；语义与父类一致（复用同一个 matcher）。"""
        try:
            matches_pattern = compile_grep_include_glob(pattern)
        except InvalidGlobPatternError as e:
            return GlobResult(error=str(e), matches=None)

        if path is None or path == "/":
            search_path = self.cwd
        else:
            try:
                search_path = self._resolve_path(path)
            except (OSError, RuntimeError, ValueError) as e:
                return GlobResult(error=f"Error globbing path '{path}': {e}", matches=[])
        if not search_path.exists() or not search_path.is_dir():
            return GlobResult(matches=[])
        prefix = self._prefix_of(search_path)
        if prefix is None:
            return GlobResult(matches=[])

        deadline = time.monotonic() + self.glob_timeout
        matches: list[FileInfo] = []
        truncated = False
        stack: list[tuple[Path, str]] = [(search_path, prefix)]
        while stack and not truncated:
            if time.monotonic() > deadline:
                truncated = True
                break
            current, cur_prefix = stack.pop()
            try:
                entries = list(os.scandir(current))
            except OSError as e:
                logger.warning("glob skipped unreadable dir", path=str(current), error=str(e))
                continue
            for entry in entries:
                if time.monotonic() > deadline:
                    truncated = True
                    break
                rel = f"{cur_prefix}/{entry.name}" if cur_prefix else entry.name
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append((Path(entry.path), rel))
                        continue
                    if not matches_pattern(rel):
                        continue
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                matches.append(
                    {
                        "path": self._display(rel),
                        "is_dir": False,
                        "size": int(st.st_size),
                        "modified_at": datetime.fromtimestamp(st.st_mtime).isoformat(),  # noqa: DTZ006
                    }
                )

        if truncated:
            logger.warning(
                "glob hit its budget; returning partial results",
                pattern=pattern,
                matches=len(matches),
                budget_s=self.glob_timeout,
            )
        matches.sort(key=lambda item: item["path"])
        return GlobResult(
            matches=matches,
            truncated=truncated,
            truncation_reason="budget" if truncated else None,
        )

    # ── read ────────────────────────────────────────────────

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        """读文本；内容判定为二进制时返回明确错误（不返回 base64）。"""
        try:
            resolved = self._resolve_path(file_path)
        except (OSError, RuntimeError, ValueError) as e:
            # ValueError = 越界/`..`；`/../etc/passwd` 这类输入必须变成错误字符串，不能抛
            return ReadResult(error=f"Error reading file '{file_path}': {e}")

        try:
            if not resolved.exists() or not resolved.is_file():
                return ReadResult(error=f"File '{file_path}' not found")
            size = resolved.stat().st_size
            if size > self.max_read_bytes:
                return ReadResult(
                    error=(
                        f"File '{file_path}' is too large to read ({size} bytes > "
                        f"{self.max_read_bytes}); use a dedicated tool to sample it"
                    )
                )
            # 按声明大小读：`read(max_read_bytes + 1)` 会让 CPython 一次性分配 32MB
            # 缓冲并做一次大尺寸 IO，实测把 1.5KB 的读拖到 5.7ms；精确读只要 0.02ms。
            with resolved.open("rb") as handle:
                raw = handle.read(size)
        except OSError as e:
            return ReadResult(error=f"Error reading file '{file_path}': {e}")

        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            return ReadResult(error=self._binary_msg(file_path, len(raw), "not valid UTF-8"))
        if _has_nul(raw[:8192]):
            return ReadResult(error=self._binary_msg(file_path, len(raw), "contains NUL bytes"))

        empty_msg = check_empty_content(content)
        if empty_msg:
            return ReadResult(file_data=FileData(content=empty_msg, encoding="utf-8"))
        return slice_read_response(FileData(content=content, encoding="utf-8"), offset, limit)

    @staticmethod
    def _binary_msg(file_path: str, size: int, why: str) -> str:
        return (
            f"File '{file_path}' is binary ({size} bytes, {why}); "
            "read() only returns UTF-8 text — use a dedicated tool (e.g. read_xlsx) instead"
        )

    # ── 写侧：默认拒绝 ──────────────────────────────────────

    def write(self, file_path: str, content: str) -> WriteResult:
        """默认拒绝；``allow_write=True`` 时委托父类。"""
        if not self.allow_write:
            return WriteResult(error=_READ_ONLY_MSG.format(op="write", path=file_path), path=None)
        return super().write(file_path, content)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,  # noqa: FBT001, FBT002
    ) -> EditResult:
        """默认拒绝；``allow_write=True`` 时委托父类。"""
        if not self.allow_write:
            return EditResult(error=_READ_ONLY_MSG.format(op="edit", path=file_path), path=None)
        return super().edit(file_path, old_string, new_string, replace_all)

    def delete(self, file_path: str) -> DeleteResult:
        """默认拒绝；``allow_write=True`` 时委托父类。"""
        if not self.allow_write:
            return DeleteResult(error=_READ_ONLY_MSG.format(op="delete", path=file_path), path=None)
        return super().delete(file_path)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """默认整体拒绝；``allow_write=True`` 时委托父类。"""
        if not self.allow_write:
            return [
                FileUploadResponse(path=path, error=_READ_ONLY_MSG.format(op="upload", path=path))
                for path, _ in files
            ]
        return super().upload_files(files)
