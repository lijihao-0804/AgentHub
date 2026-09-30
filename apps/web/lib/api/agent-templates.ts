import { apiRequest } from "@/lib/api/client";
import type { AuthInput } from "@/lib/api/client";

export type AgentTemplate = {
  key: string;
  name: string;
  description: string;
  system_prompt: string;
  tool_hints: string[];
  thread_kind: string;
};

/** Read-only template catalog; the create form stays the only writer. */
export async function listAgentTemplates(input: AuthInput): Promise<AgentTemplate[]> {
  return apiRequest<AgentTemplate[]>(
    `/api/v1/workspaces/${encodeURIComponent(input.workspaceId)}/agent-templates`,
    input.accessToken,
  );
}
