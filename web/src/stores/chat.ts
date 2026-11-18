import { computed, reactive, ref } from "vue";
import { defineStore } from "pinia";
import { ApiError, getToken, setToken } from "../api/client";
import * as api from "../api/endpoints";
import { clearToken } from "../api/client";

const TERMINAL = new Set(["cancelled", "success", "error", "timeout", "interrupted"]);

export type ViewMessage = {
  key: string;
  role: "human" | "ai" | "tool" | "callback" | "notice";
  content: string;
  name?: string;
  toolCalls?: boolean;
};

export type TaskView = api.Task & { text: string; events: string[]; subscribed: boolean };

function textOf(content: unknown): string {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) return content.map(textOf).join("");
  if (content && typeof content === "object") {
    const c = content as Record<string, unknown>;
    if (typeof c.text === "string") return c.text;
  }
  return "";
}

/** 把 thread state 的消息转成可渲染条目；子 agent 回写单独标 role=callback。 */
function toView(m: api.ChatMessage, i: number): ViewMessage {
  const raw = textOf(m.content);
  const type = m.type ?? m.role ?? "ai";
  if (type === "human" && raw.startsWith("[Async subagent")) {
    const name = /\[Async subagent '([^']+)'/.exec(raw)?.[1] ?? "subagent";
    // 状态通知（was cancelled / was interrupted）不带 Result:，渲染成灰色状态条而不是结果卡片。
    // 通知没有正文，所以内容要用一句人话，而不是剩下的 task_id/run_id 两行。
    const isResult = raw.includes("Result:");
    const content = isResult
      ? raw.replace(/^\[Async subagent[^\]]*\]\s*(Result:\s*)?/, "")
      : raw.includes("was cancelled")
        ? "这个任务被取消了，没有结果"
        : raw.includes("was interrupted")
          ? "这个任务中途停了，没有结果"
          : "这个任务已停止，没有结果";
    return { key: m.id ?? `cb-${i}`, role: isResult ? "callback" : "notice", content, name };
  }
  if (type === "tool") return { key: m.id ?? `t-${i}`, role: "tool", content: raw.slice(0, 300), name: m.name ?? undefined };
  return { key: m.id ?? `${type}-${i}`, role: type === "human" ? "human" : "ai", content: raw, toolCalls: !!m.tool_calls?.length };
}

export const useChat = defineStore("chat", () => {
  const identity = ref("");
  const permissions = ref<string[]>([]);
  const threads = ref<api.ThreadMeta[]>([]);
  const threadId = ref<string | null>(null);
  const messages = ref<ViewMessage[]>([]);
  const streaming = ref("");
  const interrupts = ref<api.Interrupt[]>([]);
  const tasks = reactive<Record<string, TaskView>>({});
  const running = ref(false);
  const error = ref("");
  const currentRun = ref<string | null>(null);

  const isSuper = computed(() => permissions.value.includes("write"));
  const activeTasks = computed(() => Object.values(tasks).filter((t) => !TERMINAL.has(t.status)));

  let runAbort: AbortController | null = null;
  const taskAborts: Record<string, AbortController> = {};
  let pollTimer: number | undefined;

  // ---------- 认证 ----------

  async function login(id: string, password: string) {
    const res = await api.login(id, password);
    setToken(res.access_token);
    await bootstrap();
  }

  /** 首次访问：建超级用户并直接登录。 */
  async function bootstrapSuperUser(id: string, displayName: string, password: string) {
    const res = await api.bootstrapSuperUser({ identity: id, display_name: displayName, password });
    setToken(res.access_token);
    await bootstrap();
  }

  async function bootstrap() {
    if (!getToken()) return;
    const me = await api.whoami();
    identity.value = me.identity;
    permissions.value = me.permissions;
    await loadThreads();
  }

  function logout() {
    stopRun();
    window.clearTimeout(pollTimer);
    pollTimer = undefined;
    clearToken();
    identity.value = "";
    permissions.value = [];
    threads.value = [];
    threadId.value = null;
    messages.value = [];
    tasks_clear();
  }

  function tasks_clear() {
    Object.keys(tasks).forEach((k) => {
      taskAborts[k]?.abort();
      delete taskAborts[k];
      delete tasks[k];
    });
  }

  // ---------- 会话 ----------

  async function loadThreads() {
    threads.value = await api.searchThreads();
  }

  async function openThread(id: string) {
    stopRun();
    window.clearTimeout(pollTimer);
    pollTimer = undefined;
    threadId.value = id;
    streaming.value = "";
    error.value = "";
    await refreshState();
  }

  /** 拉一次权威状态：消息、待回答的中断、后台任务。 */
  async function refreshState() {
    if (!threadId.value) return;
    const st = await api.threadState(threadId.value);
    messages.value = (st.values.messages ?? []).map(toView);
    interrupts.value = st.interrupts ?? [];
    syncTasks(st.values.async_tasks ?? {});
  }

  function syncTasks(raw: Record<string, api.Task>) {
    for (const [id, t] of Object.entries(raw)) {
      const old = tasks[id];
      tasks[id] = { ...t, text: old?.text ?? "", events: old?.events ?? [], subscribed: old?.subscribed ?? false };
      if (!TERMINAL.has(t.status) && !tasks[id].subscribed) void subscribeTask(id);
    }
    for (const id of Object.keys(tasks)) if (!raw[id]) delete tasks[id];
  }

  // ---------- 子 agent 实时流 ----------

  async function subscribeTask(id: string) {
    if (!threadId.value) return;
    const ctrl = new AbortController();
    taskAborts[id] = ctrl;
    tasks[id].subscribed = true;
    try {
      for await (const ev of api.taskStream(id, threadId.value, ctrl.signal)) {
        const t = tasks[id];
        if (!t) return;
        if (ev.event === "custom") {
          const d = ev.data as { tool?: string; event?: string };
          if (d?.tool) t.events.push(`${d.tool} ${d.event ?? ""}`.trim());
        } else if (ev.event === "messages/partial") {
          const m = lastMessage(ev.data);
          if (m?.content) t.text = m.content;
        } else if (ev.event === "end") {
          break;
        }
      }
    } catch (e) {
      if (!ctrl.signal.aborted) fail(e);
    } finally {
      tasks[id] && (tasks[id].subscribed = false);
      try {
        const fresh = await api.task(id, threadId.value);
        if (tasks[id]) tasks[id] = { ...tasks[id], ...fresh };
      } catch {
        /* 状态刷新失败不影响展示 */
      }
    }
  }

  function lastMessage(data: unknown): { id?: string; content?: string } | null {
    const arr = Array.isArray(data) ? data : [data];
    let last = arr[arr.length - 1];
    if (Array.isArray(last)) last = last[0]; // [chunk, metadata]
    if (!last || typeof last !== "object") return null;
    const m = last as Record<string, unknown>;
    const content = textOf(m.content);
    return { id: typeof m.id === "string" ? m.id : undefined, content };
  }

  // ---------- 发消息 / 追问 ----------

  async function ensureThread(): Promise<string> {
    if (threadId.value) return threadId.value;
    const t = await api.createThread();
    threadId.value = t.thread_id;
    await loadThreads();
    return t.thread_id;
  }

  async function send(text: string) {
    const id = await ensureThread();
    messages.value.push({ key: `local-${Date.now()}`, role: "human", content: text });
    await streamRun(id, { assistant_id: "supervisor", input: { messages: [{ role: "user", content: text }] } });
  }

  async function resume(label: string) {
    if (!threadId.value) return;
    interrupts.value = [];
    messages.value.push({ key: `pick-${Date.now()}`, role: "human", content: label });
    await streamRun(threadId.value, { assistant_id: "supervisor", command: { resume: label } });
  }

  async function streamRun(id: string, body: Record<string, unknown>) {
    stopRun();
    running.value = true;
    error.value = "";
    streaming.value = "";
    runAbort = new AbortController();
    try {
      // on_disconnect=continue：刷新/切页断开 SSE 不杀 run，取消只走显式 cancelRun
      for await (const ev of api.runStream(
        id,
        { ...body, stream_mode: ["messages", "updates", "values", "custom"], on_disconnect: "continue" },
        runAbort.signal,
      )) {
        handleEvent(ev);
      }
    } catch (e) {
      if (!(e instanceof DOMException && e.name === "AbortError")) fail(e);
    } finally {
      running.value = false;
      // 先拉权威状态再清流式草稿，否则会先清空、后重填（闪一下）
      await refreshState().catch(fail);
      streaming.value = "";
      await loadThreads().catch(fail);
      if (activeTasks.value.length) schedulePoll();
    }
  }

  function handleEvent(ev: { event: string; data: unknown }) {
    if (ev.event === "metadata") {
      currentRun.value = (ev.data as { run_id?: string })?.run_id ?? null;
    } else if (ev.event === "messages/partial") {
      const m = lastMessage(ev.data);
      if (m?.content) streaming.value = m.content;
    } else if (ev.event === "values") {
      const v = ev.data as { async_tasks?: Record<string, api.Task> };
      if (v?.async_tasks) syncTasks(v.async_tasks);
    } else if (ev.event === "updates") {
      const upd = ev.data as Record<string, unknown>;
      const intr = upd?.__interrupt__;
      if (Array.isArray(intr) && intr.length) interrupts.value = intr as api.Interrupt[];
    } else if (ev.event === "error") {
      const d = ev.data as { message?: string };
      error.value = d?.message ?? String(ev.data);
    }
  }

  function stopRun() {
    runAbort?.abort();
    runAbort = null;
    running.value = false;
  }

  /** 取消一个子任务；成功后不再订阅它的流，并立刻拉一次真实状态。
   *
   * 为什么必须重拉：父 state 里的 `async_tasks[tid].status` 要等主管下一轮 run 才刷新，
   * 中间任何一次 refreshState 都会用那个陈旧的 `running` 把卡片刷回去。
   */
  async function cancelTask(id: string) {
    const t = tasks[id];
    if (!t || !threadId.value) return;
    taskAborts[id]?.abort();
    await api.cancelTask(id, threadId.value, t.run_id);
    t.status = "cancelled";
    try {
      if (tasks[id]) tasks[id] = { ...tasks[id], ...(await api.task(id, threadId.value)) };
    } catch {
      /* 状态刷新失败不影响已显示的取消 */
    }
  }

  async function cancel() {
    if (!threadId.value || !currentRun.value) return;
    try {
      await api.cancelRun(threadId.value, currentRun.value);
    } catch (e) {
      fail(e);
    }
    stopRun();
    await refreshState().catch(fail);
  }

  async function removeThread(id: string) {
    await api.deleteThread(id);
    if (threadId.value === id) {
      threadId.value = null;
      messages.value = [];
      tasks_clear();
    }
    await loadThreads();
  }

  // ---------- 轮询：等子 agent 回写（父线程会被唤醒再跑一轮） ----------

  function schedulePoll() {
    window.clearTimeout(pollTimer);
    pollTimer = window.setTimeout(async () => {
      await refreshState().catch(fail);
      if (threadId.value && activeTasks.value.length) schedulePoll();
    }, 4000);
  }

  function isAuthError(e: unknown) {
    return e instanceof ApiError && e.status === 401;
  }

  /** 统一错误出口：401 直接登出回登录页，其它错误写进 chat.error。 */
  function fail(e: unknown) {
    if (isAuthError(e)) {
      logout();
      error.value = "登录已过期，请重新登录";
      return;
    }
    error.value = (e as Error).message ?? String(e);
  }

  return {
    identity, permissions, isSuper, threads, threadId, messages, streaming, interrupts, tasks,
    activeTasks, running, error, currentRun,
    login, bootstrap, logout, loadThreads, openThread, refreshState, send, resume, cancel, cancelTask,
    bootstrapSuperUser,
    removeThread, isAuthError,
  };
});
