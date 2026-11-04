import type {
  ConfigurationSource,
  ContextBudget,
  ContextBudgetSources,
  ContextPolicyRequest,
  ContextPreset,
  FrozenContextPolicyProjection,
  MemoryConfig,
  RuntimeConfig,
} from "@/lib/api/agents";

export const CONTEXT_POLICY_VERSION = "adaptive_context_v1" as const;
export const DEFAULT_CONTEXT_PRESET: ContextPreset = "GENERAL_64K";

export const CONTEXT_PRESETS: ReadonlyArray<{
  value: ContextPreset;
  tokens: number;
  label: string;
}> = [
  { value: "MINI_16K", tokens: 16_384, label: "16K" },
  { value: "STANDARD_32K", tokens: 32_768, label: "32K" },
  { value: "GENERAL_64K", tokens: 65_536, label: "64K" },
  { value: "LONG_DOCUMENT_128K", tokens: 131_072, label: "128K" },
  { value: "EXTENDED_256K", tokens: 262_144, label: "256K" },
];

const PRESET_TOKENS: Record<ContextPreset, number> = {
  MINI_16K: 16_384,
  STANDARD_32K: 32_768,
  GENERAL_64K: 65_536,
  LONG_DOCUMENT_128K: 131_072,
  EXTENDED_256K: 262_144,
};

const DEFAULT_THRESHOLDS = {
  target: 55,
  soft: 70,
  hard: 85,
} as const;

export type ContextPolicyDraft = {
  enabled: boolean;
  preset: ContextPreset;
  workspace_input_limit_source: ConfigurationSource;
  workspace_input_limit: string;
  soft_trigger_percent: string;
  hard_trigger_percent: string;
  target_percent: string;
  retained_recent_turns: string;
  max_history_groups: string;
  safety_reserve_percent: string;
  safety_reserve_tokens: string;
};

export type RuntimeLimitDraft = {
  max_steps: string;
  max_tool_calls: string;
  max_identical_calls: string;
  max_parallel_reads: string;
  reserved_output_tokens: string;
  max_retrieval_tokens: string;
  max_tool_result_tokens: string;
};

export type MemoryDraft = {
  thread_history_search: boolean;
  long_term_memory: boolean;
  selection_policy: "" | "lexical_overlap_v1";
};

export type MemoryDraftValidation = {
  valid: boolean;
  issue: "LONG_TERM_MEMORY_REQUIRED" | null;
};

export type ContextPolicyField =
  | "workspace_input_limit"
  | "soft_trigger_percent"
  | "hard_trigger_percent"
  | "target_percent"
  | "retained_recent_turns"
  | "max_history_groups"
  | "safety_reserve_percent"
  | "safety_reserve_tokens";

export type ContextPolicyValidationIssue = {
  field: ContextPolicyField | "thresholds";
  code: string;
};

export type ContextPolicyValidation = {
  valid: boolean;
  policy: ContextPolicyRequest | null;
  issues: ContextPolicyValidationIssue[];
  presetLimit: number;
  estimatedLimit: number | null;
  estimatedThresholds: {
    target: number;
    soft: number;
    hard: number;
  } | null;
};

export function defaultContextPolicyDraft(): ContextPolicyDraft {
  return {
    enabled: false,
    preset: DEFAULT_CONTEXT_PRESET,
    workspace_input_limit_source: "UNSET",
    workspace_input_limit: "",
    soft_trigger_percent: String(DEFAULT_THRESHOLDS.soft),
    hard_trigger_percent: String(DEFAULT_THRESHOLDS.hard),
    target_percent: String(DEFAULT_THRESHOLDS.target),
    retained_recent_turns: "2",
    max_history_groups: "256",
    safety_reserve_percent: "5",
    safety_reserve_tokens: "1024",
  };
}

export function defaultMemoryDraft(): MemoryDraft {
  return {
    thread_history_search: false,
    long_term_memory: false,
    selection_policy: "",
  };
}

export function memoryDraftFromConfig(
  memory: MemoryConfig | null | undefined,
): MemoryDraft {
  return {
    thread_history_search: memory?.thread_history_search === true,
    long_term_memory: memory?.long_term_memory === true,
    selection_policy:
      memory?.selection_policy === "lexical_overlap_v1"
        ? "lexical_overlap_v1"
        : "",
  };
}

export function validateMemoryDraft(draft: MemoryDraft): MemoryDraftValidation {
  if (draft.selection_policy === "lexical_overlap_v1" && !draft.long_term_memory) {
    return { valid: false, issue: "LONG_TERM_MEMORY_REQUIRED" };
  }
  return { valid: true, issue: null };
}

function textOrFallback(value: number | null | undefined, fallback: number): string {
  return value === null || value === undefined ? String(fallback) : String(value);
}

export function contextPolicyDraftFromPolicy(
  policy: ContextPolicyRequest | null | undefined,
): ContextPolicyDraft {
  if (!policy) return defaultContextPolicyDraft();
  const source = policy.workspace_input_limit_source ?? "DEFAULT";
  return {
    enabled: true,
    preset: policy.preset,
    workspace_input_limit_source: source,
    workspace_input_limit:
      source === "CUSTOM" && policy.workspace_input_limit !== undefined
        ? String(policy.workspace_input_limit)
        : "",
    soft_trigger_percent: textOrFallback(policy.soft_trigger_percent, DEFAULT_THRESHOLDS.soft),
    hard_trigger_percent: textOrFallback(policy.hard_trigger_percent, DEFAULT_THRESHOLDS.hard),
    target_percent: textOrFallback(policy.target_percent, DEFAULT_THRESHOLDS.target),
    retained_recent_turns: textOrFallback(policy.retained_recent_turns, 2),
    max_history_groups: textOrFallback(policy.max_history_groups, 256),
    safety_reserve_percent: textOrFallback(policy.safety_reserve_percent, 5),
    safety_reserve_tokens: textOrFallback(policy.safety_reserve_tokens, 1024),
  };
}

export function contextPresetLimit(preset: ContextPreset): number {
  return PRESET_TOKENS[preset];
}

function parseInteger(
  value: string,
  minimum: number,
  maximum: number,
): number | null {
  const trimmed = value.trim();
  if (!/^(0|[1-9]\d*)$/.test(trimmed)) return null;
  const parsed = Number(trimmed);
  return Number.isSafeInteger(parsed) && parsed >= minimum && parsed <= maximum ? parsed : null;
}

function scaledThreshold(limit: number, percent: number): number {
  return percent <= 55
    ? Math.floor((percent * limit) / 100)
    : Math.ceil((percent * limit) / 100);
}

export function estimateContextThresholds(
  limit: number,
  thresholds: { target: number; soft: number; hard: number },
): { target: number; soft: number; hard: number } {
  return {
    target: scaledThreshold(limit, thresholds.target),
    soft: scaledThreshold(limit, thresholds.soft),
    hard: scaledThreshold(limit, thresholds.hard),
  };
}

/**
 * Mirrors the server request validator so malformed drafts stay in the form
 * and never become a misleading partial PATCH.
 */
export function validateContextPolicyDraft(
  draft: ContextPolicyDraft,
): ContextPolicyValidation {
  const presetLimit = contextPresetLimit(draft.preset);
  if (!draft.enabled) {
    return {
      valid: true,
      policy: null,
      issues: [],
      presetLimit,
      estimatedLimit: null,
      estimatedThresholds: null,
    };
  }

  const issues: ContextPolicyValidationIssue[] = [];
  const target = parseInteger(draft.target_percent, 5, 90);
  const soft = parseInteger(draft.soft_trigger_percent, 10, 95);
  const hard = parseInteger(draft.hard_trigger_percent, 15, 100);
  const retained = parseInteger(draft.retained_recent_turns, 0, 50);
  const groups = parseInteger(draft.max_history_groups, 16, 4096);
  const reservePercent = parseInteger(draft.safety_reserve_percent, 0, 30);
  const reserveTokens = parseInteger(draft.safety_reserve_tokens, 0, 65_536);

  if (target === null) issues.push({ field: "target_percent", code: "INVALID_TARGET_PERCENT" });
  if (soft === null) issues.push({ field: "soft_trigger_percent", code: "INVALID_SOFT_PERCENT" });
  if (hard === null) issues.push({ field: "hard_trigger_percent", code: "INVALID_HARD_PERCENT" });
  if (target !== null && soft !== null && hard !== null && !(target < soft && soft < hard)) {
    issues.push({ field: "thresholds", code: "INVALID_THRESHOLD_ORDER" });
  }
  if (retained === null) {
    issues.push({ field: "retained_recent_turns", code: "INVALID_RETAINED_TURNS" });
  }
  if (groups === null) issues.push({ field: "max_history_groups", code: "INVALID_HISTORY_GROUPS" });
  if (reservePercent === null) {
    issues.push({ field: "safety_reserve_percent", code: "INVALID_RESERVE_PERCENT" });
  }
  if (reserveTokens === null) {
    issues.push({ field: "safety_reserve_tokens", code: "INVALID_RESERVE_TOKENS" });
  }

  let workspaceLimit: number | undefined;
  if (draft.workspace_input_limit_source === "CUSTOM") {
    const parsed = parseInteger(draft.workspace_input_limit, 1, presetLimit);
    if (parsed === null) {
      issues.push({
        field: "workspace_input_limit",
        code: "INVALID_CUSTOM_LIMIT",
      });
    } else {
      workspaceLimit = parsed;
    }
  }

  if (
    issues.length > 0 ||
    target === null ||
    soft === null ||
    hard === null ||
    retained === null ||
    groups === null ||
    reservePercent === null ||
    reserveTokens === null
  ) {
    return {
      valid: false,
      policy: null,
      issues,
      presetLimit,
      estimatedLimit: null,
      estimatedThresholds: null,
    };
  }

  const estimatedLimit = workspaceLimit ?? presetLimit;
  return {
    valid: true,
    policy: {
      policy_version: CONTEXT_POLICY_VERSION,
      preset: draft.preset,
      workspace_input_limit_source: draft.workspace_input_limit_source,
      ...(workspaceLimit === undefined ? {} : { workspace_input_limit: workspaceLimit }),
      soft_trigger_percent: soft,
      hard_trigger_percent: hard,
      target_percent: target,
      retained_recent_turns: retained,
      max_history_groups: groups,
      safety_reserve_percent: reservePercent,
      safety_reserve_tokens: reserveTokens,
      counter_id: "approx_language_v1",
    },
    issues: [],
    presetLimit,
    estimatedLimit,
    estimatedThresholds: estimateContextThresholds(estimatedLimit, {
      target,
      soft,
      hard,
    }),
  };
}

export function hasContextPolicyDependents(runtimeConfig: RuntimeConfig): boolean {
  return runtimeConfig.history_policy != null || runtimeConfig.summary_policy != null;
}

type BudgetField =
  | "reserved_output_tokens"
  | "max_retrieval_tokens"
  | "max_tool_result_tokens";

const BUDGET_REQUEST_FIELDS: ReadonlyArray<{
  draft: BudgetField;
  runtime: BudgetField;
}> = [
  { draft: "reserved_output_tokens", runtime: "reserved_output_tokens" },
  { draft: "max_retrieval_tokens", runtime: "max_retrieval_tokens" },
  { draft: "max_tool_result_tokens", runtime: "max_tool_result_tokens" },
];

const RUNTIME_LIMIT_FIELDS: ReadonlyArray<keyof RuntimeLimitDraft> = [
  "max_steps",
  "max_tool_calls",
  "max_identical_calls",
  "max_parallel_reads",
  "reserved_output_tokens",
  "max_retrieval_tokens",
  "max_tool_result_tokens",
];

function positiveIntegerOrUndefined(value: string): number | undefined {
  const trimmed = value.trim();
  if (!/^[1-9]\d*$/.test(trimmed)) return undefined;
  const parsed = Number(trimmed);
  return Number.isSafeInteger(parsed) ? parsed : undefined;
}

/**
 * An empty string is an explicit removal. A non-empty string that is not a
 * positive safe integer is an input error and must not mutate a stored limit.
 */
export function invalidRuntimeLimitFields(
  draft: RuntimeLimitDraft,
): Array<keyof RuntimeLimitDraft> {
  return RUNTIME_LIMIT_FIELDS.filter((field) => {
    const value = draft[field].trim();
    return value !== "" && positiveIntegerOrUndefined(value) === undefined;
  });
}

function setOptionalPositiveInteger(
  runtimeConfig: RuntimeConfig,
  field: "max_steps" | "max_tool_calls" | "max_identical_calls" | "max_parallel_reads",
  value: string,
) {
  const parsed = positiveIntegerOrUndefined(value);
  if (parsed === undefined) {
    delete runtimeConfig[field];
  } else {
    runtimeConfig[field] = parsed;
  }
}

/**
 * Patch the runtime map from an editable draft while carrying forward every
 * key this page does not own. That includes future server fields, frozen
 * budget sources, history policy, and summary policy.
 */
export function mergeRuntimeConfigDraft({
  currentRuntime,
  runtimeDraft,
  memoryDraft,
  contextDraft,
}: {
  currentRuntime: RuntimeConfig;
  runtimeDraft: RuntimeLimitDraft;
  memoryDraft: MemoryDraft;
  contextDraft: ContextPolicyDraft;
}): RuntimeConfig {
  const invalidFields = invalidRuntimeLimitFields(runtimeDraft);
  if (invalidFields.length > 0) {
    throw new RangeError(
      `Runtime limits must be blank or positive integers: ${invalidFields.join(", ")}`,
    );
  }
  const memoryValidation = validateMemoryDraft(memoryDraft);
  if (!memoryValidation.valid) {
    throw new RangeError(
      "lexical_overlap_v1 requires long_term_memory to be enabled",
    );
  }

  const next: RuntimeConfig = { ...currentRuntime };
  setOptionalPositiveInteger(next, "max_steps", runtimeDraft.max_steps);
  setOptionalPositiveInteger(next, "max_tool_calls", runtimeDraft.max_tool_calls);
  setOptionalPositiveInteger(next, "max_identical_calls", runtimeDraft.max_identical_calls);
  setOptionalPositiveInteger(next, "max_parallel_reads", runtimeDraft.max_parallel_reads);

  const budget: ContextBudget = { ...(currentRuntime.context_budget ?? {}) };
  const sources: ContextBudgetSources = {
    ...(currentRuntime.context_budget_sources ?? {}),
  };
  for (const field of BUDGET_REQUEST_FIELDS) {
    const parsed = positiveIntegerOrUndefined(runtimeDraft[field.draft]);
    if (parsed === undefined) {
      delete budget[field.runtime];
      sources[field.runtime] = "UNSET";
      continue;
    }
    budget[field.runtime] = parsed;
    const source = sources[field.runtime];
    const currentValue = currentRuntime.context_budget?.[field.runtime];
    if (source === "CUSTOM" || currentValue !== parsed) {
      sources[field.runtime] = "CUSTOM";
    } else if (source === undefined) {
      sources[field.runtime] = "CUSTOM";
    } else {
      sources[field.runtime] = source;
    }
  }
  if (Object.keys(budget).length > 0) {
    next.context_budget = budget;
  } else {
    delete next.context_budget;
  }
  if (Object.keys(sources).length > 0) {
    next.context_budget_sources = sources;
  } else {
    delete next.context_budget_sources;
  }

  if (memoryDraft.thread_history_search || memoryDraft.long_term_memory) {
    const memory: MemoryConfig = {
      ...(currentRuntime.memory ?? {}),
      thread_history_search: memoryDraft.thread_history_search,
      long_term_memory: memoryDraft.long_term_memory,
    };
    if (
      memoryDraft.long_term_memory &&
      memoryDraft.selection_policy === "lexical_overlap_v1"
    ) {
      memory.selection_policy = "lexical_overlap_v1";
    } else {
      delete memory.selection_policy;
    }
    if (!memoryDraft.long_term_memory) {
      delete memory.max_memory_tokens;
      delete memory.max_memory_tokens_source;
    }
    next.memory = memory;
  } else {
    delete next.memory;
  }

  const validation = validateContextPolicyDraft(contextDraft);
  next.context_policy = validation.policy;
  return next;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function recordNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isSafeInteger(value) ? value : null;
}

function recordText(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function isContextPreset(value: unknown): value is ContextPreset {
  return CONTEXT_PRESETS.some((preset) => preset.value === value);
}

function isConfigurationSource(value: unknown): value is ConfigurationSource {
  return value === "UNSET" || value === "DEFAULT" || value === "CUSTOM";
}

function parseFrozenModel(value: unknown): FrozenContextPolicyProjection["models"][number] | null {
  if (!isRecord(value) || !isRecord(value.budget)) return null;
  const profileId = recordText(value.profile_id);
  const credentialRef = recordText(value.credential_ref);
  const provider = recordText(value.provider);
  const model = recordText(value.model);
  const resolvedModel = recordText(value.resolved_model);
  const reservedOutput = recordNumber(value.budget.reserved_output_tokens);
  const applicableLimit = recordNumber(value.budget.applicable_input_limit_tokens);
  const safetyReserve = recordNumber(value.budget.safety_reserve_tokens);
  const availableInput = recordNumber(value.budget.available_input_tokens);
  if (
    profileId === null ||
    credentialRef === null ||
    provider === null ||
    model === null ||
    resolvedModel === null ||
    reservedOutput === null ||
    applicableLimit === null ||
    safetyReserve === null ||
    availableInput === null
  ) {
    return null;
  }
  return {
    profile_id: profileId,
    credential_ref: credentialRef,
    provider,
    model,
    resolved_model: resolvedModel,
    budget: {
      reserved_output_tokens: reservedOutput,
      applicable_input_limit_tokens: applicableLimit,
      safety_reserve_tokens: safetyReserve,
      available_input_tokens: availableInput,
    },
    reasons: Array.isArray(value.reasons)
      ? value.reasons.filter((reason): reason is string => typeof reason === "string")
      : [],
  };
}

export function extractFrozenContextPolicy(
  resolvedSpec: Record<string, unknown>,
): FrozenContextPolicyProjection | null {
  const runtime = isRecord(resolvedSpec.runtime) ? resolvedSpec.runtime : null;
  const policy = runtime && isRecord(runtime.context_policy) ? runtime.context_policy : null;
  if (!policy) return null;
  const preset = policy.preset;
  const source = policy.workspace_input_limit_source;
  const policyVersion = policy.policy_version;
  const workspaceLimit = recordNumber(policy.workspace_input_limit);
  const inputLimit = recordNumber(policy.input_limit_tokens);
  const modelAvailable = recordNumber(policy.model_available_input_tokens);
  const minTotalValue = policy.min_total_context_tokens;
  const minTotal = minTotalValue === null ? null : recordNumber(minTotalValue);
  const reservedOutput = recordNumber(policy.reserved_output_tokens);
  const thresholds = isRecord(policy.thresholds) ? policy.thresholds : null;
  const target = thresholds ? recordNumber(thresholds.target) : null;
  const soft = thresholds ? recordNumber(thresholds.soft) : null;
  const hard = thresholds ? recordNumber(thresholds.hard) : null;
  const counter = isRecord(policy.counter) ? policy.counter : null;
  const counterId = counter ? recordText(counter.id) : null;
  const counterVersion = counter ? recordNumber(counter.version) : null;
  const counterPrecision = counter ? recordText(counter.precision) : null;
  const models = Array.isArray(policy.models)
    ? policy.models.map(parseFrozenModel).filter((model): model is NonNullable<typeof model> => model !== null)
    : [];
  if (
    policyVersion !== CONTEXT_POLICY_VERSION ||
    !isContextPreset(preset) ||
    !isConfigurationSource(source) ||
    workspaceLimit === null ||
    inputLimit === null ||
    modelAvailable === null ||
    (minTotalValue !== null && minTotal === null) ||
    reservedOutput === null ||
    target === null ||
    soft === null ||
    hard === null ||
    counterId === null ||
    counterVersion === null ||
    counterPrecision === null ||
    models.length === 0
  ) {
    return null;
  }
  return {
    policy_version: CONTEXT_POLICY_VERSION,
    preset,
    workspace_input_limit: workspaceLimit,
    workspace_input_limit_source: source,
    input_limit_tokens: inputLimit,
    model_available_input_tokens: modelAvailable,
    min_total_context_tokens: minTotal,
    reserved_output_tokens: reservedOutput,
    thresholds: { soft, hard, target },
    counter: {
      id: counterId,
      version: counterVersion,
      precision: counterPrecision,
    },
    models,
    limitations: Array.isArray(policy.limitations)
      ? policy.limitations.filter(isRecord)
      : [],
    unknowns: Array.isArray(policy.unknowns)
      ? policy.unknowns.filter((item): item is string => typeof item === "string")
      : [],
    ...(recordNumber(policy.retained_recent_turns) === null
      ? {}
      : { retained_recent_turns: recordNumber(policy.retained_recent_turns) as number }),
    ...(recordNumber(policy.max_history_groups) === null
      ? {}
      : { max_history_groups: recordNumber(policy.max_history_groups) as number }),
    ...(recordNumber(policy.safety_reserve_percent) === null
      ? {}
      : { safety_reserve_percent: recordNumber(policy.safety_reserve_percent) as number }),
    ...(recordNumber(policy.safety_reserve_tokens) === null
      ? {}
      : { safety_reserve_tokens: recordNumber(policy.safety_reserve_tokens) as number }),
    ...(recordNumber(policy.soft_trigger_percent) === null
      ? {}
      : { soft_trigger_percent: recordNumber(policy.soft_trigger_percent) as number }),
    ...(recordNumber(policy.hard_trigger_percent) === null
      ? {}
      : { hard_trigger_percent: recordNumber(policy.hard_trigger_percent) as number }),
    ...(recordNumber(policy.target_percent) === null
      ? {}
      : { target_percent: recordNumber(policy.target_percent) as number }),
  };
}
