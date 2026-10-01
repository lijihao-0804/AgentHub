import { apiRequest, isRecord } from "@/lib/api/client";

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
  // Caller-relative access, present on the single-workspace read (absent on
  // list responses).
  org_role?: string | null;
  workspace_role?: string | null;
  permissions?: string[];
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

/**
 * Same envelope tolerance as ``listItems`` but honest about protocol drift:
 * an unrecognized shape is ``null`` ("failed to parse"), not ``[]`` ("genuinely
 * empty"). The shell must be able to tell those apart -- treating a failed
 * tenancy read as "no workspaces" would wipe the remembered selection.
 */
export function listEnvelope<T>(payload: unknown): T[] | null {
  if (Array.isArray(payload)) return payload as T[];
  if (isRecord(payload) && Array.isArray(payload.items)) return payload.items as T[];
  return null;
}

export async function listOrganizations(accessToken: string): Promise<Organization[] | null> {
  const payload = await apiRequest<unknown>("/api/v1/organizations", accessToken);
  return listEnvelope<Organization>(payload);
}

export async function listWorkspaces(accessToken: string): Promise<Workspace[] | null> {
  const payload = await apiRequest<unknown>("/api/v1/workspaces", accessToken);
  return listEnvelope<Workspace>(payload);
}

/**
 * Reads one workspace with the caller's own role and permissions: the shell
 * uses it for the active workspace, so the UI can present a role-appropriate
 * surface instead of letting users discover 403s by submit.
 */
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

// ---------- Members ----------

export type Member = { user_id: string; email: string; role: string };

export function listOrganizationMembers(
  organizationId: string,
  accessToken: string,
): Promise<Member[]> {
  return apiRequest<Member[]>(
    `/api/v1/organizations/${encodeURIComponent(organizationId)}/members`,
    accessToken,
  );
}

export async function removeOrganizationMember(
  organizationId: string,
  userId: string,
  accessToken: string,
): Promise<void> {
  await apiRequest<unknown>(
    `/api/v1/organizations/${encodeURIComponent(organizationId)}/members/${encodeURIComponent(userId)}`,
    accessToken,
    { method: "DELETE" },
  );
}

export function listWorkspaceMembers(
  workspaceId: string,
  accessToken: string,
): Promise<Member[]> {
  return apiRequest<Member[]>(
    `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/members`,
    accessToken,
  );
}

export async function removeWorkspaceMember(
  workspaceId: string,
  userId: string,
  accessToken: string,
): Promise<void> {
  await apiRequest<unknown>(
    `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/members/${encodeURIComponent(userId)}`,
    accessToken,
    { method: "DELETE" },
  );
}
