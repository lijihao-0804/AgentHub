/**
 * Shared API request helper for the documented /api/v1 envelope.
 * Each call supplies its own workspaceId + accessToken; credentials are
 * never stored in module state.
 */
export class ApiError extends Error {
  code: string;
  status: number;

  constructor(code: string, message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
  }
}

export const apiBaseUrl = (process.env.NEXT_PUBLIC_API_BASE_URL?.trim() ?? "").replace(/\/$/, "");

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

/**
 * Registered by the session provider. When a request comes back 401 the
 * transport asks it for a renewed access token (single-flight behind the
 * provider's own refresh scheduling) and replays the request exactly once
 * with it; ``null`` means the session is really gone and the original 401
 * stands. Auth endpoints themselves never carry a token and never trigger it,
 * so there is no refresh loop.
 */
type TokenRefresher = () => Promise<string | null>;
let tokenRefresher: TokenRefresher | null = null;

export function setTokenRefresher(refresher: TokenRefresher | null): void {
  tokenRefresher = refresher;
}

/**
 * Core transport for the documented /api/v1 envelope.
 *
 * `credentials: "include"` is always sent so the backend-owned HttpOnly
 * refresh cookie travels with /auth/* calls. The refresh cookie is never
 * read by JavaScript.
 */
async function transport<T>(
  path: string,
  init: { method?: string; body?: unknown; accessToken?: string; formData?: FormData; signal?: AbortSignal },
): Promise<T> {
  const send = (token: string | undefined) => {
    const hasJsonBody = init.formData === undefined && init.body !== undefined;
    return fetch(`${apiBaseUrl}${path}`, {
      method: init.method ?? "GET",
      headers: {
        ...(hasJsonBody ? { "Content-Type": "application/json" } : {}),
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      credentials: "include",
      ...(init.signal !== undefined ? { signal: init.signal } : {}),
      ...(init.formData !== undefined
        ? { body: init.formData }
        : hasJsonBody
          ? { body: JSON.stringify(init.body) }
          : {}),
    });
  };
  const token = init.accessToken?.trim();
  let response = await send(token);
  if (response.status === 401 && token && tokenRefresher) {
    const renewed = await tokenRefresher().catch(() => null);
    // A renewal that returned the unchanged token (no rotation upstream) is
    // treated as "no renewal": replaying with it would just 401 again. The
    // caller keeps the original 401 even though the session is technically
    // healthy -- a rare, fail-visible outcome beats a retry storm.
    if (renewed && renewed.trim() && renewed.trim() !== token) {
      response = await send(renewed.trim());
    }
  }
  const body = (await response.json().catch(() => null)) as unknown;
  if (!response.ok) {
    const error = isRecord(body) && isRecord(body.error) ? body.error : {};
    // A body that is not the API envelope (a proxy's HTML error page, an
    // empty body) means the API itself never answered -- report that as its
    // own condition instead of a generic request failure.
    const envelopeMissing = !isRecord(body) || !isRecord(body.error);
    const code = envelopeMissing
      ? "UPSTREAM_UNAVAILABLE"
      : typeof error.code === "string"
        ? error.code
        : "REQUEST_FAILED";
    throw new ApiError(
      code,
      typeof error.message === "string" ? error.message : "The API request failed.",
      response.status,
    );
  }
  return body as T;
}

export async function apiRequest<T>(
  path: string,
  accessToken: string,
  init?: { method?: string; body?: unknown; signal?: AbortSignal },
): Promise<T> {
  const token = accessToken.trim();
  if (!token) {
    throw new ApiError("SESSION_REQUIRED", "A workspace session with an access token is required.", 401);
  }
  return transport<T>(path, {
    method: init?.method,
    body: init?.body,
    signal: init?.signal,
    accessToken: token,
  });
}

/** Multipart upload against the same envelope and error contract. */
export async function apiUpload<T>(
  path: string,
  accessToken: string,
  formData: FormData,
  init?: { signal?: AbortSignal },
): Promise<T> {
  const token = accessToken.trim();
  if (!token) {
    throw new ApiError("SESSION_REQUIRED", "A workspace session with an access token is required.", 401);
  }
  return transport<T>(path, { method: "POST", formData, signal: init?.signal, accessToken: token });
}

/**
 * Unauthenticated request for the /auth/* surface. Used before an access
 * token exists (login, register) and for cookie-only refresh/logout.
 */
export function publicApiRequest<T>(
  path: string,
  init?: { method?: string; body?: unknown },
): Promise<T> {
  return transport<T>(path, { method: init?.method, body: init?.body });
}

/** Shared shape for workspace-scoped client calls. */
export type AuthInput = { workspaceId: string; accessToken: string };

/**
 * Dictionary keys for localized hints of common API failures. The hint
 * text itself lives in the i18n dictionaries; raw server messages are
 * never translated here.
 */
export type ErrorHintKey =
  | "errors.hint.sessionInvalid"
  | "errors.hint.forbidden"
  | "errors.hint.notFound";

export function errorHintKey(error: ApiError): ErrorHintKey | null {
  if (error.status === 401) return "errors.hint.sessionInvalid";
  if (error.status === 403) return "errors.hint.forbidden";
  if (error.status === 404) return "errors.hint.notFound";
  return null;
}

export function toApiError(error: unknown, fallbackMessage: string): ApiError {
  if (error instanceof ApiError) return error;
  return new ApiError("REQUEST_FAILED", fallbackMessage, 0);
}
