import { apiRequest, type AuthInput } from "./api-client";
import { listItems } from "./tenancy";

/**
 * Tool management client.
 *
 * Effect, risk level and approval policy are backend-owned facts. The UI
 * only ever displays what the catalog or the tool revision returns — it
 * never derives or hard-codes them.
 */

export type ToolEffect = "READ" | "WRITE";
export type ToolRisk = "LOW" | "MEDIUM" | "HIGH";
export type ToolApprovalPolicy = "NEVER" | "ALWAYS";

function toolsBase(workspaceId: string): string {
  return `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/tools`;
}

export type ToolExecutionKind = "builtin" | "action";

export type ToolCatalogItem = {
  identity: string;
  description?: string | null;
  input_schema: Record<string, unknown>;
  effect: ToolEffect | string;
  risk_level: ToolRisk | string;
  approval_policy: ToolApprovalPolicy | string;
  timeout_seconds?: number | null;
  execution_kind: ToolExecutionKind | string;
};

/**
 * The governance projection is null for historic rows that have no
 * revision yet; those fields are rendered as unset rather than filled in
 * from builtin metadata.
 */
export type Tool = {
  id: string;
  workspace_id: string;
  name: string;
  description: string | null;
  enabled: boolean;
  created_at: string;
  identity: string | null;
  effect: ToolEffect | string | null;
  risk_level: ToolRisk | string | null;
  approval_policy: ToolApprovalPolicy | string | null;
  execution_kind: ToolExecutionKind | string | null;
  current_revision_id: string | null;
  current_revision_number: number | null;
  current_spec_hash: string | null;
};

export type ToolRevision = {
  id: string;
  workspace_id: string;
  tool_id: string;
  revision_number: number;
  spec: Record<string, unknown>;
  spec_hash: string;
  created_at: string;
  created_by?: string | null;
};

export async function listToolCatalog(input: AuthInput): Promise<ToolCatalogItem[]> {
  const payload = await apiRequest<unknown>(
    `/api/v1/workspaces/${encodeURIComponent(input.workspaceId)}/tool-catalog`,
    input.accessToken,
  );
  return listItems<ToolCatalogItem>(payload);
}

export async function listTools(input: AuthInput): Promise<Tool[]> {
  const payload = await apiRequest<unknown>(toolsBase(input.workspaceId), input.accessToken);
  return listItems<Tool>(payload);
}

export function getTool(input: AuthInput, toolId: string): Promise<Tool> {
  return apiRequest<Tool>(`${toolsBase(input.workspaceId)}/${encodeURIComponent(toolId)}`, input.accessToken);
}

/**
 * Creates a workspace tool from a builtin catalog entry. Only the builtin
 * identity plus presentation metadata is sent; the executable spec stays
 * server-owned.
 */
export function createTool(
  input: AuthInput,
  body: { identity: string; name: string; description?: string | null },
): Promise<Tool> {
  return apiRequest<Tool>(toolsBase(input.workspaceId), input.accessToken, {
    method: "POST",
    body: {
      identity: body.identity,
      name: body.name,
      description: body.description ?? null,
    },
  });
}

export function patchTool(
  input: AuthInput,
  toolId: string,
  body: { name?: string; description?: string | null; enabled?: boolean },
): Promise<Tool> {
  return apiRequest<Tool>(`${toolsBase(input.workspaceId)}/${encodeURIComponent(toolId)}`, input.accessToken, {
    method: "PATCH",
    body,
  });
}

export async function listToolRevisions(input: AuthInput, toolId: string): Promise<ToolRevision[]> {
  const payload = await apiRequest<unknown>(
    `${toolsBase(input.workspaceId)}/${encodeURIComponent(toolId)}/revisions`,
    input.accessToken,
  );
  return listItems<ToolRevision>(payload);
}

export function getToolRevision(
  input: AuthInput,
  toolId: string,
  revisionId: string,
): Promise<ToolRevision> {
  return apiRequest<ToolRevision>(
    `${toolsBase(input.workspaceId)}/${encodeURIComponent(toolId)}/revisions/${encodeURIComponent(revisionId)}`,
    input.accessToken,
  );
}

/** Reads a spec field from a revision without inventing a fallback. */
export function specField(revision: ToolRevision | null | undefined, key: string): string | null {
  const value = revision?.spec?.[key];
  return typeof value === "string" ? value : null;
}
