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
from graphs.access.spaces import allowed_spaces
from graphs.middleware.async_task_guard import AsyncTaskGuard
from graphs.prompts import MAIN_SYSTEM_PROMPT
from graphs.tools import MAIN_TOOLS, XLSX_TOOLS
from shared.knowledge.spec import knowledge_root, list_spaces, load_agent_spec, space_dir

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

# 领域子 agent：(agent 名 = space 名, 绑定同一个通用图)。
# space 经拦截层单发给子 agent（guard._config_for），用户没有该 space 权限时
# start 直接拒绝；绑定只在 guard spec 里，deepagents 不读多余键。
#
# **不在这里写名单**：可用子 agent = 当前这个主管自己拥有的 spaces（服务端注入的
# langgraph_auth_user.user_spaces）。加一个 space 就多一个子 agent，删一个就少一个，
# 代码不用动。
_GRAPH_ID = "domain_agent"  # aegra.json 里那个唯一域子 agent 图的 key
# 服务自己调自己的地址；容器/换端口在 config.yaml 的 agents.server_url 改
_SERVER_URL = config.get("agents.server_url")


def domain_specs(config: RunnableConfig) -> dict[str, dict]:
    """按本次 run 的 spaces 现算 AsyncSubAgent 列表（name = space 名 = 目录名）。

    无鉴权上下文（本地 demo）时退到知识根下所有目录，否则只看自己拥有的 space。
    """
    spaces = allowed_spaces(config)
    names = sorted(spaces) if spaces is not None else list_spaces()
    specs: dict[str, dict] = {}
    for name in names:
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
    return specs


def supervisor_backend() -> SpaceBackend:
    """主管的存储路由：按 space 隔离（ADR-0005）。"""
    return SpaceBackend(root_dir=knowledge_root())


def graph(config: RunnableConfig):  # noqa: ANN201 — 返回编译后的图
    """主管图（工厂图）：子 agent 名单每次 run 现算，见 domain_specs。"""
    specs = domain_specs(config)
    return create_deep_agent(
        model=model,
        system_prompt=MAIN_SYSTEM_PROMPT,
        tools=[*MAIN_TOOLS, *XLSX_TOOLS],
        # 按空间隔离的 backend（ADR-0005）
        backend=supervisor_backend(),
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
