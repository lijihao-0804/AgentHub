import { apiRequest, isRecord } from "./api-client";

/**
 * Organization / workspace listing for the control plane shell.
 *
 * Membership management is out of scope for M7.5-A: the shell only needs
 * to list what the signed-in user can reach and select one workspace.
 */

export type Organization = {
  id: string;
  name: string;
};

export type Workspace = {
  id: string;
  organization_id: string;
  name: string;
};

/**
 * Accepts both a bare array and the `{ items: [...] }` envelope so the
 * client stays correct whichever list shape the endpoint returns.
 */
export function listItems<T>(payload: unknown): T[] {
  if (Array.isArray(payload)) return payload as T[];
  if (isRecord(payload) && Array.isArray(payload.items)) return payload.items as T[];
  return [];
}

export async function listOrganizations(accessToken: string): Promise<Organization[]> {
  const payload = await apiRequest<unknown>("/api/v1/organizations", accessToken);
  return listItems<Organization>(payload);
}

export async function listWorkspaces(accessToken: string): Promise<Workspace[]> {
  const payload = await apiRequest<unknown>("/api/v1/workspaces", accessToken);
  return listItems<Workspace>(payload);
}

export function getWorkspace(workspaceId: string, accessToken: string): Promise<Workspace> {
  return apiRequest<Workspace>(`/api/v1/workspaces/${encodeURIComponent(workspaceId)}`, accessToken);
}

export function createOrganization(accessToken: string, body: { name: string }): Promise<Organization> {
  return apiRequest<Organization>("/api/v1/organizations", accessToken, {
    method: "POST",
    body: { name: body.name },
  });
}

export function createWorkspace(
  accessToken: string,
  body: { organization_id: string; name: string },
): Promise<Workspace> {
  return apiRequest<Workspace>("/api/v1/workspaces", accessToken, {
    method: "POST",
    body: { organization_id: body.organization_id, name: body.name },
  });
}
