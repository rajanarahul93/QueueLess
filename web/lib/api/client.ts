import { API_URL } from "@/lib/api/config";
import { getAccessToken, refreshSession } from "@/lib/auth/session";

/** An RFC 7807 problem from the API, or a network failure (status 0, code NETWORK). */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    readonly retryAfterSec: number | null,
    readonly body: Record<string, unknown>,
  ) {
    super(code);
  }
}

export type RequestOptions = {
  method?: "GET" | "POST" | "PATCH" | "DELETE";
  body?: unknown;
  /** One key per user action (newIdempotencyKey()); the retry after a refresh reuses it. */
  idempotencyKey?: string;
  /** Send the access token and refresh once on a 401. Off for the auth endpoints. */
  auth?: boolean;
  signal?: AbortSignal;
};

export function newIdempotencyKey(): string {
  return crypto.randomUUID();
}

function headersFor(options: RequestOptions, token: string | null): Headers {
  const headers = new Headers({ Accept: "application/json" });
  if (options.body !== undefined) headers.set("Content-Type", "application/json");
  if (options.idempotencyKey) headers.set("Idempotency-Key", options.idempotencyKey);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  return headers;
}

async function send(path: string, options: RequestOptions, token: string | null) {
  try {
    return await fetch(`${API_URL}${path}`, {
      method: options.method ?? "GET",
      headers: headersFor(options, token),
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      credentials: "include", // the refresh cookie (HttpOnly, Path=/v1/auth)
      signal: options.signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiError(0, "NETWORK", null, {});
  }
}

export async function toApiError(response: Response): Promise<ApiError> {
  const body = (await response.json().catch(() => ({}))) as Record<string, unknown>;
  const retryAfter = Number(response.headers.get("Retry-After"));
  return new ApiError(
    response.status,
    typeof body.code === "string" ? body.code : `HTTP_${response.status}`,
    Number.isFinite(retryAfter) && retryAfter > 0 ? retryAfter : null,
    body,
  );
}

/** Sends with this action's key. A duplicate still in progress (the same key racing itself) is
 *  retried once, with the same key, after Retry-After. */
export async function sendKeyed<T>(path: string, method: "POST" | "PATCH", body: object | undefined, key: string): Promise<T> {
  const send = () => api<T>(path, { method, body, idempotencyKey: key });
  try {
    return await send();
  } catch (error) {
    if (!(error instanceof ApiError && error.code === "IDEMPOTENCY_CONFLICT" && error.body.in_progress)) {
      throw error;
    }
    await new Promise((resolve) => setTimeout(resolve, (error.retryAfterSec ?? 1) * 1000));
    return send();
  }
}

/** JSON request to the API. A 401 on an authenticated call refreshes once and retries the
 *  same request (same Idempotency-Key); a second 401 is returned to the caller. */
export async function api<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const auth = options.auth ?? true;
  let response = await send(path, options, auth ? getAccessToken() : null);
  if (response.status === 401 && auth) {
    const session = await refreshSession();
    if (session) response = await send(path, options, session.accessToken);
  }
  if (!response.ok) throw await toApiError(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}
