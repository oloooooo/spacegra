"""拦截 async subagent 的派活与结果投递：spaces 收窄、子线程归属、结果写回主管 state。

===============================================================================
一、它拦哪两个工具
===============================================================================

deepagents 内置的 `start_async_task` / `update_async_task` 建子 run 时不带任何 config
（`AsyncSubAgent` spec 没有 config 字段，内置工具也没有关闭开关），所以我们短路
handler，自己用 spec 里的静态服务 token 建 thread/run，把两样东西塞进去：
权限（只给本任务的 space）与归属（哪个终端用户拥有这个子线程）。

check / cancel / list 不创建 run，直接放行给内置 handler；check 到终态时额外记一笔
"这个结果已经给过主管了"并 ack 发件箱行（见 `_amark_checked`）。

===============================================================================
二、派活路径
===============================================================================

    start_async_task(subagent_type="cbam-reporting", description="…")
      │
      │ 1. _prepare()        subagent_type -> spec（名单来自 aegra.json 的图 + space）
      │ 2. _config_for()     what the child may read
      │      configurable.user_spaces = ["cbam-reporting"]     ← 只给本任务的那个
      │      （spec 绑了 space 时还先校验主管有该 space，没有就拒绝）
      │ 3. _thread_meta()   what the child thread is owned by
      │      metadata = {owner_identity: <终端用户>, parent_thread_id: <主管>}
      │      （子线程属主是服务账号；parent_thread_id 同时是 finalize_run 写发件箱行的判据）
      │ 4. POST /threads -> POST /threads/{tid}/runs
      │      stream_mode=["messages","custom"]（前端代理要订阅子 agent 实时输出）
      ▼
    Command(update={messages: [ToolMessage("Launched … task_id: …")],
                    async_tasks: {task_id: {...}}})

**没有并发上限**：一个主管能派多少个子 agent 不受限制。规模由 thread 配额
（ADR-0012）与 worker 队列（`N_JOBS_PER_WORKER`）承接，超额的子 run 在服务端排队。

===============================================================================
三、投递路径：把结果写进主管自己的 state
===============================================================================

子 agent 跑完，结果要进主管上下文。主管 run 中途没有任何 API 能往里塞消息，
所以只有一个时刻：run 开始（before_agent）。run 期间完成的结果不在这里写——
那一条模型已经没机会转述，写进去等于少一次能反应的机会；交给分发者开唤醒 run。

    before_agent
      │ 1. _refreshed_async()  向子 run 问真实状态（state 里的 status 只是快照）
      │ 2. _plan()             谁已终态、且结果还没进过主管上下文
      │ 3. POST /custom/tasks/{id}/deliver-claim   抢投递权（与发件箱分发者互斥）
      │ 4. GET /threads/{child}                    取子 agent 最后一条消息当摘要
      │ 5. 返回 {"messages": [HumanMessage("[Async subagent … ] Result: …")],
      │          "async_tasks": {task_id: {..., delivered_run_id: run_id}}}
      ▼
    本次 run 的 checkpoint 里多一条 human 消息
    （前端 `toView` 靠 `[Async subagent` 前缀渲染成「子 agent 返回」卡片）

结果正文**不截断**（超过 3000 字只打告警），模型一次就能看到全量。

判重两个来源：发件箱行 `(task_id, run_id)`（与分发者互斥：谁能把行从 pending 翻成
delivered，谁去写 state）、task 上的 `delivered_run_id`（模型自己 check 过也算已投，
见 `_amark_checked`）。**不再扫 messages**：摘要会删掉卡片、模型又能复述伪造它。

===============================================================================
四、多进程
===============================================================================

guard 自己不存任何跨请求状态（旧版的名额账本已随 ADR-0008 删除）。所有跨进程
判重都落在两处服务端：
  - `async_task_outbox(task_id, run_id)`：管“这次结果投没投过”（ADR-0010）；
  - `thread.metadata_json`：管“这个子线程是谁的”（ADR-0012）。

===============================================================================
五、代码分区（改代码前先看这张表）
===============================================================================

  A. 文本与判重    _TERMINAL, _now(), _message_text(),
                   _delivered(), _completion_text(),
                   _thread_summary()
  B. 派活          AsyncTaskGuard._key/_sync_client/_async_client, _prepare(),
                   _config_for(), _thread_meta(), _task(), _launched(),
                   _start_sync/_astart, _tracked(), _updated(),
                   _update_sync/_aupdate, _err()
  C. 投递          _spec_of(), _refresh_targets(), _refreshed_sync/_refreshed_async,
                   _summary_async(), _claim_async/_release_async, _plan(),
                   _deliver(), abefore_agent, _amark_checked()
  D. 接线          wrap_tool_call / awrap_tool_call
"""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.config import get_config
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command
from langgraph_sdk import get_client, get_sync_client
from langgraph_sdk.client import LangGraphClient, SyncLangGraphClient

from graphs.access.spaces import allowed_spaces, current_identity
from shared.outbox import completion_text, is_long_result, message_text, notice_text, result_body, task_status_of_run

logger = structlog.getLogger(__name__)

# 子 run 的终态分成两类：能当"结果"投的，和只能当"状态"通知的。
# interrupted 单独归通知类——它意味着这一轮没跑完，把半截内容当结论投给主管是误导。
_RESULTS = frozenset({"success", "error", "timeout"})
_NOTICES = frozenset({"cancelled", "interrupted"})
_TERMINAL = _RESULTS | _NOTICES

# 投递账本标记：格式定义在 shared/outbox.py（与分发者、finalize_run 共用同一口径）
_TASK_MARK = "task_id: "


# ============================ A. 文本与判重（纯函数） ============================


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _message_text(msg: Any) -> str:
    """消息正文：兼容 str / content 块列表 / dict 三种形态。"""
    return message_text(msg)


def _delivered(task: dict[str, Any]) -> bool:
    """这个 run 的结果是否已经入过主管上下文。

    只认 `delivered_run_id`（state 里的键控字段，随 checkpoint 走），**不扫 messages**：
    ① 摘要中间件用 `RemoveMessage(REMOVE_ALL_MESSAGES)` 重写整段历史，投递卡片的痕迹
    会消失，扫不到就再投一遍；② 模型自己能写出一条带同样前缀的消息，扫到就当成已
    投递，真结果反而永远不投。判重必须偏保守（倾向判"未投"）：多投可见，漏投静默
    且不可恢复。

    代价：state 里没记下的已投任务，本轮会走一次 claim（见 `_deliver`），判过一次就
    写回这里，之后 O(1) 跳过。
    """
    run_id = task.get("run_id") or ""
    return bool(run_id) and task.get("delivered_run_id") == run_id


def _notified(task: dict[str, Any]) -> bool:
    """这个 run 的**状态**是否已经告诉过主管（取消 / 中断）。

    和 `_delivered` 分开两个键：cancelled 没有"结果"，不能写 `delivered_run_id`
    （那会让这条 run 后续真出了结果时被永久跳过）；反过来，结果投递也不该把
    `notified_run_id` 当成证据。
    """
    run_id = task.get("run_id") or ""
    return bool(run_id) and task.get("notified_run_id") == run_id


def _completion_text(task: dict[str, Any], summary: str) -> str:
    """投递消息正文——格式口径在 shared/outbox.py。"""
    return completion_text(task["task_id"], task.get("run_id"), task.get("agent_name"), task.get("status"), summary)


def _thread_summary(thread: Any) -> str:
    """子 agent 的结论 = 它自己线程的最后一条消息，**原文不截断**。"""
    messages = ((thread or {}).get("values") or {}).get("messages") or []
    return result_body(_message_text(messages[-1])) if messages else "(no output)"


class AsyncTaskGuard(AgentMiddleware):
    """拦截 async subagent 的 run 创建：注入 user_spaces、给子线程打归属标记。"""

    def __init__(self, specs: dict[str, dict[str, Any]]) -> None:
        """specs: {agent_name: AsyncSubAgent spec}（名单由 `domain_specs()` 按本次 run 的
        spaces 现算；唤醒 run 不再回写父线程，所以不需要主管自己的 assistant_id）。"""
        self._specs = specs
        self._sync: dict[tuple, SyncLangGraphClient] = {}
        self._async: dict[tuple, LangGraphClient] = {}

    # =========================== B. 派活 ===========================

    # ---------- 客户端（按 url+headers 缓存，与 deepagents _ClientCache 同键） ----------

    @staticmethod
    def _key(spec: dict[str, Any]) -> tuple:
        return (spec.get("url"), frozenset((spec.get("headers") or {}).items()))

    def _sync_client(self, spec: dict[str, Any]) -> SyncLangGraphClient:
        key = self._key(spec)
        if key not in self._sync:
            self._sync[key] = get_sync_client(url=spec["url"], headers=dict(spec.get("headers") or {}))
        return self._sync[key]

    def _async_client(self, spec: dict[str, Any]) -> LangGraphClient:
        key = self._key(spec)
        if key not in self._async:
            self._async[key] = get_client(url=spec["url"], headers=dict(spec.get("headers") or {}))
        return self._async[key]

    # ---------- 前置：校验 + 权限 + 归属 ----------

    def _prepare(self, request: ToolCallRequest) -> tuple[str, dict[str, Any]] | str:
        """(agent 名, spec) 或错误文本。名字要写进 task，供之后的 update/投递反查。"""
        agent_type = (request.tool_call.get("args") or {}).get("subagent_type", "")
        spec = self._specs.get(agent_type)
        if spec is None:
            allowed = ", ".join(f"`{k}`" for k in self._specs)
            return f"Unknown async subagent type `{agent_type}`. Available types: {allowed}"
        return agent_type, spec

    def _config_for(self, request: ToolCallRequest, spec: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        """(子 run 的 configurable, 错误文本)：本任务 space + 父上下文（收尾回写用）。

        spec 绑定了 ``space`` 的子 agent 只发 ``[space]``，并先校验主管有该空间权限；
        没有 space 绑定时回退主管的全量快照。子 run 的 configurable 只放 `user_spaces`：
        父上下文（谁是我的父线程）走 `_thread_meta` 的子线程 metadata，那是 `finalize_run`
        写发件箱行的判据；不再往子 run 里塞父身份/回调地址。
        """
        cfg = request.runtime.config or {}
        space = spec.get("space")
        if space:
            spaces = allowed_spaces(cfg)
            if spaces is not None and space not in spaces:
                return None, f"用户没有空间 {space!r} 的权限，无法启动该子 agent（可用：{sorted(spaces)}）"
            user_spaces: list[str] | None = [space]
        else:
            all_spaces = allowed_spaces(cfg)
            user_spaces = None if all_spaces is None else sorted(all_spaces)
        return {"configurable": {"user_spaces": user_spaces}}, None

    @staticmethod
    def _thread_meta(request: ToolCallRequest) -> dict[str, Any]:
        """子线程的归属标记：属主是服务账号，但配额与级联清理按终端用户算（ADR-0012）。"""
        conf = (request.runtime.config or {}).get("configurable") or {}
        identity = current_identity(request.runtime.config)
        return {
            "owner_identity": None if identity == "local" else identity,
            "parent_thread_id": conf.get("thread_id"),
        }

    @staticmethod
    def _task(task_id: str, agent_name: str, run_id: str) -> dict[str, Any]:
        """新任务记录（thread_id == task_id：子线程就是任务句柄）。"""
        now = _now()
        return {
            "task_id": task_id,
            "agent_name": agent_name,
            "thread_id": task_id,
            "run_id": run_id,
            "status": "running",
            "created_at": now,
            "last_checked_at": now,
            "last_updated_at": now,
        }

    @staticmethod
    def _launched(request: ToolCallRequest, task: dict[str, Any], refreshed: dict[str, dict[str, Any]]) -> Command[Any]:
        """派活成功的 state 更新：工具消息 + 新任务 + 顺手刷新的旧任务状态。"""
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        f"Launched async subagent. task_id: {task['task_id']}",
                        tool_call_id=request.runtime.tool_call_id,
                    )
                ],
                "async_tasks": {**refreshed, task["task_id"]: task},
            }
        )

    def _err(self, request: ToolCallRequest, msg: str) -> ToolMessage:
        """普通错误回给模型（不走 HITL：它自己等一等或换个问法即可）。"""
        return ToolMessage(content=msg, tool_call_id=request.runtime.tool_call_id, name=request.tool_call.get("name"))

    # ---------- start（sync / async 只差 client 与 await） ----------

    def _start_args(
        self, request: ToolCallRequest, spec: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any], str] | str:
        """(子 run config, 子线程 metadata, description) 或错误文本。"""
        config, err = self._config_for(request, spec)
        if err:
            return err
        assert config is not None
        return config, self._thread_meta(request), (request.tool_call.get("args") or {}).get("description", "")

    def _start_sync(self, request: ToolCallRequest) -> ToolMessage | Command[Any]:
        refreshed = self._refreshed_sync((request.state or {}).get("async_tasks") or {})
        prepared = self._prepare(request)
        if isinstance(prepared, str):
            return self._err(request, prepared)
        name, spec = prepared
        args = self._start_args(request, spec)
        if isinstance(args, str):
            return self._err(request, args)
        config, meta, description = args
        try:
            thread = self._sync_client(spec).threads.create(metadata=meta)
            run = self._sync_client(spec).runs.create(
                thread_id=thread["thread_id"],
                assistant_id=spec["graph_id"],
                input={"messages": [{"role": "user", "content": description}]},
                stream_mode=["messages", "custom"],
                config=config,
            )
        except Exception as e:  # noqa: BLE001 — SDK 异常无类型，回给模型
            logger.warning("failed_to_launch_async_subagent", error=str(e))
            return self._err(request, f"Failed to launch async subagent: {e}")
        return self._launched(request, self._task(thread["thread_id"], name, run["run_id"]), refreshed)

    async def _astart(self, request: ToolCallRequest) -> ToolMessage | Command[Any]:
        refreshed = await self._refreshed_async((request.state or {}).get("async_tasks") or {})
        prepared = self._prepare(request)
        if isinstance(prepared, str):
            return self._err(request, prepared)
        name, spec = prepared
        args = self._start_args(request, spec)
        if isinstance(args, str):
            return self._err(request, args)
        config, meta, description = args
        client = self._async_client(spec)
        try:
            thread = await client.threads.create(metadata=meta)
            run = await client.runs.create(
                thread_id=thread["thread_id"],
                assistant_id=spec["graph_id"],
                input={"messages": [{"role": "user", "content": description}]},
                stream_mode=["messages", "custom"],
                config=config,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("failed_to_launch_async_subagent", error=str(e))
            return self._err(request, f"Failed to launch async subagent: {e}")
        return self._launched(request, self._task(thread["thread_id"], name, run["run_id"]), refreshed)

    # ---------- update（新 run 同样要带 config，否则子 agent 中途丢 spaces） ----------

    @staticmethod
    def _tracked(request: ToolCallRequest) -> tuple[dict[str, Any], str] | str:
        """(被更新的任务, 追加给子 agent 的消息) 或错误文本。"""
        args = request.tool_call.get("args") or {}
        task_id = str(args.get("task_id") or "").strip()
        tracked = ((request.state or {}).get("async_tasks") or {}).get(task_id)
        if not tracked:
            return f"No tracked task found for task_id: {task_id!r}"
        return tracked, args.get("message", "")

    def _update_ready(
        self, request: ToolCallRequest
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str] | str:
        """(任务, spec, 子 run config, 追加消息) 或错误文本——update 的前置合并成一处。"""
        tracked = self._tracked(request)
        if isinstance(tracked, str):
            return tracked
        task, message = tracked
        spec = self._specs.get(task["agent_name"])
        if spec is None:
            return f"Unknown agent for task: {task['task_id']!r}"
        config, err = self._config_for(request, spec)
        if err:
            return err
        assert config is not None
        return task, spec, config, message

    @staticmethod
    def _updated(request: ToolCallRequest, task: dict[str, Any], run_id: str) -> Command[Any]:
        """update 成功的 state 更新：任务换新 run、回到 running。"""
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        f"Updated async subagent. task_id: {task['task_id']}",
                        tool_call_id=request.runtime.tool_call_id,
                    )
                ],
                "async_tasks": {
                    task["task_id"]: {**task, "run_id": run_id, "status": "running", "last_updated_at": _now()}
                },
            }
        )

    def _update_sync(self, request: ToolCallRequest) -> ToolMessage | Command[Any]:
        ready = self._update_ready(request)
        if isinstance(ready, str):
            return self._err(request, ready)
        task, spec, config, message = ready
        try:
            run = self._sync_client(spec).runs.create(
                thread_id=task["thread_id"],
                assistant_id=spec["graph_id"],
                input={"messages": [{"role": "user", "content": message}]},
                multitask_strategy="interrupt",
                stream_mode=["messages", "custom"],
                config=config,
            )
        except Exception as e:  # noqa: BLE001
            return self._err(request, f"Failed to update async subagent: {e}")
        return self._updated(request, task, run["run_id"])

    async def _aupdate(self, request: ToolCallRequest) -> ToolMessage | Command[Any]:
        ready = self._update_ready(request)
        if isinstance(ready, str):
            return self._err(request, ready)
        task, spec, config, message = ready
        try:
            run = await self._async_client(spec).runs.create(
                thread_id=task["thread_id"],
                assistant_id=spec["graph_id"],
                input={"messages": [{"role": "user", "content": message}]},
                multitask_strategy="interrupt",
                stream_mode=["messages", "custom"],
                config=config,
            )
        except Exception as e:  # noqa: BLE001
            return self._err(request, f"Failed to update async subagent: {e}")
        return self._updated(request, task, run["run_id"])

    # =========================== C. 投递 ===========================

    def _spec_of(self, task: dict[str, Any]) -> dict[str, Any] | None:
        """任务对应的 spec：优先按 agent_name 找；该 space 已被删时退到任意一个同部署 spec。

        已派出的任务保留当时授权的 space（删 space 不影响旧任务投递），而这里的 spec
        只用来拼 url/headers 去查子 run、取摘要——它们跟 space 无关。

        shortcut: 所有子 agent 跑在同一部署（url/headers 相同），所以借一个就行；
        将来多部署时要把 url 记进 task。
        """
        if not task.get("run_id"):
            return None
        spec = self._specs.get(task.get("agent_name", ""))
        if spec is not None:
            return spec
        return next(iter(self._specs.values()), None)

    @staticmethod
    def _refresh_targets(tasks: dict[str, Any]) -> list[dict[str, Any]]:
        """需要向服务端核实的任务：还非终态、且有 run_id（sync/async 两条路共用）。"""
        return [t for t in tasks.values() if t.get("status") not in _TERMINAL and t.get("run_id")]

    def _refreshed_sync(self, tasks: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """查到的真实状态变化（只含变了的）。查不到就按原状态算，不阻断派活。"""
        out: dict[str, dict[str, Any]] = {}
        for task in self._refresh_targets(tasks):
            spec = self._spec_of(task)
            if spec is None:
                continue
            try:
                run = self._sync_client(spec).runs.get(task["thread_id"], task["run_id"])
            except Exception:  # noqa: BLE001
                continue
            status = task_status_of_run(run)
            if status and status != task.get("status"):
                out[task["task_id"]] = {**task, "status": status, "last_checked_at": _now()}
        return out

    async def _refreshed_async(self, tasks: dict[str, Any]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for task in self._refresh_targets(tasks):
            spec = self._spec_of(task)
            if spec is None:
                continue
            try:
                run = await self._async_client(spec).runs.get(task["thread_id"], task["run_id"])
            except Exception:  # noqa: BLE001
                continue
            status = task_status_of_run(run)
            if status and status != task.get("status"):
                out[task["task_id"]] = {**task, "status": status, "last_checked_at": _now()}
        return out

    async def _summary_async(self, task: dict[str, Any]) -> str:
        """取子 agent 的最终结论，**原文不截断**（模型一次看到全量，不用再调 check）。

        注：发件箱行里也存了一份写入时的摘要快照（分发者用的）；这里现查线程是为了
        拿到**当下**的最后一条消息（同一条 run 里模型可能后来又追加了内容）。
        过长只告警、不切文本：静默截断会让模型把“被切了”当成“写完了”。
        """
        spec = self._spec_of(task)
        if spec is None or not task.get("thread_id"):
            return "(no output)"
        try:
            text = _thread_summary(await self._async_client(spec).threads.get(task["thread_id"]))
        except Exception:  # noqa: BLE001
            return "(summary unavailable)"
        if is_long_result(text):
            logger.warning("task_result_long", task_id=task.get("task_id"), chars=len(text))
        return text

    @staticmethod
    def _claim_endpoint(
        spec: dict[str, Any] | None, task: dict[str, Any], parent_thread_id: str
    ) -> tuple[str, dict[str, Any], dict[str, str]] | None:
        """认领端点 + body + headers；缺信息时返回 None（不认领、但也不阻断投递）。

        认领 = 把发件箱行从 pending 原子翻成 delivered（见 custom/api/delivery.py）。
        没有时间窗口、没有“让路”参数：一行一判，同批多个任务互不影响。
        """
        if not parent_thread_id or spec is None or not spec.get("url"):
            return None
        endpoint = f"{str(spec['url']).rstrip('/')}/custom/tasks/{task['task_id']}/deliver-claim"
        return (
            endpoint,
            {
                "parent_thread_id": parent_thread_id,
                "run_id": task.get("run_id"),
            },
            dict(spec.get("headers") or {}),
        )

    async def _claim_async(self, task: dict[str, Any], parent_thread_id: str) -> bool | None:
        """抢投递权：True 抢到 / False 别人投过 / None 不知道（调用方据此**不投**，等下一轮）。

        None 不能当“没投过”：投了行还是 pending，分发者会再送一遍（两张卡片）。
        行没卖掉就不算漏——端点恢复、下一轮 run 或分发者接手时会再试。
        """
        req = self._claim_endpoint(self._spec_of(task), task, parent_thread_id)
        if req is None:
            return None
        endpoint, body, headers = req
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.post(endpoint, json=body, headers=headers)
                resp.raise_for_status()
                return bool(resp.json().get("claimed"))
        except Exception:  # noqa: BLE001
            logger.warning("task_delivery_claim_failed", task_id=task.get("task_id"))
            return None

    async def _release_async(self, task: dict[str, Any], parent_thread_id: str) -> None:
        """撤销认领：state 没写成时调，否则这次结果会被判为「已投递」而丢掉。"""
        req = self._claim_endpoint(self._spec_of(task), task, parent_thread_id)
        if req is None:
            return
        endpoint, body, headers = req
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                await client.delete(endpoint, params=body, headers=headers)
        except Exception:  # noqa: BLE001
            logger.warning("task_delivery_release_failed", task_id=task.get("task_id"))

    @staticmethod
    def _plan(
        state: Any, fresh: dict[str, str | None]
    ) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        """(要写回 state 的状态更新, 待投递结果, 待发状态通知)。

        结果和通知分成两路，因为判重键不同、投递通道也不同：

        - **结果**（`success` / `error` / `timeout`）：占发件箱行，靠 claim 互斥，
          记 `delivered_run_id`；
        - **状态**（`cancelled` / `interrupted`）：这一轮没跑完，**不能**把半截内容
          当结论。只通知状态，记 `notified_run_id`。

        `fresh` 只含本轮查到的非终态任务新状态。
        """
        tasks = (state or {}).get("async_tasks") or {}
        changed: dict[str, dict[str, Any]] = {}
        results: list[dict[str, Any]] = []
        notices: list[dict[str, Any]] = []
        for task_id, task in tasks.items():
            status = task.get("status") or ""
            if status not in _TERMINAL:
                new = fresh.get(task_id)
                if new and new != status:
                    status = new
                    changed[task_id] = {**task, "status": status, "last_checked_at": _now()}
            # 工具路径可能已把 state 写成终态却没投递/没通知（_refreshed_* 只改状态），这里补
            current = {**task, "status": status}
            if status in _RESULTS and not _delivered(current):
                results.append(current)
            elif status in _NOTICES and not _notified(current):
                notices.append(current)
        return changed, results, notices

    async def _deliver_results(self, tasks: list[dict[str, Any]], changed: dict[str, dict[str, Any]]) -> list[str]:
        """对终态结果抢发件箱行并生成投递正文。claim 三态见 `_deliver` 的 docstring。"""
        parent = self._parent_thread_id()
        # 慢 await（查子线程摘要）全部前置：claim 成功之后到返回之间不能再有 await，
        # 否则取消（Ctrl+C 两次 / drain 超时）会留下"行已 delivered、消息没写"的永久漏。
        summaries = [await self._summary_async(task) for task in tasks]
        texts: list[str] = []
        claimed: list[dict[str, Any]] = []
        try:
            for task, summary in zip(tasks, summaries, strict=True):
                got = await self._claim_async(task, parent)
                task_id = task["task_id"]
                if got is not True:
                    if got is False:  # 已投过：记下来，下次 O(1) 跳过
                        logger.info("task_delivery_skipped_claim", task_id=task_id)
                        changed[task_id] = {
                            **changed.get(task_id, task),
                            "status": task["status"],
                            "delivered_run_id": task.get("run_id"),
                        }
                    else:  # 不知道投没投：不投，行留 pending 等下一轮
                        logger.warning("task_delivery_claim_undetermined", task_id=task_id)
                    continue
                claimed.append(task)
                texts.append(_completion_text(task, summary))
                changed[task_id] = {
                    **changed.get(task_id, task),
                    "status": task["status"],
                    "delivered_run_id": task.get("run_id"),
                }
        except BaseException:
            # CancelledError 不是 Exception：优雅关停 drain 超时后 cancel、Ctrl+C 两下都走
            # 这条路。用 except Exception 会跳过 release，行就永远停在 delivered（结果再也
            # 投不出去）。硬取消下 release 尽力而为，失败只记日志。
            for task in claimed:
                with contextlib.suppress(BaseException):
                    await self._release_async(task, parent)
            raise
        if texts:
            logger.info("task_results_delivered", tasks=[t["task_id"] for t in claimed], count=len(texts))
        return texts

    @staticmethod
    def _notices(tasks: list[dict[str, Any]], changed: dict[str, dict[str, Any]]) -> list[str]:
        """状态通知：不占发件箱行、不查子线程，直接生成一条独立文案。"""
        out: list[str] = []
        for task in tasks:
            out.append(notice_text(task["task_id"], task.get("run_id"), task.get("agent_name"), task.get("status")))
            changed[task["task_id"]] = {
                **changed.get(task["task_id"], task),
                "status": task["status"],
                "notified_run_id": task.get("run_id"),
            }
        logger.info("task_status_notified", tasks=[t["task_id"] for t in tasks], count=len(out))
        return out

    async def _deliver(self, state: Any) -> dict[str, Any] | None:
        """把"已经终态、主管还不知道"的子任务写进本次 run 的 state。

        只有 `before_agent` 一个调用点（run 首一次）：补上 run 开始前就已终态、还没入过
        主管上下文的结果/状态。run 期间完成的等分发者开唤醒 run——那时模型这轮的回答
        已经定了，写进去它没机会反应。

        结果投递的 claim 三态，误差只允许朝"晚一点"，不允许朝"漏"：

        - `True`（抢到）：投。
        - `False`（分发者/别的 run 已投）：跳过，并把 `delivered_run_id` 写回 state。
          没这一步，每个 run 都要为同一条已投结果再打一次 claim。
        - `None`（服务不可达/超时）：**不投**。行还在 pending，下一轮分发者或下一个 run
          会再试。投了反而会让分发者再投一遍（行没翻，对方不知道）。

        状态通知（cancelled / interrupted）没有发件箱行，也不调 claim：它告知的是"这轮
        没跑完"，不是结论，所以不存在"谁先投"的竞争。
        """
        tasks = (state or {}).get("async_tasks") or {}
        fresh = {tid: t["status"] for tid, t in (await self._refreshed_async(tasks)).items()}
        changed, results, notices = self._plan(state, fresh)
        texts = await self._deliver_results(results, changed) if results else []
        if notices:
            texts.extend(self._notices(notices, changed))
        out: dict[str, Any] = {}
        if changed:
            out["async_tasks"] = changed
        if texts:
            out["messages"] = [HumanMessage(content=t) for t in texts]
        return out or None

    async def abefore_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """run 开始：补上此前已终态、还没进过主管上下文的结果。

        刻意不实现 `after_agent`：run 结束时写进去的结果模型已没机会转述，代价是多一条
        谁也解释不了的卡片；不写则分发者在 run 结束后开一轮唤醒 run，模型能对结果说话。
        """
        return await self._deliver(state)

    @staticmethod
    def _parent_thread_id() -> str:
        """当前（主管）run 的 thread_id；hook 在图外被触发时返回空串。"""
        try:
            conf = (get_config() or {}).get("configurable") or {}
        except RuntimeError:
            return ""
        return str(conf.get("thread_id") or "")

    @staticmethod
    def _mark_checked(request: ToolCallRequest, result: Any) -> dict[str, Any] | None:
        """模型自己 check 到终态结果时补记 `delivered_run_id`；是终态则返回被 check 的任务。

        判重只认这个字段（不扫 messages：摘要会删掉卡片、模型又能伪造前缀），所以模型
        自己看到终态结果也得在这里留记号，否则下一个 run 会认为它没投过。
        返回值供调用方 ack 发件箱行——只记号不 ack 的话，分发者仍当它是待投，
        父 run 一结束就开唤醒 run 把同一个结果再送一遍（用户看到两张一样的卡片）。
        """
        task_id = str((request.tool_call.get("args") or {}).get("task_id") or "").strip()
        task = ((request.state or {}).get("async_tasks") or {}).get(task_id)
        update = getattr(result, "update", None)
        if task is None or not isinstance(update, dict):
            return None
        checked = (update.get("async_tasks") or {}).get(task_id) or {}
        if checked.get("status") not in _RESULTS:  # cancelled/interrupted 没有结果可记
            return None
        update["async_tasks"] = {
            **(update.get("async_tasks") or {}),
            task_id: {**checked, "delivered_run_id": task.get("run_id")},
        }
        return {**task, "status": checked["status"]}

    async def _amark_checked(self, request: ToolCallRequest, result: Any) -> Any:
        """`_mark_checked` + ack：抢不到行说明分发者已在投（或已投），不再重复。

        shortcut: ack 早于 ToolMessage 落 checkpoint，这轮 run 崩在中间时结果只能从子线程
        找回（模型已拿到全文，等量信息没丢）；返回 None 表示服务不可达，行留在 pending。
        """
        task = self._mark_checked(request, result)
        if task is not None and await self._claim_async(task, self._parent_thread_id()) is None:
            logger.warning("task_delivery_claim_undetermined", task_id=task.get("task_id"))
        return result

    # =========================== D. 接线 ===========================

    def wrap_tool_call(self, request: ToolCallRequest, handler: Callable[..., Any]) -> ToolMessage | Command[Any]:
        """同步接线：start/update 自己建 run，check 之后补记一次投递账。"""
        name = request.tool_call.get("name")
        if name == "start_async_task":
            return self._start_sync(request)
        if name == "update_async_task":
            return self._update_sync(request)
        result = handler(request)
        # 本图只实现 async hook，同步 invoke 在建图时就报错，这条路径实际走不到
        if name == "check_async_task":
            self._mark_checked(request, result)
        return result

    async def awrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[..., Awaitable[Any]]
    ) -> ToolMessage | Command[Any]:
        """异步接线（线上走这条）：同上。"""
        name = request.tool_call.get("name")
        if name == "start_async_task":
            return await self._astart(request)
        if name == "update_async_task":
            return await self._aupdate(request)
        result = await handler(request)
        return await self._amark_checked(request, result) if name == "check_async_task" else result
