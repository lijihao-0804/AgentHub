import { ApiError, apiRequest, isRecord, type AuthInput } from "@/lib/api/client";
import { listItems } from "@/lib/api/tenancy";

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
  [key: string]: unknown;
};

export type ContextPreset =
  | "MINI_16K"
  | "STANDARD_32K"
  | "GENERAL_64K"
  | "LONG_DOCUMENT_128K"
  | "EXTENDED_256K";

export type ConfigurationSource = "UNSET" | "DEFAULT" | "CUSTOM";

export type ContextBudgetField =
  | "reserved_output_tokens"
  | "max_retrieval_tokens"
  | "max_tool_result_tokens";

export type ContextBudgetSources = Partial<Record<ContextBudgetField, ConfigurationSource>>;

export type ContextPolicyRequest = {
  policy_version: "adaptive_context_v1";
  preset: ContextPreset;
  workspace_input_limit?: number;
  workspace_input_limit_source?: ConfigurationSource;
  soft_trigger_percent: number;
  hard_trigger_percent: number;
  target_percent: number;
  retained_recent_turns?: number;
  max_history_groups?: number;
  safety_reserve_percent?: number;
  safety_reserve_tokens?: number;
  counter_id?: "approx_language_v1";
};

export type FrozenContextPolicyModel = {
  profile_id: string;
  credential_ref: string;
  provider: string;
  model: string;
  resolved_model: string;
  budget: {
    reserved_output_tokens: number;
    applicable_input_limit_tokens: number;
    safety_reserve_tokens: number;
    available_input_tokens: number;
  };
  reasons: string[];
};

export type FrozenContextPolicyProjection = {
  policy_version: "adaptive_context_v1";
  preset: ContextPreset;
  workspace_input_limit: number;
  workspace_input_limit_source: ConfigurationSource;
  input_limit_tokens: number;
  model_available_input_tokens: number;
  min_total_context_tokens: number | null;
  reserved_output_tokens: number;
  thresholds: {
    soft: number;
    hard: number;
    target: number;
  };
  counter: {
    id: string;
    version: number;
    precision: string;
  };
  models: FrozenContextPolicyModel[];
  limitations: Array<Record<string, unknown>>;
  unknowns: string[];
  retained_recent_turns?: number;
  max_history_groups?: number;
  safety_reserve_percent?: number;
  safety_reserve_tokens?: number;
  soft_trigger_percent?: number;
  hard_trigger_percent?: number;
  target_percent?: number;
};

/**
 * Per-agent memory switches. Both default off, and an agent that turns
 * neither on publishes the same spec hash it published before memory existed.
 */
export type MemorySelectionPolicy = "lexical_overlap_v1";

export type MemoryConfig = {
  thread_history_search?: boolean;
  long_term_memory?: boolean;
  max_memory_tokens?: number | null;
  max_memory_tokens_source?: ConfigurationSource | null;
  selection_policy?: MemorySelectionPolicy | null;
};

export type SummaryPolicy = {
  enabled: true;
  policy_version: "thread_summary_policy_v1";
  prompt_version: "summary_candidate_zh_v1";
  validator_version: "summary_mechanical_v1";
  tokenizer_id: "approx_language_v1";
  tokenizer_version: number;
  soft_trigger_percent: number;
  hard_trigger_percent: number;
  target_percent: number;
};

export type RuntimeConfig = {
  max_steps?: number | null;
  max_tool_calls?: number | null;
  max_identical_calls?: number | null;
  max_parallel_reads?: number | null;
  max_cost_micro_usd?: number | null;
  context_budget?: ContextBudget | null;
  context_budget_sources?: ContextBudgetSources | null;
  memory?: MemoryConfig | null;
  context_policy?: ContextPolicyRequest | null;
  history_policy?: "deterministic_history_v1" | null;
  summary_policy?: SummaryPolicy | null;
  [key: string]: unknown;
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

export type AgentDeriveResult = {
  source_version_id: string;
  source_version_number: number;
  source_resolved_spec_hash: string;
  values: {
    system_prompt: string | null;
    prompt_version: number | null;
    model_profile_id: string | null;
    knowledge_binding_mode: string | null;
    model_retry_policy: unknown;
    retrieval_config: unknown;
    runtime_config: unknown;
  };
};

/** Draft-shaped values extracted from a published version; applying them goes
 * through the normal (fully validated) draft update path. */
export function deriveAgentDraft(
  input: AuthInput,
  agentId: string,
  versionId: string,
): Promise<AgentDeriveResult> {
  return apiRequest<AgentDeriveResult>(
    `${agentsBase(input.workspaceId)}/${encodeURIComponent(agentId)}/versions/${encodeURIComponent(versionId)}/derive`,
    input.accessToken,
    { method: "POST" },
  );
}

export function publishAgent(input: AuthInput, agentId: string): Promise<AgentPublishResult> {
  return apiRequest<AgentPublishResult>(
    `${agentsBase(input.workspaceId)}/${encodeURIComponent(agentId)}/publish`,
    input.accessToken,
    { method: "POST", body: {} },
  );
}

/**
 * Server-resolved preview of the draft as it stands right now.
 *
 * A READY preflight is a readiness report, not a lock, a reservation or a
 * promise about a later publish: publishing still goes through
 * `publishAgent`, which re-validates and re-resolves from scratch.
 */
export type AgentPreflightResult = {
  status: "READY";
  agent_id: string;
  workspace_id: string;
  draft_updated_at: string;
  spec_schema_version: number;
  resolved_spec_hash: string;
  resolved_spec: Record<string, unknown>;
};

export function preflightAgent(input: AuthInput, agentId: string): Promise<AgentPreflightResult> {
  return apiRequest<AgentPreflightResult>(
    `${agentsBase(input.workspaceId)}/${encodeURIComponent(agentId)}/preflight`,
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

/** Reads one immutable version directly — never a list plus a client-side find. */
export function getAgentVersion(
  input: AuthInput,
  agentId: string,
  versionId: string,
): Promise<AgentVersion> {
  return apiRequest<AgentVersion>(
    `${agentsBase(input.workspaceId)}/${encodeURIComponent(agentId)}/versions/${encodeURIComponent(versionId)}`,
    input.accessToken,
  );
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
      // The endpoint takes the collection itself, not an envelope object.
      body: bindings.map((binding) => ({
        knowledge_base_id: binding.knowledge_base_id,
        binding_mode: binding.binding_mode,
        snapshot_id: binding.binding_mode === "PINNED" ? binding.snapshot_id : null,
      })),
    },
  );
  return listItems<AgentKnowledgeBinding>(payload);
}

// ---------- Wiki binding ----------

/**
 * The draft-level Wiki binding is a single replaceable value. `LATEST`
 * resolves once at publication; readers of an existing AgentVersion use the
 * frozen `resolved_spec.wiki_binding` instead of resolving it again.
 */
type AgentWikiBindingIdentity = {
  workspace_id: string;
  agent_id: string;
  knowledge_base_id: string;
};

export type AgentWikiBinding =
  | (AgentWikiBindingIdentity & {
      binding_mode: "PINNED";
      wiki_publication_id: string;
      publication_hash: string;
    })
  | (AgentWikiBindingIdentity & {
      binding_mode: "LATEST";
      wiki_publication_id: null;
      publication_hash: null;
    });

export type AgentWikiBindingInput =
  | {
      knowledge_base_id: string;
      binding_mode: "PINNED";
      wiki_publication_id: string;
    }
  | {
      knowledge_base_id: string;
      binding_mode: "LATEST";
      wiki_publication_id: null;
    };

function nonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim() !== "";
}

function parseAgentWikiBinding(
  payload: unknown,
  expected: { workspaceId: string; agentId: string },
): AgentWikiBinding | null {
  if (payload === null) return null;
  if (
    !isRecord(payload) ||
    Array.isArray(payload) ||
    payload.workspace_id !== expected.workspaceId ||
    payload.agent_id !== expected.agentId ||
    !nonEmptyString(payload.knowledge_base_id) ||
    (payload.binding_mode !== "PINNED" && payload.binding_mode !== "LATEST")
  ) {
    throw new ApiError(
      "INVALID_RESPONSE",
      "The server returned an invalid Agent Wiki binding.",
      502,
    );
  }

  const mode = payload.binding_mode;
  const publicationId = payload.wiki_publication_id;
  const publicationHash = payload.publication_hash;
  if (mode === "LATEST") {
    if (publicationId !== null || publicationHash !== null) {
      throw new ApiError(
        "INVALID_RESPONSE",
        "The server returned an invalid Agent Wiki binding.",
        502,
      );
    }
    return {
      workspace_id: payload.workspace_id,
      agent_id: payload.agent_id,
      knowledge_base_id: payload.knowledge_base_id,
      binding_mode: mode,
      wiki_publication_id: null,
      publication_hash: null,
    };
  }

  if (!nonEmptyString(publicationId) || !nonEmptyString(publicationHash)) {
    throw new ApiError(
      "INVALID_RESPONSE",
      "The server returned an invalid Agent Wiki binding.",
      502,
    );
  }
  return {
    workspace_id: payload.workspace_id,
    agent_id: payload.agent_id,
    knowledge_base_id: payload.knowledge_base_id,
    binding_mode: mode,
    wiki_publication_id: publicationId,
    publication_hash: publicationHash,
  };
}

export async function getAgentWikiBinding(
  input: AuthInput,
  agentId: string,
): Promise<AgentWikiBinding | null> {
  const payload = await apiRequest<unknown>(
    `${agentsBase(input.workspaceId)}/${encodeURIComponent(agentId)}/wiki-binding`,
    input.accessToken,
  );
  return parseAgentWikiBinding(payload, { workspaceId: input.workspaceId, agentId });
}

/** Passing `null` clears the draft binding; the successful response is also null. */
export async function replaceAgentWikiBinding(
  input: AuthInput,
  agentId: string,
  binding: AgentWikiBindingInput | null,
): Promise<AgentWikiBinding | null> {
  const payload = await apiRequest<unknown>(
    `${agentsBase(input.workspaceId)}/${encodeURIComponent(agentId)}/wiki-binding`,
    input.accessToken,
    { method: "PUT", body: binding },
  );
  return parseAgentWikiBinding(payload, { workspaceId: input.workspaceId, agentId });
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
      // The endpoint takes the collection itself, not an envelope object.
      body: bindings.map((binding) => ({
        tool_id: binding.tool_id,
        tool_revision_id: binding.tool_revision_id,
      })),
    },
  );
  return listItems<AgentToolBinding>(payload);
}

// ---------- Long-term memory ----------

/**
 * One thing an agent remembers across threads.
 *
 * `status` is the whole vocabulary: ACTIVE is available to new runs,
 * SUPERSEDED is reserved for a future trusted semantic-replacement flow,
 * and INVALIDATED is switched off by a human. Automatic extraction does not
 * currently create SUPERSEDED rows. Nothing is ever deleted, so a run that
 * already used a memory can still explain itself — which is why the UI offers
 * "stop using this" rather than "delete".
 */
export type AgentMemoryStatus = "ACTIVE" | "SUPERSEDED" | "INVALIDATED";
export type AgentMemoryKind = "FACT" | "PREFERENCE" | "DECISION" | "CONSTRAINT";

export type AgentMemory = {
  id: string;
  workspace_id: string;
  agent_id: string;
  thread_id: string | null;
  source_run_id: string | null;
  content: string;
  kind: AgentMemoryKind;
  status: AgentMemoryStatus;
  superseded_by_id: string | null;
  salience: number;
  provenance: Record<string, unknown>;
  expires_at: string | null;
  last_used_at: string | null;
  created_at: string;
  updated_at: string;
};

export type AgentMemoryPage = { items: AgentMemory[]; total: number };

export async function listAgentMemories(
  input: AuthInput,
  agentId: string,
  options: {
    status?: AgentMemoryStatus;
    kind?: AgentMemoryKind;
    q?: string;
    limit?: number;
    offset?: number;
  } = {},
): Promise<AgentMemoryPage> {
  const query = new URLSearchParams();
  if (options.status) query.set("status", options.status);
  if (options.kind) query.set("kind", options.kind);
  if (options.q) query.set("q", options.q);
  if (options.limit !== undefined) query.set("limit", String(options.limit));
  if (options.offset !== undefined) query.set("offset", String(options.offset));
  const serialized = query.toString();
  const suffix = serialized ? `?${serialized}` : "";
  const payload = await apiRequest<AgentMemoryPage>(
    `${agentsBase(input.workspaceId)}/${encodeURIComponent(agentId)}/memories${suffix}`,
    input.accessToken,
  );
  return { items: payload.items ?? [], total: payload.total ?? 0 };
}

export async function invalidateAgentMemory(
  input: AuthInput,
  agentId: string,
  memoryId: string,
): Promise<AgentMemory> {
  return apiRequest<AgentMemory>(
    `${agentsBase(input.workspaceId)}/${encodeURIComponent(agentId)}/memories/${encodeURIComponent(memoryId)}/invalidate`,
    input.accessToken,
    { method: "POST" },
  );
}

export async function reactivateAgentMemory(
  input: AuthInput,
  agentId: string,
  memoryId: string,
): Promise<AgentMemory> {
  return apiRequest<AgentMemory>(
    `${agentsBase(input.workspaceId)}/${encodeURIComponent(agentId)}/memories/${encodeURIComponent(memoryId)}/reactivate`,
    input.accessToken,
    { method: "POST" },
  );
}
