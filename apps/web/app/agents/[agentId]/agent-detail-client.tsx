"use client";

import Link from "next/link";
import { useCallback, useEffect, useState, type FormEvent } from "react";

import Breadcrumbs from "@/components/layout/breadcrumbs";
import { EmptyState, ErrorState, InlineError, LoadingState, Panel, SessionRequired } from "@/components/ui/states";
import TechnicalDetails from "@/components/ui/technical-details";
import { useFrontendSession } from "@/components/providers/session-provider";
import { useWorkspaceData, useWorkspaceMutation } from "@/hooks/use-workspace-data";
import { errorHintKey, type AuthInput } from "@/lib/api/client";
import {
  getAgent,
  getAgentKnowledgeBindings,
  getAgentToolBindings,
  listAgentVersions,
  patchAgent,
  preflightAgent,
  publishAgent,
  putAgentKnowledgeBindings,
  putAgentToolBindings,
  type Agent,
  type AgentPreflightResult,
  type AgentKnowledgeBinding,
  type AgentToolBinding,
  type AgentVersion,
  type ContextBudget,
  type KnowledgeBindingMode,
  type RuntimeConfig,
} from "@/lib/api/agents";
import { listKnowledgeBases, listSnapshots, type KnowledgeBase, type KnowledgeSnapshot } from "@/lib/api/knowledge";
import { listModelProfiles, type ModelProfile } from "@/lib/api/models";
import { listToolRevisions, listTools, type Tool, type ToolRevision } from "@/lib/api/tools";
import { useI18n } from "@/i18n/provider";

type Tab = "general" | "model" | "knowledge" | "tools" | "runtime" | "versions";

const TABS: Tab[] = ["general", "model", "knowledge", "tools", "runtime", "versions"];

const TAB_LABEL: Record<Tab, "agents.tab.general" | "agents.tab.model" | "agents.tab.knowledge" | "agents.tab.tools" | "agents.tab.runtime" | "agents.tab.versions"> = {
  general: "agents.tab.general",
  model: "agents.tab.model",
  knowledge: "agents.tab.knowledge",
  tools: "agents.tab.tools",
  runtime: "agents.tab.runtime",
  versions: "agents.tab.versions",
};

/**
 * A blank runtime field is an absent field, never a null: the backend
 * merges the submitted map over its defaults, so an explicit null would
 * overwrite a default and then fail validation.
 *
 * Every runtime limit and every context-budget value must be a positive
 * integer — zero is rejected by the backend, so it is not sent.
 */
function positiveIntegerOrUndefined(value: string): number | undefined {
  const trimmed = value.trim();
  if (!trimmed) return undefined;
  const parsed = Number(trimmed);
  return Number.isInteger(parsed) && parsed >= 1 ? parsed : undefined;
}

/** Blank is valid; anything else must parse as a positive integer. */
function validRuntimeValue(value: string): boolean {
  const trimmed = value.trim();
  if (!trimmed) return true;
  return positiveIntegerOrUndefined(trimmed) !== undefined;
}

/** Shown wherever the resolved spec does not carry a field. */
const MISSING = "—";

/**
 * The publish preview is read out of the server's resolved spec and
 * nothing else — never out of the current drafts, selects or binding
 * rows, which may already have moved on. Shapes are read defensively:
 * an unexpected shape renders as missing rather than as a guess.
 */
function specObject(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function specText(value: unknown): string | null {
  return typeof value === "string" && value.trim() !== "" ? value : null;
}

function specCount(value: unknown): number | null {
  return Array.isArray(value) ? value.length : null;
}

export default function AgentDetailClient({ agentId }: { agentId: string }) {
  const { t, formatDateTime } = useI18n();
  const { connected, sessionId, workspaceId } = useFrontendSession();

  const scope = `${workspaceId}:${agentId}`;
  const [tab, setTab] = useState<Tab>("general");
  const [notice, setNotice] = useState<string | null>(null);
  /**
   * A READY preflight, valid only for this session generation, this
   * workspace, this agent and the draft as it stood when the check ran.
   * It gates the confirmation panel; it is never a publish permit.
   */
  const [preflight, setPreflight] = useState<AgentPreflightResult | null>(null);
  /** True once a draft write has dropped a preview the user had open. */
  const [preflightStale, setPreflightStale] = useState(false);
  const [publishResult, setPublishResult] = useState<{ version: number; hash: string } | null>(null);

  const loadAgent = useCallback((auth: AuthInput) => getAgent(auth, agentId), [agentId]);
  const loadVersions = useCallback((auth: AuthInput) => listAgentVersions(auth, agentId), [agentId]);
  const loadProfiles = useCallback((auth: AuthInput) => listModelProfiles(auth), []);
  const loadBases = useCallback((auth: AuthInput) => listKnowledgeBases(auth), []);
  const loadTools = useCallback((auth: AuthInput) => listTools(auth), []);
  const loadKnowledgeBindings = useCallback(
    (auth: AuthInput) => getAgentKnowledgeBindings(auth, agentId),
    [agentId],
  );
  const loadToolBindings = useCallback((auth: AuthInput) => getAgentToolBindings(auth, agentId), [agentId]);

  const agent = useWorkspaceData<Agent>(loadAgent, `agent:${scope}`);
  const versions = useWorkspaceData<AgentVersion[]>(loadVersions, `agent-versions:${scope}`);
  const profiles = useWorkspaceData<ModelProfile[]>(loadProfiles, `profiles:${workspaceId}`);
  const bases = useWorkspaceData<KnowledgeBase[]>(loadBases, `knowledge-bases:${workspaceId}`);
  const tools = useWorkspaceData<Tool[]>(loadTools, `tools:${workspaceId}`);
  const knowledgeBindings = useWorkspaceData<AgentKnowledgeBinding[]>(
    loadKnowledgeBindings,
    `agent-knowledge-bindings:${scope}`,
  );
  const toolBindings = useWorkspaceData<AgentToolBinding[]>(
    loadToolBindings,
    `agent-tool-bindings:${scope}`,
  );

  const agentMutation = useWorkspaceMutation(`agent:${scope}`);
  const knowledgeMutation = useWorkspaceMutation(`agent-knowledge-bindings:${scope}`);
  const toolMutation = useWorkspaceMutation(`agent-tool-bindings:${scope}`);
  const preflightMutation = useWorkspaceMutation(`agent-preflight:${scope}`);
  const publishMutation = useWorkspaceMutation(`agent-publish:${scope}`);

  // ---- Editable drafts, hydrated from the server projection ----
  const [general, setGeneral] = useState({ name: "", description: "", system_prompt: "" });
  const [model, setModel] = useState({ model_profile_id: "", max_attempts: "" });
  const [bindingMode, setBindingMode] = useState<KnowledgeBindingMode>("LATEST");
  const [runtime, setRuntime] = useState({
    max_steps: "",
    max_tool_calls: "",
    max_identical_calls: "",
    max_parallel_reads: "",
    reserved_output_tokens: "",
    max_retrieval_tokens: "",
    max_tool_result_tokens: "",
  });
  // Booleans, so they live outside `runtime`: that state is all numeric
  // strings and is validated as such.
  const [memory, setMemory] = useState({ thread_history_search: false, long_term_memory: false });
  const [knowledgeDraft, setKnowledgeDraft] = useState<AgentKnowledgeBinding[]>([]);
  const [toolDraft, setToolDraft] = useState<AgentToolBinding[]>([]);

  // A session change or a different agent invalidates every local draft.
  useEffect(() => {
    setTab("general");
    setNotice(null);
    setPreflight(null);
    setPreflightStale(false);
    setPublishResult(null);
    setGeneral({ name: "", description: "", system_prompt: "" });
    setModel({ model_profile_id: "", max_attempts: "" });
    setBindingMode("LATEST");
    setRuntime({
      max_steps: "",
      max_tool_calls: "",
      max_identical_calls: "",
      max_parallel_reads: "",
      reserved_output_tokens: "",
      max_retrieval_tokens: "",
      max_tool_result_tokens: "",
    });
    setMemory({ thread_history_search: false, long_term_memory: false });
    setKnowledgeDraft([]);
    setToolDraft([]);
  }, [sessionId, agentId]);

  const loadedAgent = agent.data;
  useEffect(() => {
    if (!loadedAgent) return;
    setGeneral({
      name: loadedAgent.name,
      description: loadedAgent.description ?? "",
      system_prompt: loadedAgent.system_prompt,
    });
    setModel({
      model_profile_id: loadedAgent.model_profile_id,
      max_attempts:
        loadedAgent.model_retry_policy?.max_attempts != null
          ? String(loadedAgent.model_retry_policy.max_attempts)
          : "",
    });
    setBindingMode((loadedAgent.knowledge_binding_mode as KnowledgeBindingMode) ?? "LATEST");
    const runtimeConfig = loadedAgent.runtime_config ?? {};
    const budget = runtimeConfig.context_budget ?? {};
    setRuntime({
      max_steps: runtimeConfig.max_steps != null ? String(runtimeConfig.max_steps) : "",
      max_tool_calls: runtimeConfig.max_tool_calls != null ? String(runtimeConfig.max_tool_calls) : "",
      max_identical_calls:
        runtimeConfig.max_identical_calls != null ? String(runtimeConfig.max_identical_calls) : "",
      max_parallel_reads:
        runtimeConfig.max_parallel_reads != null ? String(runtimeConfig.max_parallel_reads) : "",
      reserved_output_tokens:
        budget.reserved_output_tokens != null ? String(budget.reserved_output_tokens) : "",
      max_retrieval_tokens: budget.max_retrieval_tokens != null ? String(budget.max_retrieval_tokens) : "",
      max_tool_result_tokens:
        budget.max_tool_result_tokens != null ? String(budget.max_tool_result_tokens) : "",
    });
    // Absent means off, which is what every agent published before memory
    // existed meant.
    const storedMemory = runtimeConfig.memory ?? {};
    setMemory({
      thread_history_search: storedMemory.thread_history_search === true,
      long_term_memory: storedMemory.long_term_memory === true,
    });
  }, [loadedAgent]);

  const loadedKnowledgeBindings = knowledgeBindings.data;
  useEffect(() => {
    if (loadedKnowledgeBindings) setKnowledgeDraft(loadedKnowledgeBindings);
  }, [loadedKnowledgeBindings]);

  const loadedToolBindings = toolBindings.data;
  useEffect(() => {
    if (loadedToolBindings) setToolDraft(loadedToolBindings);
  }, [loadedToolBindings]);

  if (!connected) {
    return (
      <div className="page">
        <header className="page-header">
          <p className="eyebrow">{t("agents.eyebrow")}</p>
          <h1>{t("agents.detail")}</h1>
        </header>
        <SessionRequired contextKey="session.context.agents" />
      </div>
    );
  }

  const profileList = profiles.data ?? [];
  const baseList = bases.data ?? [];
  const toolList = tools.data ?? [];
  const versionList = versions.data ?? [];
  const runtimeValuesValid = Object.values(runtime).every((value) => validRuntimeValue(value));

  // Preview figures come from the server's resolved spec, never from the
  // drafts above; a field the spec does not carry is shown as missing.
  const preflightSpec = preflight ? specObject(preflight.resolved_spec) : null;
  const preflightModel = specObject(preflightSpec?.model);
  const preflightProvider = specText(preflightModel?.provider);
  const preflightModelName = specText(preflightModel?.model);
  const preflightKnowledgeCount = specCount(specObject(preflightSpec?.retrieval)?.knowledge_bindings);
  const preflightToolCount = specCount(preflightSpec?.tools);
  // Only an explicit declaration counts; the profile is read as-is.
  const selectedProfileSupportsTools =
    profileList.find((profile) => profile.id === model.model_profile_id)?.capabilities?.tool_calling === true;

  /**
   * Any draft write makes an earlier readiness check describe a draft
   * that no longer exists, so the preview and its confirmation go away.
   * The next publish attempt re-runs the check; nothing re-runs silently.
   */
  function invalidatePreflight() {
    setPreflightStale((stale) => stale || preflight !== null);
    setPreflight(null);
    preflightMutation.clearError();
  }

  async function saveGeneral(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setNotice(null);
    invalidatePreflight();
    const result = await agentMutation.run((auth) =>
      patchAgent(auth, agentId, {
        name: general.name.trim(),
        description: general.description.trim() || null,
        system_prompt: general.system_prompt,
      }),
    );
    if (result) {
      setNotice(t("agents.saved"));
      agent.reload();
    }
  }

  async function saveModel(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setNotice(null);
    invalidatePreflight();
    const result = await agentMutation.run((auth) =>
      patchAgent(auth, agentId, {
        model_profile_id: model.model_profile_id,
        ...(model.max_attempts ? { model_retry_policy: { max_attempts: Number(model.max_attempts) } } : {}),
      }),
    );
    if (result) {
      setNotice(t("agents.saved"));
      agent.reload();
    }
  }

  async function saveRuntime(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setNotice(null);
    invalidatePreflight();
    const runtimeConfig: RuntimeConfig = {};
    const maxSteps = positiveIntegerOrUndefined(runtime.max_steps);
    const maxToolCalls = positiveIntegerOrUndefined(runtime.max_tool_calls);
    const maxIdenticalCalls = positiveIntegerOrUndefined(runtime.max_identical_calls);
    const maxParallelReads = positiveIntegerOrUndefined(runtime.max_parallel_reads);
    if (maxSteps !== undefined) runtimeConfig.max_steps = maxSteps;
    if (maxToolCalls !== undefined) runtimeConfig.max_tool_calls = maxToolCalls;
    if (maxIdenticalCalls !== undefined) runtimeConfig.max_identical_calls = maxIdenticalCalls;
    if (maxParallelReads !== undefined) runtimeConfig.max_parallel_reads = maxParallelReads;

    const budget: ContextBudget = {};
    const reservedOutput = positiveIntegerOrUndefined(runtime.reserved_output_tokens);
    const maxRetrieval = positiveIntegerOrUndefined(runtime.max_retrieval_tokens);
    const maxToolResult = positiveIntegerOrUndefined(runtime.max_tool_result_tokens);
    if (reservedOutput !== undefined) budget.reserved_output_tokens = reservedOutput;
    if (maxRetrieval !== undefined) budget.max_retrieval_tokens = maxRetrieval;
    if (maxToolResult !== undefined) budget.max_tool_result_tokens = maxToolResult;
    // An empty budget is not sent at all, and neither is an empty runtime.
    if (Object.keys(budget).length > 0) runtimeConfig.context_budget = budget;
    const memoryRequested = memory.thread_history_search || memory.long_term_memory;
    if (Object.keys(runtimeConfig).length === 0 && !memoryRequested) {
      setNotice(t("agents.runtimeEmpty"));
      return;
    }
    // Always sent, never omitted: this form replaces the runtime config
    // wholesale, so omitting the block is how a switch gets turned off.
    runtimeConfig.memory = { ...memory };

    const result = await agentMutation.run((auth) => patchAgent(auth, agentId, { runtime_config: runtimeConfig }));
    if (result) {
      setNotice(t("agents.saved"));
      agent.reload();
    }
  }

  async function saveKnowledgeBindings() {
    setNotice(null);
    invalidatePreflight();
    const draft = knowledgeDraft.filter((binding) => binding.knowledge_base_id);
    // The agent-level default mode travels with the agent, the per-base
    // modes travel with the collection; both are replaced wholesale.
    const modeResult = await agentMutation.run((auth) =>
      patchAgent(auth, agentId, { knowledge_binding_mode: bindingMode }),
    );
    if (!modeResult) return;
    const result = await knowledgeMutation.run((auth) => putAgentKnowledgeBindings(auth, agentId, draft));
    if (result) {
      setNotice(t("agents.bindingsSaved"));
      knowledgeBindings.reload();
      agent.reload();
    }
  }

  async function saveToolBindings() {
    setNotice(null);
    invalidatePreflight();
    const draft = toolDraft.filter((binding) => binding.tool_id);
    const result = await toolMutation.run((auth) => putAgentToolBindings(auth, agentId, draft));
    if (result) {
      setNotice(t("agents.bindingsSaved"));
      toolBindings.reload();
    }
  }

  /**
   * Publishing starts with a server-side readiness check. Nothing is
   * written and no confirmation panel opens until it comes back READY;
   * a failed check reports the backend's error code and stops here.
   */
  async function startPreflight() {
    setNotice(null);
    setPublishResult(null);
    setPreflight(null);
    setPreflightStale(false);
    publishMutation.clearError();
    const result = await preflightMutation.run((auth) => preflightAgent(auth, agentId));
    if (result) setPreflight(result);
  }

  async function runPublish() {
    setNotice(null);
    // Publish goes through the publish endpoint, always. The preflight
    // spec is a preview: it is never turned into a version client-side.
    const result = await publishMutation.run((auth) => publishAgent(auth, agentId));
    if (result) {
      setPreflight(null);
      setPublishResult({ version: result.version_number, hash: result.resolved_spec_hash });
      versions.reload();
    }
  }

  return (
    <div className="page">
      <Breadcrumbs
        items={[
          { label: t("nav.agents"), href: "/agents" },
          { label: loadedAgent?.name ?? t("agents.detail") },
        ]}
      />
      <header className="page-header">
        <p className="eyebrow">{t("agents.eyebrow")}</p>
        <h1>{loadedAgent?.name ?? t("agents.detail")}</h1>
        <p className="page-lede">
          <code>{agentId}</code>
        </p>
      </header>

      <div className="page-toolbar">
        {/* The Playground only runs published versions, never the draft. */}
        {versionList.length > 0 ? (
          <Link className="button button-ghost" href={`/agents/${agentId}/playground`}>
            {t("agents.playground.open")}
          </Link>
        ) : (
          <button type="button" className="button button-ghost" disabled>
            {t("agents.playground.open")}
          </button>
        )}
        <button
          type="button"
          className="button button-primary"
          onClick={() => void startPreflight()}
          disabled={preflightMutation.pending || publishMutation.pending || !loadedAgent}
        >
          {preflightMutation.pending ? t("agents.checkingReadiness") : t("agents.publish")}
        </button>
      </div>

      {notice && <p className="inline-notice">{notice}</p>}
      {publishResult && (
        <p className="inline-notice">
          {t("agents.publishedVersion", { version: publishResult.version })}{" "}
          <code className="hash-value">{publishResult.hash}</code>
        </p>
      )}
      <InlineError error={publishMutation.error} fallback={t("errors.requestFailed")} />

      {preflightStale && !preflight && !preflightMutation.pending && (
        <p className="state-hint">{t("agents.preflightStale")}</p>
      )}
      {preflightMutation.pending && <p className="state-hint">{t("agents.checkingReadiness")}</p>}
      <InlineError error={preflightMutation.error} fallback={t("errors.requestFailed")} />

      {preflight && (
        <Panel
          ariaLabel={t("agents.publishPreview")}
          eyebrow={t("agents.readinessCheck")}
          title={t("agents.publishPreview")}
        >
          <p className="inline-notice">{t("agents.readyToPublish")}</p>
          <dl className="key-values">
            <div className="key-value-row">
              <dt>{t("agents.schemaVersion")}</dt>
              <dd>{preflight.spec_schema_version}</dd>
            </div>
            <div className="key-value-row">
              <dt>{t("agents.specHash")}</dt>
              <dd>
                <code className="hash-value">{preflight.resolved_spec_hash}</code>
              </dd>
            </div>
            <div className="key-value-row">
              <dt>{t("agents.draftCheckedAt")}</dt>
              <dd>{formatDateTime(preflight.draft_updated_at)}</dd>
            </div>
            <div className="key-value-row">
              <dt>{t("agents.tab.model")}</dt>
              <dd>
                {preflightProvider || preflightModelName ? (
                  <code>{`${preflightProvider ?? MISSING} / ${preflightModelName ?? MISSING}`}</code>
                ) : (
                  MISSING
                )}
              </dd>
            </div>
            <div className="key-value-row">
              <dt>{t("agents.tab.knowledge")}</dt>
              <dd>
                {preflightKnowledgeCount === null
                  ? MISSING
                  : t("agents.knowledgeBindingCount", { count: preflightKnowledgeCount })}
              </dd>
            </div>
            <div className="key-value-row">
              <dt>{t("agents.tab.tools")}</dt>
              <dd>
                {preflightToolCount === null
                  ? MISSING
                  : t("agents.toolBindingCount", { count: preflightToolCount })}
              </dd>
            </div>
          </dl>
          <TechnicalDetails summary={t("agents.resolvedSpec")} value={preflight.resolved_spec} />
          <p className="state-hint">{t("agents.preflightNotGuarantee")}</p>
          <p className="state-hint">{t("agents.publishConfirm")}</p>
          <div className="form-actions">
            <button
              type="button"
              className="button button-primary"
              onClick={() => void runPublish()}
              disabled={publishMutation.pending}
            >
              {t("agents.publishNow")}
            </button>
            <button type="button" className="button button-ghost" onClick={() => setPreflight(null)}>
              {t("session.cancel")}
            </button>
          </div>
        </Panel>
      )}

      {agent.error && (
        <ErrorState
          code={agent.error.code}
          message={agent.error.message || t("errors.loadAgent")}
          hint={errorHintKey(agent.error) ? t(errorHintKey(agent.error)!) : undefined}
          onRetry={agent.reload}
        />
      )}
      {agent.loading && !agent.error && <LoadingState />}

      <div className="tab-strip" role="tablist" aria-label={t("agents.sections")}>
        {TABS.map((item) => (
          <button
            key={item}
            type="button"
            role="tab"
            aria-selected={tab === item}
            className={tab === item ? "button button-primary" : "button button-ghost"}
            onClick={() => setTab(item)}
          >
            {t(TAB_LABEL[item])}
          </button>
        ))}
      </div>

      <InlineError error={agentMutation.error} fallback={t("errors.requestFailed")} />

      {tab === "general" && loadedAgent && (
        <Panel ariaLabel={t("agents.tab.general")} title={t("agents.tab.general")}>
          <form className="eval-form" onSubmit={saveGeneral} noValidate>
            <div className="form-grid">
              <label>
                {t("agents.name")}
                <input
                  value={general.name}
                  onChange={(event) => setGeneral((current) => ({ ...current, name: event.target.value }))}
                  maxLength={200}
                />
              </label>
              <label>
                {t("agents.description")}
                <input
                  value={general.description}
                  onChange={(event) =>
                    setGeneral((current) => ({ ...current, description: event.target.value }))
                  }
                  maxLength={500}
                />
              </label>
            </div>
            <label>
              {t("agents.systemPrompt")}
              <textarea
                rows={10}
                value={general.system_prompt}
                onChange={(event) =>
                  setGeneral((current) => ({ ...current, system_prompt: event.target.value }))
                }
              />
            </label>
            <dl className="key-values">
              <div className="key-value-row">
                <dt>{t("agents.promptVersion")}</dt>
                <dd>v{loadedAgent.prompt_version}</dd>
              </div>
              <div className="key-value-row">
                <dt>{t("common.updated")}</dt>
                <dd>{formatDateTime(loadedAgent.updated_at)}</dd>
              </div>
            </dl>
            <div className="form-actions">
              <button type="submit" className="button button-primary" disabled={agentMutation.pending}>
                {t("common.save")}
              </button>
            </div>
          </form>
        </Panel>
      )}

      {tab === "model" && loadedAgent && (
        <Panel ariaLabel={t("agents.tab.model")} title={t("agents.tab.model")}>
          <form className="eval-form" onSubmit={saveModel} noValidate>
            <div className="form-grid">
              <label>
                {t("agents.modelProfile")}
                <select
                  value={model.model_profile_id}
                  onChange={(event) =>
                    setModel((current) => ({ ...current, model_profile_id: event.target.value }))
                  }
                >
                  <option value="">{t("agents.selectProfile")}</option>
                  {profileList.map((profile) => (
                    <option value={profile.id} key={profile.id}>
                      {profile.model}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                {t("agents.maxAttempts")}
                <input
                  type="number"
                  min="1"
                  value={model.max_attempts}
                  onChange={(event) =>
                    setModel((current) => ({ ...current, max_attempts: event.target.value }))
                  }
                />
              </label>
            </div>
            <div className="form-actions">
              <button
                type="submit"
                className="button button-primary"
                disabled={agentMutation.pending || !model.model_profile_id}
              >
                {t("common.save")}
              </button>
            </div>
          </form>
        </Panel>
      )}

      {tab === "knowledge" && (
        <Panel ariaLabel={t("agents.tab.knowledge")} title={t("agents.tab.knowledge")}>
          <p className="state-hint">{t("agents.knowledgeHint")}</p>

          {knowledgeBindings.error && (
            <ErrorState
              code={knowledgeBindings.error.code}
              message={knowledgeBindings.error.message || t("errors.loadBindings")}
              hint={
                errorHintKey(knowledgeBindings.error) ? t(errorHintKey(knowledgeBindings.error)!) : undefined
              }
              onRetry={knowledgeBindings.reload}
            />
          )}
          {knowledgeBindings.loading && !knowledgeBindings.error && <LoadingState />}

          <label>
            {t("agents.defaultBindingMode")}
            <select
              value={bindingMode}
              onChange={(event) => setBindingMode(event.target.value as KnowledgeBindingMode)}
            >
              <option value="LATEST">{t("agents.bindingModeLatest")}</option>
              <option value="PINNED">{t("agents.bindingModePinned")}</option>
            </select>
          </label>

          <div className="binding-rows">
            {knowledgeDraft.map((binding, index) => (
              <KnowledgeBindingRow
                key={`${binding.knowledge_base_id}-${index}`}
                binding={binding}
                bases={baseList}
                onChange={(next) =>
                  setKnowledgeDraft((current) =>
                    current.map((item, position) => (position === index ? next : item)),
                  )
                }
                onRemove={() =>
                  setKnowledgeDraft((current) => current.filter((_, position) => position !== index))
                }
              />
            ))}
          </div>

          {knowledgeBindings.loaded && knowledgeDraft.length === 0 && (
            <EmptyState title={t("agents.noKnowledgeBindings")} />
          )}

          <InlineError error={knowledgeMutation.error} fallback={t("errors.requestFailed")} />

          <div className="form-actions">
            <button
              type="button"
              className="button button-ghost"
              onClick={() =>
                setKnowledgeDraft((current) => [
                  ...current,
                  { knowledge_base_id: "", binding_mode: "LATEST", snapshot_id: null },
                ])
              }
              disabled={baseList.length === 0}
            >
              {t("agents.addBinding")}
            </button>
            <button
              type="button"
              className="button button-primary"
              onClick={() => void saveKnowledgeBindings()}
              disabled={knowledgeMutation.pending || agentMutation.pending}
            >
              {t("agents.saveBindings")}
            </button>
          </div>
        </Panel>
      )}

      {tab === "tools" && (
        <Panel ariaLabel={t("agents.tab.tools")} title={t("agents.tab.tools")}>
          <p className="state-hint">{t("agents.toolsHint")}</p>
          {/* Advisory only — the backend stays the authority on publish. */}
          {toolDraft.length > 0 && !selectedProfileSupportsTools && (
            <p className="inline-notice">{t("agents.toolCallingWarning")}</p>
          )}

          {toolBindings.error && (
            <ErrorState
              code={toolBindings.error.code}
              message={toolBindings.error.message || t("errors.loadBindings")}
              hint={errorHintKey(toolBindings.error) ? t(errorHintKey(toolBindings.error)!) : undefined}
              onRetry={toolBindings.reload}
            />
          )}
          {toolBindings.loading && !toolBindings.error && <LoadingState />}

          <div className="binding-rows">
            {toolDraft.map((binding, index) => (
              <ToolBindingRow
                key={`${binding.tool_id}-${index}`}
                binding={binding}
                tools={toolList}
                onChange={(next) =>
                  setToolDraft((current) =>
                    current.map((item, position) => (position === index ? next : item)),
                  )
                }
                onRemove={() =>
                  setToolDraft((current) => current.filter((_, position) => position !== index))
                }
              />
            ))}
          </div>

          {toolBindings.loaded && toolDraft.length === 0 && <EmptyState title={t("agents.noToolBindings")} />}

          <InlineError error={toolMutation.error} fallback={t("errors.requestFailed")} />

          <div className="form-actions">
            <button
              type="button"
              className="button button-ghost"
              onClick={() => setToolDraft((current) => [...current, { tool_id: "", tool_revision_id: null }])}
              disabled={toolList.length === 0}
            >
              {t("agents.addBinding")}
            </button>
            <button
              type="button"
              className="button button-primary"
              onClick={() => void saveToolBindings()}
              disabled={toolMutation.pending}
            >
              {t("agents.saveBindings")}
            </button>
          </div>
        </Panel>
      )}

      {tab === "runtime" && loadedAgent && (
        <Panel ariaLabel={t("agents.tab.runtime")} title={t("agents.tab.runtime")}>
          <p className="state-hint">{t("agents.runtimeHint")}</p>
          <form className="eval-form" onSubmit={saveRuntime} noValidate>
            <div className="form-grid">
              <label>
                {t("agents.maxSteps")}
                <input
                  type="number"
                  min="1"
                  value={runtime.max_steps}
                  onChange={(event) =>
                    setRuntime((current) => ({ ...current, max_steps: event.target.value }))
                  }
                />
              </label>
              <label>
                {t("agents.maxToolCalls")}
                <input
                  type="number"
                  min="1"
                  value={runtime.max_tool_calls}
                  onChange={(event) =>
                    setRuntime((current) => ({ ...current, max_tool_calls: event.target.value }))
                  }
                />
              </label>
              <label>
                {t("agents.maxIdenticalCalls")}
                <input
                  type="number"
                  min="1"
                  value={runtime.max_identical_calls}
                  onChange={(event) =>
                    setRuntime((current) => ({ ...current, max_identical_calls: event.target.value }))
                  }
                />
              </label>
              <label>
                {t("agents.maxParallelReads")}
                <input
                  type="number"
                  min="1"
                  value={runtime.max_parallel_reads}
                  onChange={(event) =>
                    setRuntime((current) => ({ ...current, max_parallel_reads: event.target.value }))
                  }
                />
              </label>
              <label>
                {t("agents.reservedOutputTokens")}
                <input
                  type="number"
                  min="1"
                  value={runtime.reserved_output_tokens}
                  onChange={(event) =>
                    setRuntime((current) => ({ ...current, reserved_output_tokens: event.target.value }))
                  }
                />
              </label>
              <label>
                {t("agents.maxRetrievalTokens")}
                <input
                  type="number"
                  min="1"
                  value={runtime.max_retrieval_tokens}
                  onChange={(event) =>
                    setRuntime((current) => ({ ...current, max_retrieval_tokens: event.target.value }))
                  }
                />
              </label>
              <label>
                {t("agents.maxToolResultTokens")}
                <input
                  type="number"
                  min="1"
                  value={runtime.max_tool_result_tokens}
                  onChange={(event) =>
                    setRuntime((current) => ({ ...current, max_tool_result_tokens: event.target.value }))
                  }
                />
              </label>
            </div>
            <p className="state-hint">{t("agents.runtimeOmitHint")}</p>
            <h3>{t("agents.memoryTitle")}</h3>
            <p className="state-hint">{t("agents.memoryHint")}</p>
            <div className="form-grid">
              <label className="checkbox-label">
                <input
                  type="checkbox"
                  checked={memory.thread_history_search}
                  onChange={(event) =>
                    setMemory((current) => ({
                      ...current,
                      thread_history_search: event.target.checked,
                    }))
                  }
                />
                {t("agents.memoryThreadSearch")}
              </label>
              <label className="checkbox-label">
                <input
                  type="checkbox"
                  checked={memory.long_term_memory}
                  onChange={(event) =>
                    setMemory((current) => ({ ...current, long_term_memory: event.target.checked }))
                  }
                />
                {t("agents.memoryLongTerm")}
              </label>
            </div>
            <p className="state-hint">{t("agents.memoryThreadSearchHint")}</p>
            {!runtimeValuesValid && (
              <p className="state-hint">{t("agents.runtimeInvalid")}</p>
            )}
            <div className="form-actions">
              <button
                type="submit"
                className="button button-primary"
                disabled={agentMutation.pending || !runtimeValuesValid}
              >
                {t("common.save")}
              </button>
            </div>
          </form>
        </Panel>
      )}

      {tab === "versions" && (
        <Panel ariaLabel={t("agents.tab.versions")} title={t("agents.tab.versions")}>
          {versions.error && (
            <ErrorState
              code={versions.error.code}
              message={versions.error.message || t("errors.loadVersions")}
              hint={errorHintKey(versions.error) ? t(errorHintKey(versions.error)!) : undefined}
              onRetry={versions.reload}
            />
          )}
          {versions.loading && !versions.error && <LoadingState />}
          {versions.loaded && !versions.error && versionList.length === 0 && (
            <EmptyState title={t("agents.noVersions")} hint={t("agents.noVersionsHint")} />
          )}

          {versionList.length > 0 && (
            <div className="data-table">
              <table>
                <thead>
                  <tr>
                    <th scope="col">{t("agents.version")}</th>
                    <th scope="col">{t("agents.specHash")}</th>
                    <th scope="col">{t("agents.schemaVersion")}</th>
                    <th scope="col">{t("common.created")}</th>
                    <th scope="col">{t("agents.playground.run")}</th>
                  </tr>
                </thead>
                <tbody>
                  {versionList.map((version) => (
                    <tr key={version.id}>
                      <td data-label={t("agents.version")}>
                        <Link href={`/agents/${agentId}/versions/${version.id}`}>
                          v{version.version_number}
                        </Link>
                      </td>
                      <td data-label={t("agents.specHash")}>
                        <code className="hash-value">{version.resolved_spec_hash}</code>
                      </td>
                      <td data-label={t("agents.schemaVersion")}>{version.spec_schema_version}</td>
                      <td data-label={t("common.created")}>{formatDateTime(version.created_at)}</td>
                      <td data-label={t("agents.playground.run")}>
                        <Link href={`/agents/${agentId}/playground?version=${encodeURIComponent(version.id)}`}>
                          {t("agents.playground.run")}
                        </Link>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>
      )}
    </div>
  );
}

/**
 * One knowledge binding. Snapshots are only offered — and only ever
 * submitted — while the row is PINNED.
 */
function KnowledgeBindingRow({
  binding,
  bases,
  onChange,
  onRemove,
}: {
  binding: AgentKnowledgeBinding;
  bases: KnowledgeBase[];
  onChange: (next: AgentKnowledgeBinding) => void;
  onRemove: () => void;
}) {
  const { t } = useI18n();
  const { workspaceId } = useFrontendSession();
  const knowledgeBaseId = binding.knowledge_base_id;
  const pinned = binding.binding_mode === "PINNED";

  const load = useCallback(
    (auth: AuthInput) => listSnapshots(auth, knowledgeBaseId),
    [knowledgeBaseId],
  );
  const snapshots = useWorkspaceData<KnowledgeSnapshot[]>(
    load,
    `kb-snapshots:${workspaceId}:${knowledgeBaseId}`,
    { enabled: pinned && Boolean(knowledgeBaseId) },
  );
  const snapshotList = snapshots.data ?? [];

  return (
    <div className="binding-row">
      <label>
        {t("agents.knowledgeBase")}
        <select
          value={knowledgeBaseId}
          onChange={(event) =>
            onChange({ ...binding, knowledge_base_id: event.target.value, snapshot_id: null })
          }
        >
          <option value="">{t("agents.selectKnowledgeBase")}</option>
          {bases.map((base) => (
            <option value={base.id} key={base.id}>
              {base.name}
            </option>
          ))}
        </select>
      </label>
      <label>
        {t("agents.bindingMode")}
        <select
          value={binding.binding_mode}
          onChange={(event) => {
            const mode = event.target.value as KnowledgeBindingMode;
            onChange({ ...binding, binding_mode: mode, snapshot_id: mode === "PINNED" ? binding.snapshot_id : null });
          }}
        >
          <option value="LATEST">{t("agents.bindingModeLatest")}</option>
          <option value="PINNED">{t("agents.bindingModePinned")}</option>
        </select>
      </label>
      {pinned ? (
        <label>
          {t("agents.snapshot")}
          <select
            value={binding.snapshot_id ?? ""}
            onChange={(event) => onChange({ ...binding, snapshot_id: event.target.value || null })}
          >
            <option value="">{t("agents.selectSnapshot")}</option>
            {snapshotList.map((snapshot) => (
              <option value={snapshot.id} key={snapshot.id}>
                {snapshot.content_hash.slice(0, 12)} · {snapshot.item_count}
              </option>
            ))}
          </select>
          <InlineError error={snapshots.error} fallback={t("errors.loadSnapshots")} />
        </label>
      ) : (
        <span className="state-hint">{t("agents.latestResolves")}</span>
      )}
      <button type="button" className="button button-ghost" onClick={onRemove}>
        {t("common.remove")}
      </button>
    </div>
  );
}

/** One tool binding. "Latest" submits `tool_revision_id: null`. */
function ToolBindingRow({
  binding,
  tools,
  onChange,
  onRemove,
}: {
  binding: AgentToolBinding;
  tools: Tool[];
  onChange: (next: AgentToolBinding) => void;
  onRemove: () => void;
}) {
  const { t } = useI18n();
  const { workspaceId } = useFrontendSession();
  const toolId = binding.tool_id;

  const load = useCallback((auth: AuthInput) => listToolRevisions(auth, toolId), [toolId]);
  const revisions = useWorkspaceData<ToolRevision[]>(load, `tool-revisions:${workspaceId}:${toolId}`, {
    enabled: Boolean(toolId),
  });
  const revisionList = revisions.data ?? [];

  return (
    <div className="binding-row">
      <label>
        {t("agents.tool")}
        <select
          value={toolId}
          onChange={(event) => onChange({ tool_id: event.target.value, tool_revision_id: null })}
        >
          <option value="">{t("agents.selectTool")}</option>
          {tools.map((tool) => (
            <option value={tool.id} key={tool.id}>
              {tool.name}
            </option>
          ))}
        </select>
      </label>
      <label>
        {t("agents.revision")}
        <select
          value={binding.tool_revision_id ?? ""}
          onChange={(event) => onChange({ ...binding, tool_revision_id: event.target.value || null })}
        >
          <option value="">{t("agents.latestRevision")}</option>
          {revisionList.map((revision) => (
            <option value={revision.id} key={revision.id}>
              #{revision.revision_number} · {revision.spec_hash.slice(0, 12)}
            </option>
          ))}
        </select>
      </label>
      <span className="state-hint">
        {binding.tool_revision_id ? t("agents.pinnedRevision") : t("agents.latestResolves")}
      </span>
      <button type="button" className="button button-ghost" onClick={onRemove}>
        {t("common.remove")}
      </button>
    </div>
  );
}
