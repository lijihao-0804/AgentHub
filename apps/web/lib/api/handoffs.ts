import { apiRequest, isRecord, ApiError, type AuthInput } from "@/lib/api/client";

export type HandoffCase = {
  id: string; workspace_id: string; thread_id: string; source_artifact_id: string; source_hash: string;
  status: "OPEN" | "ASSIGNED" | "IN_PROGRESS" | "CLOSED"; version: number;
  created_by: string; assignee_id: string | null; claimed_by: string | null; closed_by: string | null;
  closure_reason: string | null; unresolved_items: string[]; created_at: string; updated_at: string;
  claimed_at: string | null; closed_at: string | null;
};
export type HandoffAssignee = { user_id: string; email: string };

function decode(value: unknown): HandoffCase {
  if (!isRecord(value) || !["OPEN", "ASSIGNED", "IN_PROGRESS", "CLOSED"].includes(String(value.status)) ||
      typeof value.version !== "number" || !Number.isSafeInteger(value.version) || value.version < 1 ||
      ["id", "workspace_id", "thread_id", "source_artifact_id", "source_hash", "created_by", "created_at", "updated_at"].some(k => typeof value[k] !== "string" || !value[k]) ||
      ["assignee_id", "claimed_by", "closed_by", "closure_reason", "claimed_at", "closed_at"].some(k => value[k] !== null && typeof value[k] !== "string") ||
      !Array.isArray(value.unresolved_items) || value.unresolved_items.some(v => typeof v !== "string")) {
    throw new ApiError("INVALID_RESPONSE", "Invalid handoff response", 502);
  }
  return value as HandoffCase;
}
function base(auth: AuthInput) { return `/api/v1/workspaces/${encodeURIComponent(auth.workspaceId)}`; }
function scoped(auth: AuthInput, value: unknown): HandoffCase {
  const row = decode(value);
  if (row.workspace_id !== auth.workspaceId) throw new ApiError("INVALID_RESPONSE", "Workspace mismatch", 502);
  return row;
}
export async function getArtifactHandoff(auth: AuthInput, artifactId: string): Promise<HandoffCase | null> {
  const value = await apiRequest<unknown>(`${base(auth)}/artifacts/${encodeURIComponent(artifactId)}/handoff`, auth.accessToken);
  if (value === null) return null;
  const row = scoped(auth, value);
  if (row.source_artifact_id !== artifactId) throw new ApiError("INVALID_RESPONSE", "Artifact mismatch", 502);
  return row;
}
export async function openHandoff(auth: AuthInput, artifactId: string): Promise<HandoffCase> {
  const value = await apiRequest<unknown>(`${base(auth)}/artifacts/${encodeURIComponent(artifactId)}/handoff`, auth.accessToken, { method: "POST" });
  const row = scoped(auth, value);
  if (row.source_artifact_id !== artifactId) throw new ApiError("INVALID_RESPONSE", "Artifact mismatch", 502);
  return row;
}
export async function changeHandoff(auth: AuthInput, id: string, action: "assign" | "claim" | "close", payload: { expected_version: number; assignee_id?: string; reason?: string; unresolved_items?: string[] }): Promise<HandoffCase> {
  const value = await apiRequest<unknown>(`${base(auth)}/handoffs/${encodeURIComponent(id)}/${action}`, auth.accessToken, { method: "POST", body: payload });
  const row = scoped(auth, value);
  if (row.id !== id) throw new ApiError("INVALID_RESPONSE", "Handoff mismatch", 502);
  return row;
}
export async function listHandoffAssignees(auth: AuthInput): Promise<HandoffAssignee[]> {
  const value = await apiRequest<unknown>(`${base(auth)}/handoffs/assignees`, auth.accessToken);
  if (!Array.isArray(value) || value.some(v => !isRecord(v) || typeof v.user_id !== "string" || typeof v.email !== "string")) throw new ApiError("INVALID_RESPONSE", "Invalid assignee response", 502);
  return value as HandoffAssignee[];
}

