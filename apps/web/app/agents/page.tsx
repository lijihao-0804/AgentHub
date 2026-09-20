"use client";

import Link from "next/link";
import { useCallback, useEffect, useState, type FormEvent } from "react";

import { EmptyState, ErrorState, LoadingState, Panel, SessionRequired } from "../../components/states";
import { useFrontendSession } from "../../components/session-provider";
import { useWorkspaceData, useWorkspaceMutation } from "../../components/use-workspace-data";
import { errorHintKey, type AuthInput } from "../../lib/api-client";
import { createAgent, listAgents, type Agent, type KnowledgeBindingMode } from "../../lib/agents";
import { listModelProfiles, type ModelProfile } from "../../lib/models";
import { useI18n } from "../../i18n/provider";

const EMPTY_FORM = {
  name: "",
  description: "",
  system_prompt: "",
  model_profile_id: "",
  knowledge_binding_mode: "LATEST" as KnowledgeBindingMode,
  max_attempts: "",
  max_steps: "",
  max_tool_calls: "",
};

export default function AgentsPage() {
  const { t, formatDateTime } = useI18n();
  const { connected, sessionId, workspaceId } = useFrontendSession();

  const loadAgents = useCallback((auth: AuthInput) => listAgents(auth), []);
  const loadProfiles = useCallback((auth: AuthInput) => listModelProfiles(auth), []);

  const agents = useWorkspaceData<Agent[]>(loadAgents, `agents:${workspaceId}`);
  const profiles = useWorkspaceData<ModelProfile[]>(loadProfiles, `profiles:${workspaceId}`);
  const mutation = useWorkspaceMutation(`agents:${workspaceId}`);

  const [form, setForm] = useState(EMPTY_FORM);
  const [showForm, setShowForm] = useState(false);
  const [showAdvanced, setShowAdvanced] = useState(false);

  useEffect(() => {
    setForm(EMPTY_FORM);
    setShowForm(false);
    setShowAdvanced(false);
  }, [sessionId]);

  if (!connected) {
    return (
      <div className="page">
        <header className="page-header">
          <p className="eyebrow">{t("agents.eyebrow")}</p>
          <h1>{t("agents.title")}</h1>
        </header>
        <SessionRequired contextKey="session.context.agents" />
      </div>
    );
  }

  const agentList = agents.data ?? [];
  const profileList = profiles.data ?? [];

  function profileLabel(profileId: string): string {
    const profile = profileList.find((item) => item.id === profileId);
    return profile ? profile.model : t("common.unknown");
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const runtimeConfig = {
      ...(form.max_steps ? { max_steps: Number(form.max_steps) } : {}),
      ...(form.max_tool_calls ? { max_tool_calls: Number(form.max_tool_calls) } : {}),
    };
    const result = await mutation.run((auth) =>
      createAgent(auth, {
        name: form.name.trim(),
        description: form.description.trim() || null,
        system_prompt: form.system_prompt,
        model_profile_id: form.model_profile_id,
        knowledge_binding_mode: form.knowledge_binding_mode,
        ...(form.max_attempts ? { model_retry_policy: { max_attempts: Number(form.max_attempts) } } : {}),
        ...(Object.keys(runtimeConfig).length > 0 ? { runtime_config: runtimeConfig } : {}),
      }),
    );
    if (result) {
      setForm(EMPTY_FORM);
      setShowForm(false);
      setShowAdvanced(false);
      agents.reload();
    }
  }

  return (
    <div className="page">
      <header className="page-header">
        <p className="eyebrow">{t("agents.eyebrow")}</p>
        <h1>{t("agents.title")}</h1>
        <p className="page-lede">{t("agents.lede")}</p>
      </header>

      <Panel
        ariaLabel={t("agents.title")}
        title={t("agents.agents")}
        actions={
          <button
            type="button"
            className="button button-primary"
            onClick={() => {
              setShowForm((value) => !value);
              setForm(EMPTY_FORM);
            }}
            disabled={profileList.length === 0}
          >
            {t("agents.createAgent")}
          </button>
        }
      >
        {profiles.error && (
          <ErrorState
            code={profiles.error.code}
            message={profiles.error.message || t("errors.loadProfiles")}
            hint={errorHintKey(profiles.error) ? t(errorHintKey(profiles.error)!) : undefined}
            onRetry={profiles.reload}
          />
        )}
        {profiles.loaded && profileList.length === 0 && (
          <p className="state-hint">
            {t("agents.needsProfile")} <Link href="/settings/models">{t("settings.models.title")}</Link>
          </p>
        )}

        {showForm && (
          <form className="eval-form" onSubmit={submit} noValidate>
            <p className="eval-form-title">{t("agents.createAgent")}</p>
            <div className="form-grid">
              <label>
                {t("agents.name")}
                <input
                  value={form.name}
                  onChange={(event) => setForm((current) => ({ ...current, name: event.target.value }))}
                  maxLength={200}
                />
              </label>
              <label>
                {t("agents.modelProfile")}
                <select
                  value={form.model_profile_id}
                  onChange={(event) =>
                    setForm((current) => ({ ...current, model_profile_id: event.target.value }))
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
                {t("agents.description")}
                <input
                  value={form.description}
                  onChange={(event) =>
                    setForm((current) => ({ ...current, description: event.target.value }))
                  }
                  maxLength={500}
                />
              </label>
              <label>
                {t("agents.bindingMode")}
                <select
                  value={form.knowledge_binding_mode}
                  onChange={(event) =>
                    setForm((current) => ({
                      ...current,
                      knowledge_binding_mode: event.target.value as KnowledgeBindingMode,
                    }))
                  }
                >
                  <option value="LATEST">LATEST</option>
                  <option value="PINNED">PINNED</option>
                </select>
              </label>
            </div>
            <label>
              {t("agents.systemPrompt")}
              <textarea
                rows={6}
                value={form.system_prompt}
                onChange={(event) =>
                  setForm((current) => ({ ...current, system_prompt: event.target.value }))
                }
              />
            </label>

            <details open={showAdvanced} onToggle={(event) => setShowAdvanced(event.currentTarget.open)}>
              <summary>{t("agents.advanced")}</summary>
              <p className="state-hint">{t("agents.advancedHint")}</p>
              <div className="form-grid">
                <label>
                  {t("agents.maxAttempts")}
                  <input
                    type="number"
                    min="1"
                    value={form.max_attempts}
                    onChange={(event) =>
                      setForm((current) => ({ ...current, max_attempts: event.target.value }))
                    }
                  />
                </label>
                <label>
                  {t("agents.maxSteps")}
                  <input
                    type="number"
                    min="1"
                    value={form.max_steps}
                    onChange={(event) => setForm((current) => ({ ...current, max_steps: event.target.value }))}
                  />
                </label>
                <label>
                  {t("agents.maxToolCalls")}
                  <input
                    type="number"
                    min="1"
                    value={form.max_tool_calls}
                    onChange={(event) =>
                      setForm((current) => ({ ...current, max_tool_calls: event.target.value }))
                    }
                  />
                </label>
              </div>
            </details>

            {mutation.error && (
              <p className="session-error" role="alert">
                <code>{mutation.error.code}</code> {mutation.error.message || t("errors.requestFailed")}
              </p>
            )}
            <div className="form-actions">
              <button
                type="submit"
                className="button button-primary"
                disabled={
                  mutation.pending ||
                  !form.name.trim() ||
                  !form.model_profile_id ||
                  !form.system_prompt.trim()
                }
              >
                {t("agents.createAgent")}
              </button>
              <button type="button" className="button button-ghost" onClick={() => setShowForm(false)}>
                {t("session.cancel")}
              </button>
            </div>
          </form>
        )}

        {agents.error && (
          <ErrorState
            code={agents.error.code}
            message={agents.error.message || t("errors.loadAgents")}
            hint={errorHintKey(agents.error) ? t(errorHintKey(agents.error)!) : undefined}
            onRetry={agents.reload}
          />
        )}
        {agents.loading && !agents.error && <LoadingState />}
        {agents.loaded && !agents.error && agentList.length === 0 && (
          <EmptyState title={t("agents.noAgents")} hint={t("agents.noAgentsHint")} />
        )}

        {agentList.length > 0 && (
          <div className="data-table">
            <table>
              <thead>
                <tr>
                  <th scope="col">{t("agents.name")}</th>
                  <th scope="col">{t("agents.modelProfile")}</th>
                  <th scope="col">{t("agents.bindingMode")}</th>
                  <th scope="col">{t("agents.promptVersion")}</th>
                  <th scope="col">{t("common.updated")}</th>
                </tr>
              </thead>
              <tbody>
                {agentList.map((agent) => (
                  <tr key={agent.id}>
                    <td data-label={t("agents.name")}>
                      <Link href={`/agents/${agent.id}`}>{agent.name}</Link>
                      {agent.description && <span className="state-hint">{agent.description}</span>}
                    </td>
                    <td data-label={t("agents.modelProfile")}>{profileLabel(agent.model_profile_id)}</td>
                    <td data-label={t("agents.bindingMode")}>
                      <code>{agent.knowledge_binding_mode}</code>
                    </td>
                    <td data-label={t("agents.promptVersion")}>v{agent.prompt_version}</td>
                    <td data-label={t("common.updated")}>
                      {agent.updated_at ? formatDateTime(agent.updated_at) : t("common.none")}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>
  );
}
