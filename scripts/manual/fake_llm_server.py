"""假的 OpenAI 兼容服务：按固定节奏吐 SSE token，用来造长时任务与并发长对话。

必须独立进程跑，别和后端挤在同一个事件循环里 —— 后端的 worker 租约心跳
（`src/aegra_api/services/worker_executor.py:_heartbeat_loop`）也跑在 uvicorn 的 loop 上，
同步 sleep 或阻塞 IO 会把它饿死，30s 租约一过期 reaper 就会把 run 收回重跑。

    python scripts/manual/fake_llm_server.py --port 2027 --tokens 180 --interval 0.5

默认 180 片 × 0.5s ≈ 90s，是 LEASE_DURATION_SECONDS(30) 的 3 倍，足够看到 8 次以上续租。
统计在 GET /__stats，POST /__reset 清零；并发期间用 chat_completions 断言"没有重复执行"。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()

TOKENS = 180
INTERVAL = 0.5
STARTED_AT = time.time()
CHAT_COMPLETIONS = 0
STREAMS_OPEN = 0
STREAMS_FINISHED = 0


def _chunk(completion_id: str, model: str, delta: dict, finish: str | None = None) -> str:
    body = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    if finish is not None:
        body["usage"] = {"prompt_tokens": 1, "completion_tokens": TOKENS, "total_tokens": TOKENS + 1}
    return f"data: {json.dumps(body, ensure_ascii=False)}\n\n"


async def _stream(completion_id: str, model: str, index: int):  # noqa: ANN201 — async generator
    global STREAMS_OPEN, STREAMS_FINISHED
    STREAMS_OPEN += 1
    jitter = 0.8 + 0.2 * (index % 5)  # 每条流一个固定抖动，避免 20 条流整齐地同一毫秒收尾
    try:
        yield _chunk(completion_id, model, {"role": "assistant", "content": ""})
        for i in range(TOKENS):
            await asyncio.sleep(INTERVAL * jitter)
            yield _chunk(completion_id, model, {"content": f"段{i}，"})
        yield _chunk(completion_id, model, {}, finish="stop")
        yield "data: [DONE]\n\n"
        STREAMS_FINISHED += 1
    finally:
        STREAMS_OPEN -= 1


@app.get("/v1/models")
async def models() -> dict:
    return {"object": "list", "data": [{"id": "fake", "object": "model", "owned_by": "fake"}]}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> object:
    global CHAT_COMPLETIONS
    body = await request.json()
    model = body.get("model") or "fake"
    index = CHAT_COMPLETIONS
    CHAT_COMPLETIONS += 1
    completion_id = f"chatcmpl-fake-{index}"

    if not body.get("stream"):
        # 非流式也得很慢：gateway 默认不向模型要流（agent 节点是 ainvoke），
        # 这里不 sleep 的话“长任务”2 秒就结束了，心跳根本没机会被验证。
        await asyncio.sleep(TOKENS * INTERVAL)
        text = "".join(f"段{i}，" for i in range(TOKENS))
        return JSONResponse(
            {
                "id": completion_id,
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        )

    return StreamingResponse(
        _stream(completion_id, model, index),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/__stats")
async def stats() -> dict:
    return {
        "chat_completions": CHAT_COMPLETIONS,
        "streams_open": STREAMS_OPEN,
        "streams_finished": STREAMS_FINISHED,
        "tokens": TOKENS,
        "interval": INTERVAL,
        "elapsed": round(time.time() - STARTED_AT, 1),
    }


@app.post("/__reset")
async def reset() -> dict:
    global CHAT_COMPLETIONS, STREAMS_OPEN, STREAMS_FINISHED, STARTED_AT
    CHAT_COMPLETIONS = STREAMS_OPEN = STREAMS_FINISHED = 0
    STARTED_AT = time.time()
    return {"ok": True}


def main() -> None:
    global TOKENS, INTERVAL
    parser = argparse.ArgumentParser(description="假的 OpenAI 兼容流式服务")
    parser.add_argument("--port", type=int, default=2027)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--tokens", type=int, default=180, help="每条流吐多少片")
    parser.add_argument("--interval", type=float, default=0.5, help="每片间隔秒数")
    args = parser.parse_args()

    TOKENS = args.tokens
    INTERVAL = args.interval
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
