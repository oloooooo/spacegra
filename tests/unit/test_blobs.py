"""`shared/session/blobs.py` 的纯逻辑测试：不碰真 S3，用假 client 记调用。

重点锁三件事：key 命名（删会话靠前缀）、sha256/字节数、**超限时必须 abort 分片**
（`upload_fileobj` 那条路做不到，只能自己管）。
"""

import hashlib
from typing import Any

import pytest

from shared.session import blobs
from shared.session.store import session_namespace


class FakeS3:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.objects: dict[str, bytes] = {}
        self.aborted: list[str] = []
        self._mp = 0
        self.missing = False
        # reap_tmp / abort_stale_multiparts 用到的两个视图
        self.mtimes: dict[str, Any] = {}
        self.deleted: list[str] = []
        self.uploads: list[dict] = []

    def put_object(self, **kw):
        self.calls.append(("put_object", kw))
        self.objects[kw["Key"]] = kw["Body"]

    def create_multipart_upload(self, **kw):
        self.calls.append(("create_multipart_upload", kw))
        self._mp += 1
        return {"UploadId": f"mp-{self._mp}"}

    def upload_part(self, **kw):
        self.calls.append(("upload_part", kw))
        return {"ETag": f'"part{kw["PartNumber"]}"'}

    def complete_multipart_upload(self, **kw):
        self.calls.append(("complete_multipart_upload", kw))

    def abort_multipart_upload(self, **kw):
        self.calls.append(("abort_multipart_upload", kw))
        self.aborted.append(kw["UploadId"])

    def head_object(self, **kw):
        self.calls.append(("head_object", kw))
        if self.missing:
            from botocore.exceptions import ClientError

            raise ClientError(
                {"Error": {"Code": "404", "Message": "Not Found"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
                "HeadObject",
            )
        return {"ContentLength": len(self.objects[kw["Key"]])}

    def list_objects_v2(self, **kw):
        self.calls.append(("list_objects_v2", kw))
        prefix = kw.get("Prefix", "")
        return {
            "Contents": [
                {"Key": k, "Size": len(v), "LastModified": self.mtimes.get(k)}
                for k, v in self.objects.items()
                if k.startswith(prefix)
            ],
            "IsTruncated": False,
        }

    def delete_objects(self, **kw):
        self.calls.append(("delete_objects", kw))
        for o in kw["Delete"]["Objects"]:
            self.deleted.append(o["Key"])
            self.objects.pop(o["Key"], None)

    def list_multipart_uploads(self, **kw):
        self.calls.append(("list_multipart_uploads", kw))
        return {"Uploads": list(self.uploads), "IsTruncated": False}

    def names(self):
        return [c[0] for c in self.calls]


@pytest.fixture
def s3(monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(blobs, "client", lambda: fake)
    return fake


def test_identity_digest_matches_store_namespace():
    """Store 命名空间与对象存储前缀必须用同一个哈希，否则同一份内容落在两套前缀下。"""
    assert blobs.session_prefix("张三", "t-1") == f"sessions/{session_namespace('张三', 't-1')[1]}/t-1/"


def test_keys_are_under_session_prefix():
    p = blobs.session_prefix("u1", "t1")
    assert blobs.blob_key("u1", "t1", "a" * 64, "x.xlsx") == f"{p}{'a' * 16}/x.xlsx"
    assert blobs.index_key("u1", "t1", "x.xlsx", 0, 3) == f"{p}idx/x.xlsx.h0.v3.sqlite"
    assert blobs.tmp_key("x.xlsx").startswith("tmp/")
    assert blobs.tmp_key("x.xlsx") != blobs.tmp_key("x.xlsx")


def test_put_stream_single_chunk_uses_put_object(s3):
    data = b"hello world"
    sha, n = blobs.put_stream(__import__("io").BytesIO(data), "k", max_bytes=1024)

    assert (sha, n) == (hashlib.sha256(data).hexdigest(), len(data))
    assert s3.names() == ["put_object"]
    assert s3.objects["k"] == data  # ETag=md5 便于对账


def test_put_stream_empty_file(s3):
    sha, n = blobs.put_stream(__import__("io").BytesIO(b""), "k", max_bytes=1024)

    assert (sha, n) == (hashlib.sha256(b"").hexdigest(), 0)
    assert s3.names() == ["put_object"]
    assert s3.objects["k"] == b""


def test_put_stream_multi_chunk_parts_in_order(s3):
    data = b"a" * blobs._PART_BYTES + b"b" * blobs._PART_BYTES + b"c" * 5
    sha, n = blobs.put_stream(__import__("io").BytesIO(data), "k", max_bytes=100 * 1024 * 1024)

    assert (sha, n) == (hashlib.sha256(data).hexdigest(), len(data))
    assert s3.names() == [
        "create_multipart_upload",
        "upload_part",
        "upload_part",
        "upload_part",
        "complete_multipart_upload",
    ]
    parts = [c[1] for c in s3.calls if c[0] == "upload_part"]
    assert [p["PartNumber"] for p in parts] == [1, 2, 3]
    assert [len(p["Body"]) for p in parts] == [blobs._PART_BYTES, blobs._PART_BYTES, 5]
    assert s3.aborted == []


def test_put_stream_over_limit_aborts_multipart(s3):
    """第二片把累计顶超上限：必须 abort，否则未完成分片会一直占空间。"""
    data = b"a" * blobs._PART_BYTES * 2
    with pytest.raises(blobs.BlobTooLarge):
        blobs.put_stream(__import__("io").BytesIO(data), "k", max_bytes=blobs._PART_BYTES + 10)

    assert "abort_multipart_upload" in s3.names()
    assert s3.aborted == ["mp-1"]


def test_put_stream_over_limit_first_chunk_touches_nothing(s3):
    """第一片就超：不该往 S3 发任何请求。"""
    with pytest.raises(blobs.BlobTooLarge):
        blobs.put_stream(__import__("io").BytesIO(b"x" * 100), "k", max_bytes=10)

    assert s3.calls == []


def test_stat_returns_none_on_404(s3):
    s3.missing = True
    assert blobs.stat("k") is None


def test_stat_reraises_other_errors(s3):
    """只有 404 该被吞；存储不可用要往上抛，不能被当成「文件不存在」。"""

    def boom(**kw):
        from botocore.exceptions import ClientError

        raise ClientError(
            {"Error": {"Code": "503", "Message": "slow down"}, "ResponseMetadata": {"HTTPStatusCode": 503}},
            "HeadObject",
        )

    s3.head_object = boom
    with pytest.raises(Exception, match="slow down"):
        blobs.stat("k")


def test_delete_prefix_pages_and_batches(monkeypatch):
    """跨分页 + 单批 1000 上限：删会话时可能一次几百个对象。"""
    pages = {
        None: {
            "Contents": [{"Key": f"p/{i}"} for i in range(1000)],
            "IsTruncated": True,
            "NextContinuationToken": "t2",
        },
        "t2": {"Contents": [{"Key": "p/z"}], "IsTruncated": False},
    }
    batches: list[int] = []

    class PagingS3:
        def list_objects_v2(self, **kw):
            return pages[kw.get("ContinuationToken")]

        def delete_objects(self, **kw):
            batches.append(len(kw["Delete"]["Objects"]))

    monkeypatch.setattr(blobs, "client", lambda: PagingS3())
    assert blobs.delete_prefix("p/") == 1001
    assert batches == [1000, 1]


def test_configured_follows_endpoint(monkeypatch):
    # 直接盯判据：空串 = 未配置（env 空串会让位给 config.yaml，见 shared/config.get）
    monkeypatch.setattr(blobs.config, "get", lambda key: "http://x:9000" if key == "s3.endpoint" else None)
    assert blobs.configured() is True
    monkeypatch.setattr(blobs.config, "get", lambda key: "")
    assert blobs.configured() is False


# ---------- 上传垃圾清理（scripts/reap_multipart.py） ----------


def _ago(hours: float):
    from datetime import UTC, datetime, timedelta

    return datetime.now(UTC) - timedelta(hours=hours)


def test_reap_tmp_deletes_only_stale_objects(s3):
    """只清超时的 tmp/：正在上传的（新）不能动，正式 key 更不能碰。"""
    s3.objects["tmp/old-a/x.xlsx"] = b"stale"
    s3.objects["tmp/new-b/y.xlsx"] = b"fresh"
    s3.objects["sessions/h/t1/abc/x.xlsx"] = b"real"
    s3.mtimes = {
        "tmp/old-a/x.xlsx": _ago(48),
        "tmp/new-b/y.xlsx": _ago(1),
        "sessions/h/t1/abc/x.xlsx": _ago(1000),
    }

    assert blobs.reap_tmp(24 * 3600) == 1
    assert s3.deleted == ["tmp/old-a/x.xlsx"]
    assert "tmp/new-b/y.xlsx" in s3.objects and "sessions/h/t1/abc/x.xlsx" in s3.objects


def test_reap_tmp_skips_objects_without_timestamp(s3):
    """拿不到 LastModified 就不删：宁可留垃圾也不误删。"""
    s3.objects["tmp/no-ts/x.xlsx"] = b"?"
    assert blobs.reap_tmp(0) == 0
    assert s3.deleted == []


def test_reap_tmp_dry_run_deletes_nothing(s3):
    s3.objects["tmp/old/x.xlsx"] = b"stale"
    s3.mtimes = {"tmp/old/x.xlsx": _ago(48)}
    assert blobs.reap_tmp(24 * 3600, dry_run=True) == 1
    assert s3.deleted == [] and "tmp/old/x.xlsx" in s3.objects


def test_abort_stale_multiparts_dry_run_counts_without_aborting(s3):
    s3.uploads = [{"Key": "a.xlsx", "UploadId": "mp-old", "Initiated": _ago(30)}]
    assert blobs.abort_stale_multiparts(24 * 3600, dry_run=True) == 1
    assert s3.aborted == []
    assert blobs.abort_stale_multiparts(24 * 3600) == 1
    assert s3.aborted == ["mp-old"]
