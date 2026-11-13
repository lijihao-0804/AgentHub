"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useDeferredValue, useEffect, useRef, useState, type FormEvent } from "react";

import { EmptyState, ErrorState, InlineError, LoadingState, Panel, SessionRequired } from "@/components/ui/states";
import {
  CatalogEmptyNotice,
  CatalogPageHeader,
  CatalogStatStrip,
  CatalogToolbar,
} from "@/components/catalog/catalog-layout";
import { useFrontendSession } from "@/components/providers/session-provider";
import { useWorkspaceData, useWorkspaceMutation } from "@/hooks/use-workspace-data";
import { errorHintKey, type AuthInput } from "@/lib/api/client";
import { createAgent, listAgents, type Agent, type KnowledgeBindingMode } from "@/lib/api/agents";
import { listAgentTemplates, type AgentTemplate } from "@/lib/api/agent-templates";
import { listModelProfiles, type ModelProfile } from "@/lib/api/models";
import Select from "@/components/ui/select";
import { useI18n } from "@/i18n/provider";

const EMPTY_FORM = {
  templateKey: "",
  name: "",
  description: "",
  system_prompt: "",
  model_profile_id: "",
  knowledge_binding_mode: "LATEST" as KnowledgeBindingMode,
  max_attempts: "",
  max_steps: "",
  max_tool_calls: "",
};

function withTemplate(current: typeof EMPTY_FORM, template: AgentTemplate, templates: AgentTemplate[]) {
  const previous = templates.find((entry) => entry.key === current.templateKey);
  return {
    ...current,
    templateKey: template.key,
    name: !current.name || current.name === previous?.name ? template.name : current.name,
    description: !current.description || current.description === previous?.description ? template.description : current.description,
    system_prompt: template.system_prompt,
  };
}

export default function AgentsPage() {
  return <Suspense fallback={<LoadingState />}><AgentCatalog /></Suspense>;
}

function AgentCatalog() {
  const { t, locale, formatCount, formatDateTime } = useI18n();
  const router = useRouter();
  const templateKey = useSearchParams().get("template");
  const appliedLink = useRef("");
  const { connected, sessionId, workspaceId, permissions } = useFrontendSession();
  const canCreate = permissions?.includes("agent_create") === true;

  const loadAgents = useCallback((auth: AuthInput) => listAgents(auth), []);
  const loadProfiles = useCallback((auth: AuthInput) => listModelProfiles(auth), []);

  const agents = useWorkspaceData<Agent[]>(loadAgents, `agents:${workspaceId}`);
  const profiles = useWorkspaceData<ModelProfile[]>(loadProfiles, `profiles:${workspaceId}`);
  const mutation = useWorkspaceMutation(`agents:${workspaceId}`);

  const [form, setForm] = useState(EMPTY_FORM);
  const [showForm, setShowForm] = useState(false);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [search, setSearch] = useState("");
  const deferredSearch = useDeferredValue(search);
  const templates = useWorkspaceData<AgentTemplate[]>(
    (auth: AuthInput) => listAgentTemplates(auth),
    `agent-templates:${workspaceId}`,
  );

  useEffect(() => {
    setForm(EMPTY_FORM);
    setShowForm(false);
    setShowAdvanced(false);
    setSearch("");
  }, [sessionId]);

  useEffect(() => {
    const requested = templates.data?.find((template) => template.key === templateKey);
    const link = `${sessionId}:${templateKey ?? ""}`;
    if (!canCreate || !requested || appliedLink.current === link) return;
    appliedLink.current = link;
    setForm(withTemplate(EMPTY_FORM, requested, templates.data ?? []));
    setShowForm(true);
  }, [canCreate, sessionId, templateKey, templates.data]);

  const copy = locale === "zh-CN"
    ? {
        eyebrow: "智能体目录",
        description: "创建并管理工作区里的智能体。可以从模板或空白草稿开始，再为它配置模型、知识和工具。",
        searchLabel: "搜索智能体",
        searchPlaceholder: "按名称、说明或模型搜索",
        clearSearch: "清空搜索",
        result: (visible: number, total: number) => `显示 ${visible} / ${total}`,
        noMatch: "没有匹配的智能体",
        noMatchHint: "尝试更短的关键词，或清空搜索查看全部智能体。",
        agents: "智能体",
        profiles: "模型配置",
        templates: "起始模板",
        loading: "加载中",
        unavailable: "不可用",
      }
    : {
        eyebrow: "Agent catalog",
        description: "Create and manage the agents your team works with. Start from a template or a blank draft, then connect the model, knowledge, and tools each agent needs.",
        searchLabel: "Search agents",
        searchPlaceholder: "Search by name, description, or model",
        clearSearch: "Clear search",
        result: (visible: number, total: number) => `${visible} of ${total} shown`,
        noMatch: "No matching agents",
        noMatchHint: "Try a shorter term or clear the search to see every agent.",
        agents: "Agents",
        profiles: "Model profiles",
        templates: "Start templates",
        loading: "Loading",
        unavailable: "Unavailable",
      };

  const agentList = agents.data ?? [];
  const profileList = profiles.data ?? [];
  const normalizedSearch = deferredSearch.trim().toLocaleLowerCase(locale);
  const filteredAgents = normalizedSearch
    ? agentList.filter((agent) =>
        [agent.name, agent.description ?? "", profileLabel(agent.model_profile_id)]
          .some((value) => value.toLocaleLowerCase(locale).includes(normalizedSearch)),
      )
    : agentList;

  if (!connected) {
    return (
      <div className="page">
        <header className="page-header">
          <h1>{t("agents.title")}</h1>
        </header>
        <SessionRequired contextKey="session.context.agents" />
      </div>
    );
  }

  function profileLabel(profileId: string): string {
    const profile = profileList.find((item) => item.id === profileId);
    return profile ? profile.model : t("common.unknown");
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canCreate) return;
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
      // The next step of the journey (model → bindings → publish) happens on
      // the detail page; dropping the user back on the list loses momentum.
      router.push(`/agents/${encodeURIComponent(result.id)}`);
    }
  }

  return (
    <div className="page">
      <CatalogPageHeader
        eyebrow={copy.eyebrow}
        title={t("agents.title")}
        description={copy.description}
        actions={
          <button
            type="button"
            className="button button-primary"
            onClick={() => {
              setShowForm((value) => !value);
              setForm(EMPTY_FORM);
            }}
            disabled={!canCreate || profileList.length === 0}
            title={!canCreate ? t("agents.noPermissionHint") : undefined}
          >
            {t("agents.createAgent")}
          </button>
        }
      />

      <CatalogStatStrip
        items={[
          { label: copy.agents, value: agents.loaded ? formatCount(agentList.length) : agents.loading ? copy.loading : copy.unavailable },
          { label: copy.profiles, value: profiles.loaded ? formatCount(profileList.length) : profiles.loading ? copy.loading : copy.unavailable },
          { label: copy.templates, value: templates.loaded ? formatCount((templates.data ?? []).length) : templates.loading ? copy.loading : copy.unavailable },
        ]}
      />

      <Panel
        ariaLabel={t("agents.title")}
      >
        {profiles.error && (
          <ErrorState
            code={profiles.error.code}
            message={profiles.error.message || t("errors.loadProfiles")}
            hint={errorHintKey(profiles.error) ? t(errorHintKey(profiles.error)!) : undefined}
            onRetry={profiles.reload}
          />
        )}
        {(showForm || templateKey) && templates.error && (
          <ErrorState code={templates.error.code} message={templates.error.message} onRetry={templates.reload} />
        )}
        {profiles.loaded && profileList.length === 0 && (
          <p className="state-hint">
            {t("agents.needsProfilePrefix")}{" "}
            <Link href="/settings/models">{t("agents.needsProfileLink")}</Link>
            {t("agents.needsProfileSuffix")}
          </p>
        )}

        {showForm && (templates.data?.length ?? 0) > 0 && (
          <div className="template-strip" role="group" aria-label={t("agents.templates.title")}>
            <p className="state-hint">{t("agents.templates.pick")}</p>
            {templates.data!.map((template) => (
              <button
                type="button"
                key={template.key}
                className="overview-card"
                aria-pressed={form.templateKey === template.key}
                onClick={() =>
                  setForm((current) => withTemplate(current, template, templates.data ?? []))
                }
              >
                <span className="overview-card-title">{template.name}</span>
                <span className="overview-card-description">{template.description}</span>
                {template.tool_hints.length > 0 && (
                  <span className="overview-card-cta">{t("agents.templates.toolHints", { hints: template.tool_hints.join(", ") })}</span>
                )}
              </button>
            ))}
          </div>
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
                <Select
                  value={form.model_profile_id}
                  onChange={(next) => setForm((current) => ({ ...current, model_profile_id: next }))}
                  placeholder={t("agents.selectProfile")}
                  options={profileList.map((profile) => ({ value: profile.id, label: profile.model }))}
                />
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
                <Select
                  value={form.knowledge_binding_mode}
                  onChange={(next) =>
                    setForm((current) => ({
                      ...current,
                      knowledge_binding_mode: next as KnowledgeBindingMode,
                    }))
                  }
                  options={[
                    { value: "LATEST", label: t("agents.bindingModeLatest") },
                    { value: "PINNED", label: t("agents.bindingModePinned") },
                  ]}
                />
                <span className="state-hint">{t("agents.bindingModeHint")}</span>
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
                placeholder={t("agents.systemPromptPlaceholder")}
              />
              <span className="state-hint">{t("agents.systemPromptHint")}</span>
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

            <InlineError error={mutation.error} fallback={t("errors.requestFailed")} />
            <div className="form-actions">
              <button
                type="submit"
                className="button button-primary"
                disabled={
                  !canCreate || mutation.pending ||
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
        {agents.loaded && !agents.error && agentList.length > 0 && (
          <CatalogToolbar
            searchLabel={copy.searchLabel}
            searchPlaceholder={copy.searchPlaceholder}
            searchValue={search}
            onSearchChange={setSearch}
            resultLabel={copy.result(filteredAgents.length, agentList.length)}
            clearLabel={copy.clearSearch}
          >
            <button type="button" className="button button-ghost" onClick={agents.reload} disabled={agents.loading}>
              {t("common.refresh")}
            </button>
          </CatalogToolbar>
        )}
        {agents.loaded && !agents.error && agentList.length === 0 && (
          <EmptyState
            title={t("agents.noAgents")}
            hint={t("agents.noAgentsHint")}
            actions={
              <button
                type="button"
                className="button button-primary"
                onClick={() => setShowForm(true)}
                disabled={!canCreate || profileList.length === 0}
                title={!canCreate ? t("agents.noPermissionHint") : undefined}
              >
                {t("agents.createAgent")}
              </button>
            }
          />
        )}

        {agents.loaded && !agents.error && agentList.length > 0 && filteredAgents.length === 0 && (
          <CatalogEmptyNotice title={copy.noMatch} hint={copy.noMatchHint} />
        )}

        {filteredAgents.length > 0 && (
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
                {filteredAgents.map((agent) => (
                  <tr key={agent.id}>
                    <td data-label={t("agents.name")}>
                      <Link href={`/agents/${agent.id}`}>{agent.name}</Link>
                      {agent.description && (
                        <span className="state-hint cell-clamp" title={agent.description}>
                          {agent.description}
                        </span>
                      )}
                    </td>
                    <td className="tight-cell" data-label={t("agents.modelProfile")}>{profileLabel(agent.model_profile_id)}</td>
                    <td className="tight-cell" data-label={t("agents.bindingMode")}>
                      {agent.knowledge_binding_mode === "PINNED"
                        ? t("agents.bindingModeShortPinned")
                        : t("agents.bindingModeShortLatest")}
                    </td>
                    <td className="tight-cell" data-label={t("agents.promptVersion")}>v{agent.prompt_version}</td>
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
