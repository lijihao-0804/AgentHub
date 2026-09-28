import { apiRequest } from "@/lib/api/client";

/**
 * Typed client for the remote MCP connection surface. Shapes mirror
 * apps/api/schemas/mcp_connections.py; the frontend never invents governance
 * fields the backend does not accept.
 */

type AuthInput = { workspaceId: string; accessToken: string };

function mcpBase(workspaceId: string): string {
  return `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/mcp-connections`;
}

export type McpConnection = {
  id: string;
  workspace_id: string;
  name: string;
  endpoint_url: string;
  auth_type: "NONE" | "BEARER";
  secret_configured: boolean;
  enabled: boolean;
  created_by: string;
  created_at: string;
  updated_at: string;
};

export type McpConnectionTest = {
  connection_id: string;
  status: "healthy" | "unavailable";
  failure_code: string | null;
  latency_ms: number;
  protocol_version: string | null;
  server_name: string | null;
};

export type McpDiscoveredTool = {
  name: string;
  title: string | null;
  description: string | null;
  input_schema: Record<string, unknown>;
  output_schema: Record<string, unknown> | null;
  remote_annotations: Record<string, boolean> | null;
};

export type McpDiscovery = {
  connection_id: string;
  protocol_version: string | null;
  server_name: string | null;
  tools: McpDiscoveredTool[];
};

export type McpToolImportInput = {
  remote_tool_name: string;
  identity: string;
  name: string | null;
  effect: "READ" | "WRITE";
  risk_level: "LOW" | "MEDIUM" | "HIGH";
  approval_policy: "NEVER" | "ALWAYS";
  timeout_seconds: number | null;
};

export async function listMcpConnections(input: AuthInput): Promise<McpConnection[]> {
  const payload = await apiRequest<unknown>(mcpBase(input.workspaceId), input.accessToken);
  return Array.isArray(payload) ? (payload as McpConnection[]) : [];
}

export function createMcpConnection(
  input: AuthInput,
  body: { name: string; endpoint_url: string; auth_type: "NONE" | "BEARER"; secret?: string },
): Promise<McpConnection> {
  return apiRequest<McpConnection>(mcpBase(input.workspaceId), input.accessToken, {
    method: "POST",
    body,
  });
}

export function testMcpConnection(input: AuthInput, connectionId: string): Promise<McpConnectionTest> {
  return apiRequest<McpConnectionTest>(
    `${mcpBase(input.workspaceId)}/${encodeURIComponent(connectionId)}/test`,
    input.accessToken,
    { method: "POST", body: {} },
  );
}

export function discoverMcpTools(input: AuthInput, connectionId: string): Promise<McpDiscovery> {
  return apiRequest<McpDiscovery>(
    `${mcpBase(input.workspaceId)}/${encodeURIComponent(connectionId)}/discover-tools`,
    input.accessToken,
    { method: "POST", body: {} },
  );
}

export function importMcpTool(
  input: AuthInput,
  connectionId: string,
  body: McpToolImportInput,
): Promise<{ id: string; name: string }> {
  return apiRequest<{ id: string; name: string }>(
    `${mcpBase(input.workspaceId)}/${encodeURIComponent(connectionId)}/import-tool`,
    input.accessToken,
    { method: "POST", body },
  );
}
