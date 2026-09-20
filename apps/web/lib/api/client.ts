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
 * Core transport for the documented /api/v1 envelope.
 *
 * `credentials: "include"` is always sent so the backend-owned HttpOnly
 * refresh cookie travels with /auth/* calls. The refresh cookie is never
 * read by JavaScript.
 */
async function transport<T>(
  path: string,
  init: { method?: string; body?: unknown; accessToken?: string; formData?: FormData },
): Promise<T> {
  const token = init.accessToken?.trim();
  // The browser must set its own multipart boundary, so Content-Type is
  // only declared for JSON bodies.
  const hasJsonBody = init.formData === undefined && init.body !== undefined;
  const response = await fetch(`${apiBaseUrl}${path}`, {
    method: init.method ?? "GET",
    headers: {
      ...(hasJsonBody ? { "Content-Type": "application/json" } : {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    credentials: "include",
    ...(init.formData !== undefined
      ? { body: init.formData }
      : hasJsonBody
        ? { body: JSON.stringify(init.body) }
        : {}),
  });
  const body = (await response.json().catch(() => null)) as unknown;
  if (!response.ok) {
    const error = isRecord(body) && isRecord(body.error) ? body.error : {};
    throw new ApiError(
      typeof error.code === "string" ? error.code : "REQUEST_FAILED",
      typeof error.message === "string" ? error.message : "The API request failed.",
      response.status,
    );
  }
  return body as T;
}

export async function apiRequest<T>(
  path: string,
  accessToken: string,
  init?: { method?: string; body?: unknown },
): Promise<T> {
  const token = accessToken.trim();
  if (!token) {
    throw new ApiError("SESSION_REQUIRED", "A workspace session with an access token is required.", 401);
  }
  return transport<T>(path, { method: init?.method, body: init?.body, accessToken: token });
}

/** Multipart upload against the same envelope and error contract. */
export async function apiUpload<T>(
  path: string,
  accessToken: string,
  formData: FormData,
): Promise<T> {
  const token = accessToken.trim();
  if (!token) {
    throw new ApiError("SESSION_REQUIRED", "A workspace session with an access token is required.", 401);
  }
  return transport<T>(path, { method: "POST", formData, accessToken: token });
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
