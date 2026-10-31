from contextlib import asynccontextmanager

from fastapi import FastAPI

from custom.api.account import router as account_router
from custom.api.delivery import router as delivery_router
from custom.api.graphs import router as graphs_router
from custom.api.spaces import router as spaces_router
from custom.api.tasks import router as tasks_router
from custom.service.outbox_dispatcher import dispatcher as outbox_dispatcher


@asynccontextmanager
async def lifespan(app: FastAPI):
    """发件箱分发者（ADR-0010）。

    分发者每个进程跑一个，多进程靠 SKIP LOCKED + 租约分工。
    """
    await outbox_dispatcher.start()
    try:
        yield
    finally:
        await outbox_dispatcher.stop()


app = FastAPI(lifespan=lifespan)

app.include_router(account_router)
app.include_router(spaces_router)
app.include_router(graphs_router)
app.include_router(tasks_router)
app.include_router(delivery_router)


def _openapi_with_bearer():
    """给 OpenAPI 补上 bearer securitySchemes，让 /docs 出现 Authorize 按钮。

    Aegra 的鉴权走中间件 + require_auth(Request)，FastAPI 生成 schema 时
    看不到它们，所以这里手动声明；全局 security 让每次 Try it out 都带上 token。
    """
    if not app.openapi_schema:
        schema = FastAPI.openapi(app)
        schema.setdefault("components", {})["securitySchemes"] = {
            "bearerAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"},
        }
        schema["security"] = [{"bearerAuth": []}]
        app.openapi_schema = schema
    return app.openapi_schema


app.openapi = _openapi_with_bearer
