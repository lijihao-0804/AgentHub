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

/** Human hint for common API failures; safe, no stack details. */
export function errorHint(error: ApiError): string | undefined {
  if (error.status === 401) {
    return "The workspace session may be missing or the access token is no longer valid. Reconnect the session from the top bar.";
  }
  if (error.status === 403) {
    return "The current token does not have permission for this workspace resource.";
  }
  if (error.status === 404) {
    return "The resource was not found in this workspace.";
  }
  return undefined;
}

export function toApiError(error: unknown, fallbackMessage: string): ApiError {
  if (error instanceof ApiError) return error;
  return new ApiError("REQUEST_FAILED", fallbackMessage, 0);
}
