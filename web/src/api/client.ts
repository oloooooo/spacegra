/** HTTP 封装：token 注入、错误归一、401 自动登出。 */

export const API_BASE: string = (import.meta.env.VITE_API_BASE as string | undefined) ?? "http://127.0.0.1:2026";

const TOKEN_KEY = "aegra_token";
let token = localStorage.getItem(TOKEN_KEY) ?? "";

export const getToken = () => token;
export const setToken = (t: string) => {
  token = t;
  localStorage.setItem(TOKEN_KEY, t);
};
export const clearToken = () => {
  token = "";
  localStorage.removeItem(TOKEN_KEY);
};

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    /** 限流锁定：还要等多少秒（后端 Retry-After），0 表示不是限流 */
    public retryAfter = 0,
  ) {
    super(message);
  }
}

function retryAfterSeconds(res: Response): number {
  const raw = res.headers.get("retry-after");
  const seconds = raw ? Number(raw) : 0;
  return Number.isFinite(seconds) && seconds > 0 ? Math.ceil(seconds) : 0;
}

function authHeaders(extra: Record<string, string> = {}): Record<string, string> {
  return token ? { Authorization: `Bearer ${token}`, ...extra } : extra;
}

/** 后端两种错误体：{"detail": "..."}（HTTPException）与 {"error","message"}（aegra）。 */
function errorText(status: number, body: unknown, fallback: string): string {
  if (body && typeof body === "object") {
    const b = body as Record<string, unknown>;
    for (const key of ["message", "detail", "error"]) {
      if (typeof b[key] === "string") return b[key] as string;
    }
  }
  return fallback || `HTTP ${status}`;
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: authHeaders({ "Content-Type": "application/json", ...(init.headers as Record<string, string>) }),
  });
  return unwrap<T>(res, path);
}

/** multipart 上传：Content-Type 交给浏览器（要带 boundary），其余同 api()。 */
export async function apiForm<T>(path: string, form: FormData): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, { method: "POST", body: form, headers: authHeaders() });
  return unwrap<T>(res, path);
}

async function unwrap<T>(res: Response, path: string): Promise<T> {
  if (res.status === 401) {
    clearToken();
    // 登录接口的 401 是“账号或密码错误”，别改写成“登录已过期”
    throw new ApiError(401, path.endsWith("/login") ? "账号或密码错误" : "登录已过期，请重新登录");
  }
  const text = await res.text();
  const body = text ? JSON.parse(text) : null;
  if (!res.ok) throw new ApiError(res.status, errorText(res.status, body, res.statusText), retryAfterSeconds(res));
  return body as T;
}

/** SSE 事件：aegra/代理都是 `event: <name>` + `data: <json>` 帧。 */
export type SseEvent = { event: string; data: unknown };

/**
 * 消费一个 SSE 流（POST 用 runs/stream，GET 用任务代理）。
 * 用 fetch 而不是 EventSource：EventSource 只支持 GET 且不能带 Authorization 头。
 */
export async function* sse(path: string, init: RequestInit = {}, signal?: AbortSignal): AsyncGenerator<SseEvent> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: authHeaders({ "Content-Type": "application/json", Accept: "text/event-stream", ...(init.headers as Record<string, string>) }),
    signal,
  });
  if (res.status === 401) {
    clearToken();
    throw new ApiError(401, "登录已过期，请重新登录");
  }
  if (!res.ok || !res.body) {
    const text = await res.text().catch(() => "");
    throw new ApiError(res.status, text.slice(0, 300) || res.statusText);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let cut: number;
    while ((cut = buf.indexOf("\n\n")) >= 0) {
      const raw = buf.slice(0, cut);
      buf = buf.slice(cut + 2);
      let event = "message";
      const dataLines: string[] = [];
      for (const line of raw.split("\n")) {
        if (!line || line.startsWith(":")) continue; // 心跳注释帧
        const colon = line.indexOf(":");
        const field = colon < 0 ? line : line.slice(0, colon);
        const val = colon < 0 ? "" : line.slice(colon + 1).replace(/^ /, "");
        if (field === "event") event = val;
        else if (field === "data") dataLines.push(val);
      }
      if (!dataLines.length) continue;
      const payload = dataLines.join("\n");
      try {
        yield { event, data: JSON.parse(payload) };
      } catch {
        yield { event, data: payload };
      }
    }
  }
}
