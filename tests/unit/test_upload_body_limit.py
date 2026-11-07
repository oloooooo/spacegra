"""上传体积闸门（#8）：body 落盘之前就 413。"""

from __future__ import annotations

from custom.middleware.body_limit import UploadBodyLimit


async def _drain_app(scope, receive, send) -> None:
    while True:
        message = await receive()
        if message["type"] == "http.request" and not message.get("more_body"):
            break
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


class _Recorder:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def __call__(self, message: dict) -> None:
        self.messages.append(message)

    @property
    def status(self) -> int | None:
        return self.messages[0]["status"] if self.messages else None


def _scope(*, headers=(), path: str = "/custom/uploads") -> dict:
    return {"type": "http", "path": path, "headers": list(headers)}


def _chunks(*bodies: bytes):
    pending = list(bodies)

    async def receive() -> dict:
        body = pending.pop(0)
        return {"type": "http.request", "body": body, "more_body": bool(pending)}

    return receive


async def test_rejects_declared_oversize_without_reading_body() -> None:
    sends = _Recorder()

    await UploadBodyLimit(_drain_app, limit=10)(_scope(headers=[(b"content-length", b"11")]), None, sends)

    assert sends.status == 413


async def test_counts_chunked_body() -> None:
    sends = _Recorder()

    await UploadBodyLimit(_drain_app, limit=10)(_scope(), _chunks(b"x" * 6, b"y" * 6), sends)

    assert sends.status == 413


async def test_allows_body_under_limit() -> None:
    sends = _Recorder()

    await UploadBodyLimit(_drain_app, limit=10)(_scope(), _chunks(b"x" * 4, b"y" * 4), sends)

    assert sends.status == 200


async def test_other_paths_are_not_limited() -> None:
    sends = _Recorder()

    await UploadBodyLimit(_drain_app, limit=1)(_scope(headers=[(b"content-length", b"999")], path="/threads/x/runs"), _chunks(b"ok"), sends)

    assert sends.status == 200
