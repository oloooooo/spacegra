/** 与后端接口一一对应（见 aegra-api/docs/frontend-api.md）。 */
import { api, apiForm, sse } from "./client";

export type GraphList = { graphs: string[]; identity: string; permissions: string[] };

export type Space = { name: string; path: string; accessible: boolean; file_count: number };
export type SpaceList = { spaces: Space[]; is_super: boolean };

export type SpaceFile = { path: string; size: number; modified: string; ext: string };

export type Task = {
  task_id: string;
  thread_id: string;
  run_id: string;
  agent_name: string;
  status: string;
  created_at?: string;
  last_checked_at?: string;
  message_count?: number | null;
};

export type ThreadMeta = {
  thread_id: string;
  status: string;
  metadata?: { thread_name?: string; graph_id?: string | null } & Record<string, unknown>;
  created_at?: string;
  updated_at?: string;
};

export type ChatMessage = {
  id?: string;
  type?: string;
  role?: string;
  name?: string | null;
  content: unknown;
  tool_calls?: unknown[];
};

export type Interrupt = { id?: string; value: { header?: string; question?: string; options?: { label: string; description?: string }[] } };

// ---------- 自定义层 ----------
export const login = (identity: string, password: string) =>
  api<{ access_token: string; expires_in: number }>("/custom/login", {
    method: "POST",
    body: JSON.stringify({ identity, password }),
  });

export const whoami = () => api<{ identity: string; display_name: string; permissions: string[] }>("/custom/whoami");
export const mySpaces = () => api<{ space: string[]; message?: string }>("/custom/user/spaces");
export const graphs = () => api<GraphList>("/custom/graphs");
export const spaces = () => api<SpaceList>("/custom/spaces");
export const spaceFiles = (name: string) => api<{ files: SpaceFile[]; root: string }>(`/custom/spaces/${encodeURIComponent(name)}/files`);
export const spaceUsers = (name: string) => api<{ users: string[] }>(`/custom/spaces/${encodeURIComponent(name)}/users`);
export const removeSpaceUser = (name: string, identity: string) =>
  api<{ removed: string }>(`/custom/spaces/${encodeURIComponent(name)}/users/${encodeURIComponent(identity)}`, { method: "DELETE" });

export const createSpace = (space_name: string, path: string) =>
  api<{ space_name: string; message?: string }>("/custom/spaces/create", {
    method: "POST",
    body: JSON.stringify({ space_name, path }),
  });

export const assignSpace = (identity: string, space_name: string) =>
  api<{ message?: string }>(
    `/custom/spaces/users/create?identity=${encodeURIComponent(identity)}&space_name=${encodeURIComponent(space_name)}`,
    { method: "POST" },
  );

export const task = (taskId: string, parentThreadId: string) =>
  api<Task>(`/custom/tasks/${taskId}?parent_thread_id=${encodeURIComponent(parentThreadId)}`);

// ---------- 原生 aegra 接口 ----------
export const searchThreads = (limit = 50) =>
  api<ThreadMeta[]>("/threads/search", { method: "POST", body: JSON.stringify({ limit }) });

export const createThread = () => api<ThreadMeta>("/threads", { method: "POST", body: JSON.stringify({}) });

export const deleteThread = (id: string) => api<unknown>(`/threads/${id}`, { method: "DELETE" });

export type ThreadState = {
  values: { messages?: ChatMessage[]; async_tasks?: Record<string, Task> };
  interrupts?: Interrupt[];
  next?: string[];
};

export const threadState = (id: string) => api<ThreadState>(`/threads/${id}/state`);

export const runStream = (threadId: string, body: unknown, signal?: AbortSignal) =>
  sse(`/threads/${threadId}/runs/stream`, { method: "POST", body: JSON.stringify(body) }, signal);

export const taskStream = (taskId: string, parentThreadId: string, signal?: AbortSignal) =>
  sse(`/custom/tasks/${taskId}/stream?parent_thread_id=${encodeURIComponent(parentThreadId)}`, { method: "GET" }, signal);

/** 取消一个子任务（子线程属主是服务账号，后端代理一次 cancel）。 */
export const cancelTask = (taskId: string, parentThreadId: string, runId: string) =>
  api<{ cancelled: boolean }>(`/custom/tasks/${taskId}/cancel`, {
    method: "POST",
    body: JSON.stringify({ parent_thread_id: parentThreadId, run_id: runId }),
  });

export const cancelRun = (threadId: string, runId: string) =>
  api<unknown>(`/threads/${threadId}/runs/${runId}/cancel`, { method: "POST", body: JSON.stringify({ action: "interrupt" }) });

// ---------- 定时任务 ----------
export type Cron = {
  cron_id: string;
  thread_id?: string | null;
  schedule: string;
  enabled: boolean;
  next_run_date?: string | null;
  payload?: { input?: { messages?: { content?: string }[] } } & Record<string, unknown>;
};

export const searchCrons = (limit = 50) => api<Cron[]>("/runs/crons/search", { method: "POST", body: JSON.stringify({ limit }) });
export const createCron = (threadId: string, body: unknown) => api<unknown>(`/threads/${threadId}/runs/crons`, { method: "POST", body: JSON.stringify(body) });
export const patchCron = (cronId: string, body: unknown) => api<Cron>(`/runs/crons/${cronId}`, { method: "PATCH", body: JSON.stringify(body) });
export const deleteCron = (cronId: string) => api<unknown>(`/runs/crons/${cronId}`, { method: "DELETE" });

// ---------- 会话文件上传 ----------
export type Uploaded = {
  name: string;
  status: "ok";
  size: number;
  sha256: string;
  target_agent: string;
  virtual_path: string;
  sheets: { name: string; rows: number; columns: number }[];
};
export type UploadFailed = { name: string; status: "failed"; code: number; error: string };
export type UploadResult = { files: (Uploaded | UploadFailed)[] };

/** 一次可传多个文件；单个失败不影响同批（HTTP 207 + 逐文件结果）。 */
export const uploadFiles = (threadId: string, files: File[]) => {
  const form = new FormData();
  form.append("thread_id", threadId);
  for (const f of files) form.append("files", f, f.name);
  return apiForm<UploadResult>("/custom/uploads", form);
};
