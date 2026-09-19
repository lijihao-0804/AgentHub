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

export async function apiRequest<T>(
  path: string,
  accessToken: string,
  init?: { method?: string; body?: unknown },
): Promise<T> {
  const token = accessToken.trim();
  if (!token) {
    throw new ApiError("SESSION_REQUIRED", "A workspace session with an access token is required.", 401);
  }
  const response = await fetch(`${apiBaseUrl}${path}`, {
    method: init?.method ?? "GET",
    headers: {
      ...(init?.body !== undefined ? { "Content-Type": "application/json" } : {}),
      Authorization: `Bearer ${token}`,
    },
    credentials: "include",
    ...(init?.body !== undefined ? { body: JSON.stringify(init.body) } : {}),
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
