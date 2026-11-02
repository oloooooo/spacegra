"""上传体积闸门：body 落盘之前就拒绝（#8）。

Starlette 先把整个 multipart 解析完才进 handler，等看到 `UploadFile.size` 时字节
已经写进临时文件了；`max_part_size` 只约束非文件字段，文件 part 不受它管。这里在
ASGI 层拦 `/custom/uploads`：有 `Content-Length` 就直接判，没有（chunked）就包一层
`receive` 边收边数。

上限 = 两类文件上限里的较大者 + 2MB 余量（multipart 头、其他表单字段），不是精确值
——精确值仍由 `put_stream` 的逐块计数负责。
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from shared import config

_SLACK = 2 * 1024 * 1024


def upload_body_limit() -> int:
    mb = max(int(config.get("xlsx.max_blob_mb")), int(config.get("session_files.max_upload_mb")), 1)
    return mb * 1024 * 1024 + _SLACK


class _TooLarge(Exception):
    pass


async def _reject(send: Send, limit: int) -> None:
    body = f'{{"detail":"上传内容超过 {limit // (1024 * 1024)}MB 上限"}}'.encode()
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": body})


class UploadBodyLimit:
    def __init__(self, app: ASGIApp, *, path: str = "/custom/uploads", limit: int | None = None) -> None:
        self.app = app
        self.path = path
        self.limit = limit or upload_body_limit()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"].rstrip("/") != self.path:
            await self.app(scope, receive, send)
            return
        headers = {k.lower(): v for k, v in scope.get("headers") or []}
        declared = headers.get(b"content-length")
        if declared and declared.isdigit() and int(declared) > self.limit:
            await _reject(send, self.limit)
            return

        seen = 0

        async def counting() -> Message:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body") or b"")
                if seen > self.limit:
                    raise _TooLarge
            return message

        try:
            await self.app(scope, counting, send)
        except _TooLarge:
            await _reject(send, self.limit)
