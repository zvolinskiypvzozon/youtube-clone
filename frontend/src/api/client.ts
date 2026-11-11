// HTTP-клиент API: cookie-сессия (HttpOnly, браузер шлёт сам), CSRF-заголовок на изменяющих
// запросах, единый разбор ошибок {detail, errors} в ApiError.

export const CSRF_COOKIE = "csrf";
export const CSRF_HEADER = "X-CSRF-Token";

const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

export type FieldErrors = Record<string, string>;

export class ApiError extends Error {
  readonly status: number;
  /** Поле → сообщение сервера (для 422: последний элемент loc). */
  readonly fields: FieldErrors;
  /** Секунды из Retry-After (429, 503) или null. */
  readonly retryAfter: number | null;

  constructor(status: number, message: string, fields: FieldErrors = {}, retryAfter: number | null = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.fields = fields;
    this.retryAfter = retryAfter;
  }
}

/** Ошибка сети (нет ответа): сервер недоступен или соединение оборвалось. */
export class NetworkError extends Error {
  constructor() {
    super("Нет связи с сервером. Проверьте подключение и повторите.");
    this.name = "NetworkError";
  }
}

const STATUS_MESSAGES: Record<number, string> = {
  429: "Слишком много запросов. Подождите немного и повторите.",
  502: "Сервер временно недоступен. Повторите позже.",
  503: "Сервер занят. Повторите через несколько секунд.",
  504: "Сервер не ответил вовремя. Повторите позже.",
};

export function readCookie(name: string): string | null {
  for (const part of document.cookie.split(";")) {
    const [key, ...rest] = part.trim().split("=");
    if (key !== name) continue;
    try {
      return decodeURIComponent(rest.join("="));
    } catch {
      return null; // битое значение — как отсутствие (сервер ответит 403)
    }
  }
  return null;
}

export function parseRetryAfter(value: string | null): number | null {
  if (!value) return null;
  const seconds = Number(value);
  if (Number.isFinite(seconds) && seconds >= 0) return seconds;
  const date = Date.parse(value);
  if (Number.isNaN(date)) return null;
  return Math.max(0, Math.round((date - Date.now()) / 1000));
}

async function toApiError(response: Response): Promise<ApiError> {
  const retryAfter = parseRetryAfter(response.headers.get("Retry-After"));
  let detail: string | null = null;
  let fields: FieldErrors = {};
  try {
    const body = (await response.json()) as { detail?: unknown; errors?: unknown };
    if (typeof body.detail === "string") detail = body.detail;
    if (Array.isArray(body.errors)) {
      const entries: [string, string][] = [];
      for (const e of body.errors as { loc?: unknown[]; msg?: unknown }[]) {
        const field = Array.isArray(e.loc) ? e.loc[e.loc.length - 1] : undefined;
        if (typeof field === "string" && !entries.some(([name]) => name === field)) {
          entries.push([field, typeof e.msg === "string" ? e.msg : "Неверное значение"]);
        }
      }
      fields = Object.fromEntries(entries);
    }
  } catch {
    // Тело не JSON (например, страница ошибки nginx) — берём сообщение по коду.
  }
  const message = STATUS_MESSAGES[response.status] ?? detail ?? `Ошибка ${response.status}`;
  return new ApiError(response.status, message, fields, retryAfter);
}

export interface RequestOptions {
  method?: string;
  body?: unknown;
  query?: Record<string, string | number | null | undefined>;
  signal?: AbortSignal;
}

function buildUrl(path: string, query?: RequestOptions["query"]): string {
  if (!query) return path;
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value !== null && value !== undefined && value !== "") params.set(key, String(value));
  }
  const qs = params.toString();
  return qs ? `${path}?${qs}` : path;
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const method = (options.method ?? "GET").toUpperCase();
  const csrf = SAFE_METHODS.has(method) ? null : readCookie(CSRF_COOKIE);
  const headers: Record<string, string> = {
    Accept: "application/json",
    ...(options.body !== undefined ? { "Content-Type": "application/json" } : {}),
    ...(csrf ? { [CSRF_HEADER]: csrf } : {}),
  };
  let response: Response;
  try {
    response = await fetch(buildUrl(path, options.query), {
      method,
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      credentials: "same-origin",
      signal: options.signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new NetworkError();
  }
  if (!response.ok) throw await toApiError(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

/** Текст ошибки для показа пользователю. */
export function errorMessage(error: unknown): string {
  if (error instanceof ApiError || error instanceof NetworkError) return error.message;
  return "Что-то пошло не так. Обновите страницу.";
}
