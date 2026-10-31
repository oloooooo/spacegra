"""对象存储（SeaweedFS，S3 协议）的最小封装。

当前唯一调用方是 OKF 镜像（`shared/knowledge/root.py`，ADR-0016）。会话文件与大表上传已从
main 移除，但本模块作为底座保留（`feat/unified-files-*` 分支依赖它）。三条不能改的约定：

- **key 前缀按会话说**：`sessions/{h(identity)}/{thread_id}/...`。所以「删会话」= 删这个
  前缀，字节与索引一次清完。`h()` 与 Store 命名空间共用 `session_store.identity_digest`，
  两处若不一致，同一份内容会落在两套前缀下（排查极难）。
- **`{sha16}` 进 key**：并发上传同名不同内容时各写各的 key，谁最后写 PG 行谁赢，另一个是
  孤儿对象。永远不会出现「行说 A、字节是 B」。
- **自己管分片**：`upload_fileobj` 的 `upload_id` 藏在内部，超限时**拿不到它来 abort**，
  未完成分片会一直占空间。这里手写 create/upload_part/complete，出错时能 abort。

同步库（boto3），调用方一律 `asyncio.to_thread`。
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import uuid
from typing import Any

from shared import config
from shared.session.store import identity_digest

_PART_BYTES = 8 * 1024 * 1024  # S3 要求除最后一片外 ≥5MiB
_DELETE_BATCH = 1000  # S3 单次 delete_objects 上限


class BlobTooLarge(Exception):
    """上传超过字节上限。路由层映射成 413。"""


class BlobUnavailable(Exception):
    """对象存储未配置或不可用。路由层映射成 503。"""


def _env(name: str, default: str = "") -> str:
    """凭证仍只走环境变量（不进 config.yaml）。"""
    return (os.environ.get(name) or default).strip()


def bucket() -> str:
    return config.get("s3.bucket")


def configured() -> bool:
    """有没有配对象存储。开发环境只跑磁盘时这是 False：调用方据此**跳过**而不是报错。"""
    return bool(config.get("s3.endpoint"))


_client_cache: Any = None


def client() -> Any:
    """懒加载：`graphs/tools/xlsx` 在 import 期就被拉起，模块级别建 client 会让
    没配 `S3_*` 的环境（本地 demo、单测）直接 import 失败。
    """
    global _client_cache
    if _client_cache is not None:
        return _client_cache
    endpoint = config.get("s3.endpoint")
    if not endpoint:
        raise BlobUnavailable("对象存储未配置（config.yaml 的 s3.endpoint 为空）")
    try:
        import boto3
        from botocore.config import Config
    except ImportError as e:  # pragma: no cover - 部署缺依赖时给个明白话
        raise BlobUnavailable("缺少 boto3，请先安装") from e
    _client_cache = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=_env("S3_ACCESS_KEY", "dev"),
        aws_secret_access_key=_env("S3_SECRET_KEY", "dev"),
        region_name=config.get("s3.region"),
        # 给了 endpoint_url 之后 boto3 仍默认 virtual-host，bucket.127.0.0.1:8333 解析不了
        config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 3}),
    )
    return _client_cache


def reset_client() -> None:
    """测试用：丢掉缓存的 client（改 env 后要重新建）。"""
    global _client_cache
    _client_cache = None


# ---------- key 命名（单一来源） ----------


def session_prefix(owner: str, thread_id: str) -> str:
    """一个会话在对象存储里的根前缀；删会话就删它。"""
    return f"sessions/{identity_digest(owner)}/{thread_id}/"


def blob_key(owner: str, thread_id: str, sha256: str, name: str) -> str:
    return f"{session_prefix(owner, thread_id)}{sha256[:16]}/{name}"


def index_key(owner: str, thread_id: str, name: str, header: int, build: int) -> str:
    return f"{session_prefix(owner, thread_id)}idx/{name}.h{header}.v{build}.sqlite"


def tmp_key(name: str) -> str:
    return f"tmp/{uuid.uuid4().hex}/{name}"


# ---------- 读写 ----------


def stat(key: str) -> dict[str, Any] | None:
    """HEAD 一个对象；不存在返回 None（把 404 当正常情况，调用方不必包 try）。"""
    try:
        return client().head_object(Bucket=bucket(), Key=key)
    except Exception as e:  # noqa: BLE001 —— 只有 NoSuchKey/404 该被吞
        if _is_missing(e):
            return None
        raise


def get(key: str) -> bytes:
    return client().get_object(Bucket=bucket(), Key=key)["Body"].read()


def download(key: str, dest: str) -> None:
    client().download_file(bucket(), key, dest)


def put_stream(src: Any, key: str, *, max_bytes: int) -> tuple[str, int]:
    """把文件对象写成对象，同时算 sha256 与字节数；超线立刻中止并清掉分片。

    返回 `(sha256_hex, bytes)`。只有一片时走单次 PUT（简单、ETag=md5，便于对账）；
    多片时自己管 upload_id，出任何错都能 abort。空文件逰单次 PUT（S3 不接受 0 片分片）。
    """
    s3 = client()
    b = bucket()
    digest = hashlib.sha256()
    limit = f"文件超过 {max_bytes // (1024 * 1024)}MB 上限"

    first = src.read(_PART_BYTES)
    total = len(first)
    if total > max_bytes:
        raise BlobTooLarge(limit)
    digest.update(first)
    if not first:
        s3.put_object(Bucket=b, Key=key, Body=b"")
        return digest.hexdigest(), 0

    rest = src.read(_PART_BYTES)
    if not rest:  # 只有一片
        s3.put_object(Bucket=b, Key=key, Body=first)
        return digest.hexdigest(), total

    upload_id = s3.create_multipart_upload(Bucket=b, Key=key)["UploadId"]
    parts = [_upload_part(s3, b, key, upload_id, 1, first)]
    chunk, number = rest, 2
    try:
        while chunk:
            total += len(chunk)
            if total > max_bytes:
                raise BlobTooLarge(limit)
            digest.update(chunk)
            parts.append(_upload_part(s3, b, key, upload_id, number, chunk))
            number += 1
            chunk = src.read(_PART_BYTES)
        s3.complete_multipart_upload(Bucket=b, Key=key, UploadId=upload_id, MultipartUpload={"Parts": parts})
        return digest.hexdigest(), total
    except BaseException:
        with contextlib.suppress(Exception):  # 清理失败不该掩盖原始异常
            s3.abort_multipart_upload(Bucket=b, Key=key, UploadId=upload_id)
        raise


def _upload_part(s3: Any, b: str, key: str, upload_id: str, number: int, body: bytes) -> dict[str, Any]:
    r = s3.upload_part(Bucket=b, Key=key, UploadId=upload_id, PartNumber=number, Body=body)
    return {"ETag": r["ETag"], "PartNumber": number}


def copy(src_key: str, dst_key: str) -> None:
    """服务端复制（SeaweedFS 复用 chunk，不搬字节）。目的对象一次性出现。"""
    client().copy_object(Bucket=bucket(), Key=dst_key, CopySource={"Bucket": bucket(), "Key": src_key})


def delete(key: str) -> None:
    """幂等：不存在也算成功。"""
    client().delete_object(Bucket=bucket(), Key=key)


def delete_prefix(prefix: str) -> int:
    """删一个前缀下的全部对象，返回删掉的个数（删会话时用）。"""
    s3 = client()
    b = bucket()
    removed = 0
    token: str | None = None
    while True:
        kw: dict[str, Any] = {"Bucket": b, "Prefix": prefix}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        for i in range(0, len(keys), _DELETE_BATCH):
            s3.delete_objects(Bucket=b, Delete={"Objects": keys[i : i + _DELETE_BATCH]})
            removed += len(keys[i : i + _DELETE_BATCH])
        if not page.get("IsTruncated"):
            return removed
        token = page.get("NextContinuationToken")


def abort_stale_multiparts(older_than_seconds: int = 86400, *, dry_run: bool = False) -> int:
    """清掉超时未完成的分片上传（进程被杀 / 超限中断留下的）。

    `upload_fileobj` 那条路拿不到 upload_id，只能靠这里兜底；本模块自己管分片时已经
    abort 过了，这条是给其它客户端与崩溃场景的保险。`dry_run` 只数不 abort。
    """
    from datetime import UTC, datetime, timedelta

    s3 = client()
    b = bucket()
    cutoff = datetime.now(UTC) - timedelta(seconds=older_than_seconds)
    aborted = 0
    kw: dict[str, Any] = {"Bucket": b}
    while True:
        page = s3.list_multipart_uploads(**kw)
        for up in page.get("Uploads", []):
            initiated = up.get("Initiated")
            if initiated is not None and initiated < cutoff:
                if not dry_run:
                    s3.abort_multipart_upload(Bucket=b, Key=up["Key"], UploadId=up["UploadId"])
                aborted += 1
        if not page.get("IsTruncated"):
            return aborted
        kw["KeyMarker"] = page.get("NextKeyMarker", "")
        kw["UploadIdMarker"] = page.get("NextUploadIdMarker", "")


def reap_tmp(older_than_seconds: int = 86400, *, dry_run: bool = False) -> int:
    """删掉 `tmp/` 下超时的上传中间对象，返回处理掉的个数。

    上传的写序是 `tmp/<uuid>/<name>` → copy 到正式 key → PG 事务 → 删 tmp。进程被杀、
    事务失败或调用方放弃时 tmp 会留下，而对象存储不会自己回收（实测见过残留）。
    超时口径按对象的最后写入时间，`older_than_seconds` 要大于一次上传的最长耗时。
    """
    from datetime import UTC, datetime, timedelta

    s3 = client()
    b = bucket()
    cutoff = datetime.now(UTC) - timedelta(seconds=older_than_seconds)
    removed = 0
    token: str | None = None
    while True:
        kw: dict[str, Any] = {"Bucket": b, "Prefix": "tmp/"}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        stale = [
            {"Key": o["Key"]}
            for o in page.get("Contents", [])
            # 没有 LastModified 的就不动它：宁可留垃圾也不误删正在上传的
            if o.get("LastModified") is not None and o["LastModified"] < cutoff
        ]
        for i in range(0, len(stale), _DELETE_BATCH):
            batch = stale[i : i + _DELETE_BATCH]
            if not dry_run:
                s3.delete_objects(Bucket=b, Delete={"Objects": batch})
            removed += len(batch)
        if not page.get("IsTruncated"):
            return removed
        token = page.get("NextContinuationToken")

# ---------- 内部 ----------


def _is_missing(e: Exception) -> bool:
    response = getattr(e, "response", None) or {}
    code = str(response.get("Error", {}).get("Code", ""))
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in {"404", "NoSuchKey", "NotFound"} or status == 404


def ensure_bucket() -> None:
    """启动时调一次：bucket 不存在就建（幂等）。"""
    s3 = client()
    b = bucket()
    try:
        s3.head_bucket(Bucket=b)
    except Exception:  # noqa: BLE001 —— 不存在与无权都是这一句，直接尝试创建
        s3.create_bucket(Bucket=b)


__all__ = [
    "BlobTooLarge",
    "BlobUnavailable",
    "abort_stale_multiparts",
    "blob_key",
    "bucket",
    "client",
    "configured",
    "copy",
    "delete",
    "delete_prefix",
    "download",
    "ensure_bucket",
    "get",
    "index_key",
    "put_stream",
    "reap_tmp",
    "reset_client",
    "session_prefix",
    "stat",
    "tmp_key",
]
