import { publicApiRequest } from "@/lib/api/client";

/**
 * Auth client for the frozen M7.5-A contract.
 *
 * The access token returned here is handed to the in-memory session
 * provider and is never written to storage, cookies or the URL. The
 * refresh token is owned by the backend as an HttpOnly cookie; every
 * call below travels with `credentials: "include"` so that cookie is
 * sent and set, but JavaScript never reads it.
 */

const AUTH_BASE = "/api/v1/auth";

export type AuthSessionResponse = {
  user_id: string;
  access_token: string;
  expires_in?: number | null;
};

export type RegisterInput = {
  email: string;
  password: string;
};

export type LoginInput = {
  email: string;
  password: string;
};

export function register(input: RegisterInput): Promise<AuthSessionResponse> {
  return publicApiRequest<AuthSessionResponse>(`${AUTH_BASE}/register`, {
    method: "POST",
    body: { email: input.email, password: input.password },
  });
}

export function login(input: LoginInput): Promise<AuthSessionResponse> {
  return publicApiRequest<AuthSessionResponse>(`${AUTH_BASE}/login`, {
    method: "POST",
    body: { email: input.email, password: input.password },
  });
}

/**
 * Exchanges the HttpOnly refresh cookie for a fresh access token. A 401
 * here is the normal "no session yet" answer, not an API failure, and
 * callers present it as UNAUTHENTICATED rather than an error state.
 */
export function refresh(): Promise<AuthSessionResponse> {
  return publicApiRequest<AuthSessionResponse>(`${AUTH_BASE}/refresh`, { method: "POST", body: {} });
}

export function logout(): Promise<unknown> {
  return publicApiRequest<unknown>(`${AUTH_BASE}/logout`, { method: "POST", body: {} });
}
