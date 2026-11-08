"""按 space 隔离的文件系统 backend（ADR-0005 的主管/子 agent 共用实现）。

在 ``FilesystemBackend`` 之上加一层访问判定，8 个 fs 工具（ls/read_file/grep/
glob/write_file/edit_file/delete）全部经由 backend，单点兜住：

- 读：仅允许 ``/<space>/...``（space ∈ allowed_spaces）、根散文件（process.md
  这类，Q3b 共享只读）、以及 deepagents 内部驱逐前缀（见下）；
- ``ls /``：只列出自己的 space 文件夹 + 根散文件，别人的空间与内部目录不可见；
- ``grep``/``glob``：从根搜索时按同样规则过滤命中（否则等于把别人空间的内容
  搜出来了）；内部驱逐目录一律不进搜索结果（防 uuid 枚举）；
- 写：仅放行内部驱逐前缀（large_tool_results / conversation_history / blobs，
  由 middleware 机器代码直调 backend.write，绕过工具层）；其余一律拒绝
  （工具层已有 write-deny 规则，这里是纵深防御，Q6a 只读）。

shortcut: 内部驱逐前缀的文件可被“知道 uuid 的直接读”拿到（驱逐恢复是设计
行为），但 ls/grep/glob 都不暴露它们——跨用户枚举已被挡住；要彻底隔离需按
身份重写驱逐路径，等有真实威胁再做。

spaces 解析见 ``space_access.allowed_spaces``：无鉴权上下文（本地 demo）
返回 None = 不设限，服务端 run 必有 auth_user，fail-closed。
"""

from __future__ import annotations

from pathlib import Path

import structlog
from deepagents.backends import FilesystemBackend
from deepagents.backends.protocol import (
    DeleteResult,
    EditResult,
    FileDownloadResponse,
    FileUploadResponse,
    FileUploadResponse,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)

from graphs.access.spaces import spaces_from_config_or_runtime
from shared.knowledge.root import knowledge_root

logger = structlog.getLogger(__name__)

# deepagents FilesystemMiddleware 机器代码直调 backend 的前缀（artifacts_root="/"）
_INTERNAL_PREFIXES = ("/large_tool_results", "/conversation_history", "/blobs")


def _first_segment(vpath: str) -> str:
    """/space1/x.md -> "space1"；"/" -> ""。"""
    return vpath.strip("/").split("/", 1)[0]


def _is_internal(vpath: str) -> bool:
    v = "/" + vpath.strip("/")
    return any(v == p or v.startswith(p + "/") for p in _INTERNAL_PREFIXES)


def _is_root_file(vpath: str) -> bool:
    """单段路径且指向根下真实文件（根散文件共享只读）。"""
    return "/" not in vpath.strip("/")


class SpaceBackend(FilesystemBackend):
    """FilesystemBackend + 按 allowed_spaces 的路径判定。"""

    def __init__(self, root_dir: str | Path) -> None:
        super().__init__(root_dir=root_dir, virtual_mode=True)

    # ---------- 判定 ----------

    def _check_read(self, vpath: str) -> str | None:
        """返回 None = 放行，否则是给模型看的错误文本。"""
        spaces = spaces_from_config_or_runtime()
        if spaces is None:
            return None
        first = _first_segment(vpath)
        if not first:
            return None  # "/" 本身：ls 走过滤，read "/" 由基类报错
        if first in spaces:
            if not (self.cwd / first).is_dir():
                logger.warning("space_folder_missing", space=first, root=str(self.cwd))
                return f"空间文件夹不存在：/{first}（未在知识空间根下创建，不自动创建）"
            return None
        if _is_internal(vpath):
            return None
        if _is_root_file(vpath) and (self.cwd / first).is_file():
            return None  # 根散文件，全用户只读共享
        visible = sorted(spaces) or ["（无）"]
        return f"无权访问 {vpath}：{first} 不在你的空间列表 {visible} 内"

    def _check_write(self, vpath: str) -> str | None:
        if _is_internal(vpath):
            return None  # middleware 驱逐机器代码专用
        spaces = spaces_from_config_or_runtime()
        if spaces is None and _first_segment(vpath):
            return None  # 本地 demo 不设限
        return f"只读存储，拒绝写入 {vpath}"

    # ---------- ls ----------

    def ls(self, path: str) -> LsResult:
        spaces = spaces_from_config_or_runtime()
        first = _first_segment(path)
        if spaces is None:
            return super().ls(path)
        if _is_internal(path):
            return LsResult(error=f"无权列出内部目录 {path}")
        if first and first not in spaces:
            err = self._check_read(path)
            if err:
                return LsResult(error=err)
        result = super().ls(path)
        if first:
            return result  # 已限定在自己的空间/根散文件内，无需再过滤
        # ls /：只留自己的 space 文件夹 + 根散文件（内部目录、别人的空间隐藏）
        if result.entries is None:
            return result
        kept = []
        for e in result.entries:
            name = _first_segment(e["path"])
            if e.get("is_dir") or e["path"].endswith("/"):
                if name in spaces:
                    kept.append(e)
            else:
                kept.append(e)  # 根散文件共享只读
        return LsResult(entries=kept)

    # ---------- read ----------

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        if err := self._check_read(file_path):
            return ReadResult(error=err)
        return super().read(file_path, offset, limit)

    # ---------- grep ----------

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
        context_lines: int = 0,
    ) -> GrepResult:
        spaces = spaces_from_config_or_runtime()
        if spaces is None:
            return super().grep(pattern, path, glob, max_count=max_count, context_lines=context_lines)
        if path:
            if _is_internal(path):
                return GrepResult(error=f"无权搜索内部目录 {path}", matches=[])
            if err := self._check_read(path):
                return GrepResult(error=err, matches=[])
        try:
            result = super().grep(pattern, path, glob, max_count=max_count, context_lines=context_lines)
        except UnicodeDecodeError:
            # ripgrep 子进程用系统 locale 编码解码 stdout（Windows 下是 GBK），
            # 扫到 space 里的 xlsx/图片等二进制就抛；改用 Python 扫描兜底。
            logger.warning("ripgrep_decode_failed_python_fallback", pattern=pattern, path=path)
            return self._py_grep(pattern, path, glob, max_count, spaces, context_lines)
        if result.matches is None:
            return result
        result.matches = [m for m in result.matches if self._visible(m["path"], spaces)]
        return result

    def _py_grep(
        self,
        pattern: str,
        path: str | None,
        glob_pattern: str | None,
        max_count: int | None,
        spaces: frozenset[str],
        context_lines: int = 0,
    ) -> GrepResult:
        """ripgrep 解码失败时的兜底：复用基类的 Python 搜索与 context 附加。

        不自己 walk 文件树：基类的 `_python_search` 用 `compile_grep_include_glob`
        匹配**相对路径**（与 ripgrep `-g` 同语义，`**/*.xlsx` 能命中），并自带
        max_count、超时与读失败上报；`_apply_grep_context` 负责 `context_lines`。
        """
        base_full = self._resolve_path(path or ".")
        results, truncated, partial_error = self._python_search(
            pattern, base_full, glob_pattern, max_count=max_count
        )
        matches: list[dict] = []
        for fpath, items in results.items():
            if not self._visible(fpath, spaces):
                continue
            for line_num, text in items:
                matches.append({"path": fpath, "line": int(line_num), "text": text})
        if context_lines:
            partial_error = self._apply_grep_context(
                matches, context_lines, partial_error, pattern, newline=None
            )
        return GrepResult(error=partial_error, matches=matches, truncated=truncated)

    def _visible(self, vpath: str, spaces: frozenset[str]) -> bool:
        if _is_internal(vpath):
            return False  # 驱逐文件 uuid 不进搜索结果，防跨用户枚举
        first = _first_segment(vpath)
        if first in spaces:
            return True
        return _is_root_file(vpath) and (self.cwd / first).is_file()

    # ---------- glob ----------

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        spaces = spaces_from_config_or_runtime()
        if spaces is None:
            return super().glob(pattern, path)
        if path:
            if _is_internal(path):
                return GlobResult(error=f"无权搜索内部目录 {path}", matches=[])
            if err := self._check_read(path):
                return GlobResult(error=err, matches=[])
        result = super().glob(pattern, path)
        if result.matches is None:
            return result
        result.matches = [m for m in result.matches if self._visible(m["path"], spaces)]
        return result

    # ---------- 写家族（工具层已 write-deny，这里是纵深防御） ----------

    def write(self, file_path: str, content: str) -> WriteResult:
        if err := self._check_write(file_path):
            return WriteResult(error=err)
        return super().write(file_path, content)

    def edit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        if err := self._check_write(file_path):
            return EditResult(error=err)
        return super().edit(file_path, old_string, new_string, replace_all)

    def delete(self, file_path: str) -> DeleteResult:
        if err := self._check_write(file_path):
            return DeleteResult(error=err)
        return super().delete(file_path)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        out: list[FileUploadResponse] = []
        for path, _data in files:
            if err := self._check_write(path):
                out.append(FileUploadResponse(path=path, error=err))
                return out  # 写家族只读，首个拒绝即可
        return super().upload_files(files)

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        out: list[FileDownloadResponse] = []
        for path in paths:
            if err := self._check_read(path):
                out.append(FileDownloadResponse(path=path, error=err))
            else:
                out.extend(super().download_files([path]))
        return out


def filesystem_backend(config: object) -> SpaceBackend:  # noqa: ARG001, ANN401
    """非 Mirage 模式的 backend：只读知识根。

    本分支（mirage-s3）的上传字节都在对象存储里，只有 `okf.backend: s3fs`
    走 Mirage 才看得见；其余模式 agent 没有可读的上传目录。
    """
    return SpaceBackend(root_dir=knowledge_root())
