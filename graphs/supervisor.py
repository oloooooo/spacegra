import asyncio
from typing import Annotated

import dotenv
import structlog
from deepagents import AsyncSubAgent, create_deep_agent
from deepagents.middleware import FilesystemPermission
from pydantic import BaseModel

from graphs.model import model
from shared import config

# 服务身份的 token 只在 custom/service/account.py 铸造（单一来源）。
# 导入不读 env，真正的读取发生在下方 service_token() 调用时（已在 load_dotenv 之后）。
from shared.token import service_token

dotenv.load_dotenv()


class deepAgentContext(BaseModel):
    user_kind: str | None = None


from langchain.agents.middleware import TodoListMiddleware
from langchain.tools import InjectedToolArg, ToolRuntime, tool
from langgraph.graph.state import RunnableConfig

from graphs.access.backend import SpaceBackend
from graphs.access.mirage_backend import for_agent, workspace_for
from graphs.access.spaces import allowed_spaces, upload_scope
from graphs.middleware.async_task_guard import AsyncTaskGuard
from graphs.prompts import MAIN_SYSTEM_PROMPT
from graphs.tools import MAIN_TOOLS
from shared.knowledge.spec import knowledge_root, list_spaces, load_agent_spec, space_dir
from shared.session.upload_rule import MAIN_AGENT, XLSX_AGENT

logger = structlog.getLogger(__name__)


@tool(description="该工具可以返回，用户id，用户名，用户权限，在无法获取相关内容的时候，返回错误信息")
async def get_user_data(config: Annotated[RunnableConfig, InjectedToolArg], runtime: ToolRuntime) -> dict:
    try:
        """Tool that accesses the authenticated user."""
        auth_user = config["configurable"][  # pyright: ignore[reportTypedDictNotRequiredAccess]
            "langgraph_auth_user"
        ]
        if not isinstance(auth_user, dict):
            auth_user = auth_user.to_dict()
        assert auth_user is not None, "获取auth_user失败"
        logger.info(type(auth_user))
        user_id = auth_user["identity"]
        permissions = auth_user.get("permissions", [])
        user_name = auth_user.get("display_name", "")
        user_spaces = auth_user.get("user_spaces", [])
        return {
            "user_id": user_id,
            "permissions": permissions,
            "user_name": user_name,
            "user_spaces": user_spaces,
        }
    except Exception as e:
        return {"error": str(e)}


# 静态服务 token：subagent 的 runs.create 不带终端用户 JWT（deepagents 不转发），
# 用服务身份的长效 token（铸造在 custom/service/account.py::service_token，单一来源）；
# custom_auth.py 对该 identity 免 DB 直接放行。
_service_token = service_token()

# 子 agent 图（aegra.json 里注册的两个）
_GRAPH_ID = "domain_agent"  # 领域子 agent：一个图服务所有空间
_XLSX_GRAPH_ID = "xlsx_agent"  # 通用 xlsx 子 agent：只看得见 /uploads
# 服务自己调自己的地址；容器/换端口在 config.yaml 的 agents.server_url 改
_SERVER_URL = config.get("agents.server_url")


def _mirage_mode() -> bool:
    """读文件走 Mirage（对象存储）还是本地磁盘（`okf.backend`）。见 ADR-0018。"""
    return config.get("okf.backend") == "s3fs"


def _visible_spaces(config: RunnableConfig) -> list[str]:
    """本 run 能看见的空间名（无鉴权上下文 = 知识根下全部）。"""
    spaces = allowed_spaces(config)
    return sorted(spaces) if spaces is not None else list_spaces()


def domain_specs(config: RunnableConfig) -> dict[str, dict]:
    """按本次 run 的 spaces 现算 AsyncSubAgent 列表（name = space 名 = 目录名）。

    末尾固定加一个通用 xlsx 子 agent（`xlsx`）：主 agent **不做表格分析**，认不出空间前缀的
    上传表格（`route_upload` 返回 `xlsx`）都丢给它，它只看得见 `/uploads`。
    无鉴权上下文（本地 demo）时退到知识根下所有目录，否则只看自己拥有的 space。
    """
    specs: dict[str, dict] = {}
    for name in _visible_spaces(config):
        if not space_dir(name).is_dir():  # 表里有但目录没了：跳过，不让模型选到死链
            logger.warning("space_dir_missing", space=name)
            continue
        description, _ = load_agent_spec(name)
        specs[name] = AsyncSubAgent(  # pyright: ignore[reportCallIssue]
            name=name,
            description=description,
            graph_id=_GRAPH_ID,
            url=_SERVER_URL,
            headers={"Authorization": f"Bearer {_service_token}"},
            space=name,  # type: ignore[typeddict-unknown-key]  guard 专用
        )
    specs[XLSX_AGENT] = AsyncSubAgent(  # pyright: ignore[reportCallIssue]
        name=XLSX_AGENT,
        description=(
            "通用表格处理：读 /uploads 下的 xlsx 并回答/统计/取数。任何与 <空间名>_ 前缀无关的上传表格都交给它。"
        ),
        graph_id=_XLSX_GRAPH_ID,
        url=_SERVER_URL,
        headers={"Authorization": f"Bearer {_service_token}"},
        space="",  # type: ignore[typeddict-unknown-key]  guard 专用：空 = 不给空间
        spaces=[],  # type: ignore[typeddict-unknown-key]  子 run 只看得见 /uploads
    )
    return specs


def supervisor_backend(config: RunnableConfig):  # noqa: ANN201 —— 两种 backend 之一
    """主管的存储路由：本地磁盘按 space 隔离（ADR-0005）；s3fs 走 Mirage（ADR-0018）。"""
    if not _mirage_mode():
        return SpaceBackend(root_dir=knowledge_root())
    owner, thread_id = upload_scope(config)
    logger.info("mirage_backend", owner=owner, thread_id=thread_id)
    workspace = workspace_for(
        owner=owner,
        thread_id=thread_id,
        agents={MAIN_AGENT: _visible_spaces(config), XLSX_AGENT: ()},
    )
    return for_agent(workspace, MAIN_AGENT)


def graph(config: RunnableConfig):  # noqa: ANN201 — 返回编译后的图
    """主管图（工厂图）：子 agent 名单每次 run 现算，见 domain_specs。"""
    specs = domain_specs(config)
    return create_deep_agent(
        model=model,
        system_prompt=MAIN_SYSTEM_PROMPT,
        # xlsx 工具不在主 agent 手里：主 agent 能看见所有上传文件，但表格分析一律派给
        # `xlsx` 子 agent（它的 profile 只 show /uploads）。ADR-0018。
        tools=[*MAIN_TOOLS],
        # 按空间隔离的 backend（ADR-0005 / ADR-0018）
        backend=supervisor_backend(config),
        context_schema=deepAgentContext,
        # 拆任务清单（write_todos）；guard 拦截 start/update_async_task：按 spec 绑定的
        # space 经 run config 转发给子 agent（ADR-0005）+ 并发上限
        middleware=[
            TodoListMiddleware(),
            AsyncTaskGuard(specs),
        ],
        subagents=[*specs.values()],
        permissions=[FilesystemPermission(operations=["write"], paths=["/"], mode="deny")],
    )


if __name__ == "__main__":
    message = {"messages": [{"role": "user", "content": "hello,表格消融实验1.xlsx中的CER，最低的有哪些"}]}
    # guard 只实现 async hook（abefore_agent），同步 invoke 会报
    # "No synchronous function provided"；工厂图要先拿 config 建一次
    response = asyncio.run(graph({}).ainvoke(message))  # pyright: ignore[reportArgumentType]
    print(response["messages"][-1]["content"])
