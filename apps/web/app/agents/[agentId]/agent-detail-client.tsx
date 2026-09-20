"use client";

import Link from "next/link";
import { useCallback, useEffect, useState, type FormEvent } from "react";

import { EmptyState, ErrorState, LoadingState, Panel, SessionRequired } from "../../../components/states";
import { useFrontendSession } from "../../../components/session-provider";
import { useWorkspaceData, useWorkspaceMutation } from "../../../components/use-workspace-data";
import { errorHintKey, type AuthInput } from "../../../lib/api-client";
import {
  getAgent,
  getAgentKnowledgeBindings,
  getAgentToolBindings,
  listAgentVersions,
  patchAgent,
  publishAgent,
  putAgentKnowledgeBindings,
  putAgentToolBindings,
  type Agent,
  type AgentKnowledgeBinding,
  type AgentToolBinding,
  type AgentVersion,
  type KnowledgeBindingMode,
} from "../../../lib/agents";
import { listKnowledgeBases, listSnapshots, type KnowledgeBase, type KnowledgeSnapshot } from "../../../lib/knowledge";
import { listModelProfiles, type ModelProfile } from "../../../lib/models";
import { listToolRevisions, listTools, type Tool, type ToolRevision } from "../../../lib/tools";
import { useI18n } from "../../../i18n/provider";

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

function numberOrNull(value: string): number | null {
  const trimmed = value.trim();
  if (!trimmed) return null;
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) ? parsed : null;
}

export default function AgentDetailClient({ agentId }: { agentId: string }) {
  const { t, formatDateTime } = useI18n();
  const { connected, sessionId, workspaceId } = useFrontendSession();

  const scope = `${workspaceId}:${agentId}`;
  const [tab, setTab] = useState<Tab>("general");
  const [notice, setNotice] = useState<string | null>(null);
  const [confirmPublish, setConfirmPublish] = useState(false);
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
  const [knowledgeDraft, setKnowledgeDraft] = useState<AgentKnowledgeBinding[]>([]);
  const [toolDraft, setToolDraft] = useState<AgentToolBinding[]>([]);

  // A session change or a different agent invalidates every local draft.
  useEffect(() => {
    setTab("general");
    setNotice(null);
    setConfirmPublish(false);
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

  async function saveGeneral(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setNotice(null);
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
    const result = await agentMutation.run((auth) =>
      patchAgent(auth, agentId, {
        runtime_config: {
          max_steps: numberOrNull(runtime.max_steps),
          max_tool_calls: numberOrNull(runtime.max_tool_calls),
          max_identical_calls: numberOrNull(runtime.max_identical_calls),
          max_parallel_reads: numberOrNull(runtime.max_parallel_reads),
          context_budget: {
            reserved_output_tokens: numberOrNull(runtime.reserved_output_tokens),
            max_retrieval_tokens: numberOrNull(runtime.max_retrieval_tokens),
            max_tool_result_tokens: numberOrNull(runtime.max_tool_result_tokens),
          },
        },
      }),
    );
    if (result) {
      setNotice(t("agents.saved"));
      agent.reload();
    }
  }

  async function saveKnowledgeBindings() {
    setNotice(null);
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
    const draft = toolDraft.filter((binding) => binding.tool_id);
    const result = await toolMutation.run((auth) => putAgentToolBindings(auth, agentId, draft));
    if (result) {
      setNotice(t("agents.bindingsSaved"));
      toolBindings.reload();
    }
  }

  async function runPublish() {
    setNotice(null);
    const result = await publishMutation.run((auth) => publishAgent(auth, agentId));
    setConfirmPublish(false);
    if (result) {
      setPublishResult({ version: result.version_number, hash: result.resolved_spec_hash });
      versions.reload();
    }
  }

  return (
    <div className="page">
      <header className="page-header">
        <p className="eyebrow">{t("agents.eyebrow")}</p>
        <h1>{loadedAgent?.name ?? t("agents.detail")}</h1>
        <p className="page-lede">
          <code>{agentId}</code>
        </p>
      </header>

      <div className="page-toolbar">
        <Link className="button button-ghost" href="/agents">
          {t("agents.backToAgents")}
        </Link>
        <button
          type="button"
          className="button button-primary"
          onClick={() => setConfirmPublish(true)}
          disabled={publishMutation.pending || !loadedAgent}
        >
          {t("agents.publish")}
        </button>
      </div>

      {notice && <p className="inline-notice">{notice}</p>}
      {publishResult && (
        <p className="inline-notice">
          {t("agents.publishedVersion", { version: publishResult.version })}{" "}
          <code className="hash-value">{publishResult.hash}</code>
        </p>
      )}
      {publishMutation.error && (
        <p className="session-error" role="alert">
          <code>{publishMutation.error.code}</code>{" "}
          {publishMutation.error.message || t("errors.requestFailed")}
        </p>
      )}

      {confirmPublish && (
        <Panel ariaLabel={t("agents.publish")} title={t("agents.publish")}>
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
            <button type="button" className="button button-ghost" onClick={() => setConfirmPublish(false)}>
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

      {agentMutation.error && (
        <p className="session-error" role="alert">
          <code>{agentMutation.error.code}</code>{" "}
          {agentMutation.error.message || t("errors.requestFailed")}
        </p>
      )}

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
              <option value="LATEST">LATEST</option>
              <option value="PINNED">PINNED</option>
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

          {knowledgeMutation.error && (
            <p className="session-error" role="alert">
              <code>{knowledgeMutation.error.code}</code>{" "}
              {knowledgeMutation.error.message || t("errors.requestFailed")}
            </p>
          )}

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

          {toolMutation.error && (
            <p className="session-error" role="alert">
              <code>{toolMutation.error.code}</code>{" "}
              {toolMutation.error.message || t("errors.requestFailed")}
            </p>
          )}

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
                  min="0"
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
                  min="0"
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
                  min="0"
                  value={runtime.max_tool_result_tokens}
                  onChange={(event) =>
                    setRuntime((current) => ({ ...current, max_tool_result_tokens: event.target.value }))
                  }
                />
              </label>
            </div>
            <div className="form-actions">
              <button type="submit" className="button button-primary" disabled={agentMutation.pending}>
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
          <option value="LATEST">LATEST</option>
          <option value="PINNED">PINNED</option>
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
          {snapshots.error && (
            <span className="state-hint">
              <code>{snapshots.error.code}</code> {snapshots.error.message}
            </span>
          )}
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
