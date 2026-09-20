import { apiRequest, type AuthInput } from "./api-client";
import { listItems } from "./tenancy";

/**
 * Agent draft, binding, publish and version client.
 *
 * Runtime limits, resolved specs and spec hashes are produced by the
 * backend at publish time; the frontend only submits drafts and renders
 * what publish returns.
 */

export type KnowledgeBindingMode = "PINNED" | "LATEST";

function agentsBase(workspaceId: string): string {
  return `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/agents`;
}

export type ModelRetryPolicy = { max_attempts?: number };

export type RetrievalConfig = {
  embedding_model?: string | null;
  reranker_model?: string | null;
  dense_top_k?: number | null;
  sparse_top_k?: number | null;
  candidate_top_k?: number | null;
  final_top_k?: number | null;
};

export type ContextBudget = {
  reserved_output_tokens?: number | null;
  max_retrieval_tokens?: number | null;
  max_tool_result_tokens?: number | null;
};

export type RuntimeConfig = {
  max_steps?: number | null;
  max_tool_calls?: number | null;
  max_identical_calls?: number | null;
  max_parallel_reads?: number | null;
  context_budget?: ContextBudget | null;
};

export type Agent = {
  id: string;
  workspace_id: string;
  name: string;
  description: string | null;
  system_prompt: string;
  prompt_version: number;
  model_profile_id: string;
  knowledge_binding_mode: string;
  model_retry_policy: ModelRetryPolicy;
  retrieval_config: RetrievalConfig;
  runtime_config: RuntimeConfig;
  created_at: string;
  updated_at: string;
};

export type AgentCreateInput = {
  name: string;
  description?: string | null;
  system_prompt: string;
  prompt_version?: number;
  model_profile_id: string;
  knowledge_binding_mode?: KnowledgeBindingMode;
  model_retry_policy?: ModelRetryPolicy;
  retrieval_config?: RetrievalConfig;
  runtime_config?: RuntimeConfig;
};

export type AgentPatchInput = Partial<AgentCreateInput>;

export type AgentVersion = {
  id: string;
  workspace_id: string;
  agent_id: string;
  version_number: number;
  spec_schema_version: number;
  resolved_spec: Record<string, unknown>;
  resolved_spec_hash: string;
  created_at: string;
  created_by?: string | null;
};

export type AgentPublishResult = {
  agent_version_id: string;
  agent_id: string;
  version_number: number;
  resolved_spec_hash: string;
  created_at: string;
};

export async function listAgents(input: AuthInput): Promise<Agent[]> {
  const payload = await apiRequest<unknown>(agentsBase(input.workspaceId), input.accessToken);
  return listItems<Agent>(payload);
}

export function getAgent(input: AuthInput, agentId: string): Promise<Agent> {
  return apiRequest<Agent>(`${agentsBase(input.workspaceId)}/${encodeURIComponent(agentId)}`, input.accessToken);
}

export function createAgent(input: AuthInput, body: AgentCreateInput): Promise<Agent> {
  return apiRequest<Agent>(agentsBase(input.workspaceId), input.accessToken, { method: "POST", body });
}

export function patchAgent(input: AuthInput, agentId: string, body: AgentPatchInput): Promise<Agent> {
  return apiRequest<Agent>(
    `${agentsBase(input.workspaceId)}/${encodeURIComponent(agentId)}`,
    input.accessToken,
    { method: "PATCH", body },
  );
}

export function publishAgent(input: AuthInput, agentId: string): Promise<AgentPublishResult> {
  return apiRequest<AgentPublishResult>(
    `${agentsBase(input.workspaceId)}/${encodeURIComponent(agentId)}/publish`,
    input.accessToken,
    { method: "POST", body: {} },
  );
}

export async function listAgentVersions(input: AuthInput, agentId: string): Promise<AgentVersion[]> {
  const payload = await apiRequest<unknown>(
    `${agentsBase(input.workspaceId)}/${encodeURIComponent(agentId)}/versions`,
    input.accessToken,
  );
  return listItems<AgentVersion>(payload);
}

// ---------- Knowledge bindings ----------

/**
 * A PINNED binding carries a `snapshot_id`; a LATEST binding carries
 * none. The snapshot hash is server-derived and is never submitted.
 */
export type AgentKnowledgeBinding = {
  knowledge_base_id: string;
  binding_mode: KnowledgeBindingMode;
  snapshot_id: string | null;
};

export async function getAgentKnowledgeBindings(
  input: AuthInput,
  agentId: string,
): Promise<AgentKnowledgeBinding[]> {
  const payload = await apiRequest<unknown>(
    `${agentsBase(input.workspaceId)}/${encodeURIComponent(agentId)}/knowledge-bindings`,
    input.accessToken,
  );
  return listItems<AgentKnowledgeBinding>(payload);
}

/** Replaces the full binding collection in one request. */
export async function putAgentKnowledgeBindings(
  input: AuthInput,
  agentId: string,
  bindings: AgentKnowledgeBinding[],
): Promise<AgentKnowledgeBinding[]> {
  const payload = await apiRequest<unknown>(
    `${agentsBase(input.workspaceId)}/${encodeURIComponent(agentId)}/knowledge-bindings`,
    input.accessToken,
    {
      method: "PUT",
      body: {
        bindings: bindings.map((binding) => ({
          knowledge_base_id: binding.knowledge_base_id,
          binding_mode: binding.binding_mode,
          snapshot_id: binding.binding_mode === "PINNED" ? binding.snapshot_id : null,
        })),
      },
    },
  );
  return listItems<AgentKnowledgeBinding>(payload);
}

// ---------- Tool bindings ----------

/** `tool_revision_id === null` means "resolve latest at publish time". */
export type AgentToolBinding = {
  tool_id: string;
  tool_revision_id: string | null;
};

export async function getAgentToolBindings(input: AuthInput, agentId: string): Promise<AgentToolBinding[]> {
  const payload = await apiRequest<unknown>(
    `${agentsBase(input.workspaceId)}/${encodeURIComponent(agentId)}/tool-bindings`,
    input.accessToken,
  );
  return listItems<AgentToolBinding>(payload);
}

export async function putAgentToolBindings(
  input: AuthInput,
  agentId: string,
  bindings: AgentToolBinding[],
): Promise<AgentToolBinding[]> {
  const payload = await apiRequest<unknown>(
    `${agentsBase(input.workspaceId)}/${encodeURIComponent(agentId)}/tool-bindings`,
    input.accessToken,
    {
      method: "PUT",
      body: {
        bindings: bindings.map((binding) => ({
          tool_id: binding.tool_id,
          tool_revision_id: binding.tool_revision_id,
        })),
      },
    },
  );
  return listItems<AgentToolBinding>(payload);
}
