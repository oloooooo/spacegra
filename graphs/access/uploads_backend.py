"""`/uploads` 目录的只读 backend（本地目录版，ADR-0018 §9）。

`s3fs` 模式下上传文件由 Mirage 的 S3VFS 直读（`graphs/access/mirage_backend.py`），
本模块只管另外两种配置：``local`` 和 ``s3``（拉本地镜像）。两者最终都是"磁盘上一个
会话专属的目录"：

- ``local`` + `uploads.store=fs`：字节本来就在 `data/uploads/<owner>/<thread>/`；
- ``local``/``s3`` + `uploads.store=s3`：第一次读时从对象存储拉到同一个目录
  （`shared.session.uploads.materialize`），之后就是普通本地文件。

这样 agent 侧只有一套读法（`LocalBackend` 挂到 `/uploads/` 路由上），
`okf.backend` 只决定"空间知识怎么读"，`uploads.store` 只决定"上传字节存在哪"。

用 ``LocalBackend`` 而不是 ``FilesystemBackend`` 的两个理由：它一次 ``scandir`` 遍历
（本地盘上 glob/ls 快一个数量级），以及它按**内容**判定二进制——xlsx 会得到明确的
"这是二进制，用表格工具"错误，而不是父类那种把 xlsx 当文本解码后的
``UnicodeDecodeError``。
"""

from __future__ import annotations

from pathlib import Path

from deepagents.backends import CompositeBackend
from deepagents.backends.protocol import BackendProtocol

from graphs.access.backend import SpaceBackend
from graphs.access.local_backend import LocalBackend
from graphs.access.spaces import upload_scope
from shared import config as app_config
from shared.knowledge.root import knowledge_root
from shared.session import uploads

#: 上传目录在 agent 视角下的挂载点（路由前缀必须带尾斜杠）
UPLOADS_MOUNT = "/uploads/"


def is_mirage_mode(config: dict | None = None) -> bool:
    """读文件是否走 Mirage（`okf.backend: s3fs`）。"""
    cfg = app_config if config is None else config
    return str(cfg.get("okf.backend")).strip().lower() == "s3fs"


def uploads_backend(config: dict) -> LocalBackend:
    """当前 run 的 `/uploads` backend（只读、限制在本会话目录内）。"""
    owner, thread_id = upload_scope(config)
    root = uploads.ensure_dir(uploads.local_dir(owner, thread_id))
    return LocalBackend(root, allow_write=False)


def filesystem_backend(config: dict) -> BackendProtocol:
    """本地/s3 镜像模式下的完整 backend：知识空间 + 本会话上传目录。"""
    return CompositeBackend(
        default=SpaceBackend(root_dir=knowledge_root()),
        routes={UPLOADS_MOUNT: uploads_backend(config)},
    )


def upload_path(config: dict, vpath: str) -> Path | None:
    """把 `/uploads/x.xlsx` 解析成本地文件路径（必要时从对象存储拉一份）。

    表格工具不走 backend（它要字节，不是文本），所以这里单独给一条同样受限的解析：
    会话目录由 `upload_scope` 决定，文件名只允许单段（`/`、`..` 一律拒绝），
    越界读不到别的会话的上传。
    """
    name = vpath.strip("/")
    if name.startswith("uploads/"):
        name = name[len("uploads/") :]
    if not name or "/" in name or "\\" in name or name in {".", ".."}:
        return None
    owner, thread_id = upload_scope(config)
    return uploads.materialize(owner, thread_id, name)
