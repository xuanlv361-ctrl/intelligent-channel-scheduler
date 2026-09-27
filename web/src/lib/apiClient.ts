export interface ApiErrorShape { code: string; message: string; status: number; details?: unknown; diagnosticId?: string; retryable?: boolean }

// Use the Vite / deployment reverse proxy by default so the browser makes
// same-origin requests. An explicit URL remains available for E2E environments.
const API_BASE = (import.meta.env.VITE_API_BASE_URL || "").replace(/\/$/, "");
const CSRF_HEADER = "X-CSRF-Token";
const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

let csrfToken: string | null = null;
let sessionInitialization: Promise<void> | null = null;
let writeQueue: Promise<void> = Promise.resolve();

export class ApiError extends Error {
  constructor(public detail: ApiErrorShape) { super(detail.message); }
}

function diagnosticId(response?: Response): string {
  const correlation = response?.headers.get("X-Correlation-ID");
  if (correlation) return correlation.slice(0, 80);
  const suffix = typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID().slice(0, 12) : Date.now().toString(36);
  return `local-${suffix}`;
}

const infrastructureMessages: Record<string, string> = {
  backend_offline: "本地后端尚未启动。",
  backend_starting: "本地后端正在启动，请稍候。",
  backend_unhealthy: "本地后端已启动，但健康检查未通过。",
  authentication_required: "会话已过期，请重新认证。",
  permission_denied: "当前身份没有执行此操作的权限。",
  csrf_failed: "安全校验已失效，请刷新会话后重试。",
  origin_rejected: "当前页面来源未被后端允许。",
  api_route_not_found: "API 路径版本不一致。",
  proxy_error: "本地入口无法连接后端服务。",
  request_timeout: "本地请求超时，请检查服务状态。",
  database_not_ready: "数据库迁移尚未完成。",
  unknown_backend_error: "本地服务返回了未识别的错误。",
};

const SENSITIVE_DETAIL_KEY = /(authorization|cookie|api[-_]?key|token|secret|credential(?!_fingerprint)|password)/i;
function sanitizeErrorDetail(value: unknown, depth = 0): unknown {
  if (depth > 5) return "[内容已截断]";
  if (Array.isArray(value)) return value.slice(0, 50).map((item) => sanitizeErrorDetail(item, depth + 1));
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value as Record<string, unknown>)
      .filter(([key]) => !SENSITIVE_DETAIL_KEY.test(key))
      .map(([key, item]) => [key, sanitizeErrorDetail(item, depth + 1)]));
  }
  if (typeof value === "string") {
    const redacted = value
      .replace(/Bearer\s+[A-Za-z0-9._~+/=-]+/gi, "Bearer [已脱敏]")
      .replace(/\bsk-[A-Za-z0-9_-]{8,}\b/g, "sk-[已脱敏]");
    return redacted.length > 2_000 ? `${redacted.slice(0, 2_000)}…` : redacted;
  }
  return value;
}

function responseCategory(status: number, code: string): string | null {
  const normalized = code.toLowerCase();
  if (status === 401) return "authentication_required";
  if (normalized.includes("csrf")) return "csrf_failed";
  if (normalized.includes("origin")) return "origin_rejected";
  if (status === 403) return "permission_denied";
  if (status === 404) return "api_route_not_found";
  // Preserve a structured application/upstream error.  Only an actual gateway
  // or proxy failure should be presented as a broken local backend connection.
  if ((status === 502 || status === 504) &&
      ["http_error","proxy_error","bad_gateway","gateway_timeout"].includes(normalized)) return "proxy_error";
  if (normalized.includes("migration") || normalized.includes("database")) return "database_not_ready";
  if (status === 503 && normalized.includes("starting")) return "backend_starting";
  if (status === 503) return "backend_unhealthy";
  return null;
}

async function errorFromResponse(response: Response): Promise<ApiError> {
  const body = await response.json().catch(() => ({}));
  const detail = body.detail || body;
  if (response.status === 401 || String(detail.code || "").startsWith("csrf_")) {
    csrfToken = null;
    sessionInitialization = null;
  }
  const upstreamCode = String(detail.code || "HTTP_ERROR");
  const category = responseCategory(response.status, upstreamCode);
  return new ApiError({
    code: category || upstreamCode,
    message: category ? infrastructureMessages[category] : (detail.message || `HTTP ${response.status}`),
    status: response.status,
    diagnosticId: diagnosticId(response),
    retryable: [502, 503, 504].includes(response.status),
    details: sanitizeErrorDetail({ ...detail, code: upstreamCode }),
  });
}

async function initializeSession(): Promise<void> {
  const existing = await fetch(`${API_BASE}/api/v1/security/session/csrf`, {
    method: "GET", credentials: "include", headers: { "Accept": "application/json" },
  });
  if (existing.ok) {
    const body = await existing.json() as { csrf_token?: string };
    if (!body.csrf_token) throw new ApiError({ code: "CSRF_TOKEN_MISSING", message: "Session CSRF token was not returned.", status: 502 });
    csrfToken = body.csrf_token;
    return;
  }
  if (existing.status !== 401) throw await errorFromResponse(existing);
  const created = await fetch(`${API_BASE}/api/v1/security/session/bootstrap`, {
    method: "POST", credentials: "include",
    headers: { "Accept": "application/json", "Content-Type": "application/json" },
    body: "{}",
  });
  if (!created.ok) throw await errorFromResponse(created);
  const body = await created.json() as { csrf_token?: string };
  if (!body.csrf_token) throw new ApiError({ code: "CSRF_TOKEN_MISSING", message: "Session bootstrap did not return a CSRF token.", status: 502 });
  csrfToken = body.csrf_token;
}

export function initializeEnterpriseSession(): Promise<void> {
  if (!sessionInitialization) {
    sessionInitialization = initializeSession().catch((error) => {
      sessionInitialization = null;
      throw error;
    });
  }
  return sessionInitialization;
}

async function authenticatedFetch(path: string, options: RequestInit, signal: AbortSignal): Promise<Response> {
  await initializeEnterpriseSession();
  const method = (options.method || "GET").toUpperCase();
  const headers = new Headers(options.headers);
  headers.set("Accept", "application/json");
  if (!SAFE_METHODS.has(method)) {
    if (!csrfToken) throw new ApiError({ code: "CSRF_TOKEN_MISSING", message: "No CSRF token is available for this request.", status: 0 });
    headers.set(CSRF_HEADER, csrfToken);
  }
  const response = await fetch(`${API_BASE}${path}`, {
    ...options, method, headers, credentials: "include", signal,
  });
  const rotated = response.headers.get(CSRF_HEADER);
  if (rotated) csrfToken = rotated;
  if (response.status === 401) {
    csrfToken = null;
    sessionInitialization = null;
  }
  return response;
}

async function serializeStateChange<T>(operation: () => Promise<T>): Promise<T> {
  const previous = writeQueue;
  let release!: () => void;
  writeQueue = new Promise<void>((resolve) => { release = resolve; });
  await previous;
  try { return await operation(); } finally { release(); }
}

export async function apiRequest<T>(path: string, options: RequestInit = {}, timeoutMs = 10_000): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const signal = options.signal || controller.signal;
    const method = (options.method || "GET").toUpperCase();
    const execute = () => authenticatedFetch(path, options, signal);
    const response = SAFE_METHODS.has(method) ? await execute() : await serializeStateChange(execute);
    if (!response.ok) throw await errorFromResponse(response);
    return await response.json() as T;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if ((error as Error).name === "AbortError") throw new ApiError({
      code: "request_timeout", message: infrastructureMessages.request_timeout,
      status: 0, diagnosticId: diagnosticId(), retryable: true,
      details: { requestPath: path, errorType: "AbortError" },
    });
    throw new ApiError({
      code: "backend_offline", message: infrastructureMessages.backend_offline,
      status: 0, diagnosticId: diagnosticId(), retryable: true,
      details: { requestPath: path, errorType: (error as Error).name || "NetworkError" },
    });
  } finally {
    clearTimeout(timer);
  }
}

export async function apiText(path: string, timeoutMs = 10_000): Promise<string> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await authenticatedFetch(path, { method: "GET" }, controller.signal);
    if (!response.ok) throw new ApiError({ code: "REPORT_ERROR", message: "报告生成失败", status: response.status });
    return response.text();
  } finally {
    clearTimeout(timer);
  }
}

export async function apiNdjson<T>(path: string, body: unknown,
  onEvent: (event: T) => void, signal: AbortSignal): Promise<void> {
  const execute = () => authenticatedFetch(path, {
    method: "POST", headers: { "Content-Type": "application/json", "Accept": "application/x-ndjson" },
    body: JSON.stringify(body), signal,
  }, signal);
  const response = await serializeStateChange(execute);
  if (!response.ok) throw await errorFromResponse(response);
  if (!response.body) throw new ApiError({ code: "STREAM_BODY_MISSING", message: "流式响应没有可读取的正文。", status: 502 });
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    buffer += decoder.decode(value, { stream: !done });
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";
    for (const line of lines) {
      if (!line.trim()) continue;
      onEvent(JSON.parse(line) as T);
    }
    if (done) break;
  }
  if (buffer.trim()) onEvent(JSON.parse(buffer) as T);
}

/** Test-only state reset; no credentials or session identifiers are exposed. */
export function resetEnterpriseSecurityClientForTests(): void {
  csrfToken = null;
  sessionInitialization = null;
  writeQueue = Promise.resolve();
}
