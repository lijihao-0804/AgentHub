"use client";

import Link from "next/link";
import { useCallback, useState } from "react";

import Breadcrumbs from "@/components/layout/breadcrumbs";
import { EmptyState, ErrorState, LoadingState, Panel, SessionRequired } from "@/components/ui/states";
import TechnicalDetails from "@/components/ui/technical-details";
import { useFrontendSession } from "@/components/providers/session-provider";
import { useWorkspaceData } from "@/hooks/use-workspace-data";
import { errorHintKey, type AuthInput } from "@/lib/api/client";
import {
  deriveAgentDraft,
  getAgentVersion,
  type KnowledgeBindingMode,
  type ModelRetryPolicy,
  type RetrievalConfig,
  type RuntimeConfig,
  patchAgent,
  type AgentDeriveResult,
  type AgentVersion,
} from "@/lib/api/agents";
import { useI18n } from "@/i18n/provider";

/**
 * Read-only view of one published agent version. Everything shown here
 * is the server's resolved artifact — no field is derived client-side.
 */
export default function AgentVersionDetailClient({
  agentId,
  versionId,
}: {
  agentId: string;
  versionId: string;
}) {
  const { t, formatDateTime } = useI18n();
  const { connected, workspaceId, accessToken } = useFrontendSession();

  // One version is fetched by id: the page never downloads the whole
  // version list only to search it in the browser.
  const load = useCallback(
    (auth: AuthInput) => getAgentVersion(auth, agentId, versionId),
    [agentId, versionId],
  );
  const versions = useWorkspaceData<AgentVersion>(
    load,
    `agent-version:${workspaceId}:${agentId}:${versionId}`,
  );

  const [derive, setDerive] = useState<AgentDeriveResult | null>(null);
  const [deriving, setDeriving] = useState(false);
  const [deriveError, setDeriveError] = useState<string | null>(null);
  const [applied, setApplied] = useState(false);

  async function runDerive() {
    setDeriving(true);
    setDeriveError(null);
    setApplied(false);
    try {
      setDerive(await deriveAgentDraft({ workspaceId, accessToken }, agentId, versionId));
    } catch {
      setDeriveError("derive-failed");
    } finally {
      setDeriving(false);
    }
  }

  async function applyDerive() {
    if (!derive) return;
    setDeriving(true);
    setDeriveError(null);
    try {
      const values = derive.values;
      await patchAgent({ workspaceId, accessToken }, agentId, {
        system_prompt: values.system_prompt ?? "",
        prompt_version: values.prompt_version ?? 1,
        model_profile_id: values.model_profile_id ?? "",
        ...(values.knowledge_binding_mode
          ? { knowledge_binding_mode: values.knowledge_binding_mode as KnowledgeBindingMode }
          : {}),
        ...(values.model_retry_policy
          ? { model_retry_policy: values.model_retry_policy as ModelRetryPolicy }
          : {}),
        ...(values.retrieval_config ? { retrieval_config: values.retrieval_config as RetrievalConfig } : {}),
        ...(values.runtime_config ? { runtime_config: values.runtime_config as RuntimeConfig } : {}),
      });
      setApplied(true);
    } catch {
      setDeriveError("apply-failed");
    } finally {
      setDeriving(false);
    }
  }

  if (!connected) {
    return (
      <div className="page">
        <header className="page-header">
          <p className="eyebrow">{t("agents.eyebrow")}</p>
          <h1>{t("agents.versionDetail")}</h1>
        </header>
        <SessionRequired contextKey="session.context.agents" />
      </div>
    );
  }

  const version = versions.data;

  return (
    <div className="page">
      <Breadcrumbs
        items={[
          { label: t("nav.agents"), href: "/agents" },
          { label: t("agents.detail"), href: `/agents/${agentId}` },
          { label: version ? `v${version.version_number}` : t("agents.versionDetail") },
        ]}
      />
      <header className="page-header">
        <p className="eyebrow">{t("agents.eyebrow")}</p>
        <h1>{version ? `v${version.version_number}` : t("agents.versionDetail")}</h1>
        <p className="page-lede">
          <code>{versionId}</code>
        </p>
      </header>

      <div className="page-toolbar">
        <Link
          className="button button-ghost"
          href={`/agents/${agentId}/versions/compare?left=${encodeURIComponent(versionId)}`}
        >
          {t("agents.versionDiff.entry")}
        </Link>
        <button type="button" className="button button-primary" onClick={() => void runDerive()} disabled={deriving}>
          {deriving ? t("agents.derive.working") : t("agents.derive.entry")}
        </button>
      </div>

      {deriveError && (
        <p className="inline-error" role="alert">
          {deriveError === "apply-failed" ? t("agents.derive.applyFailed") : t("agents.derive.failed")}
        </p>
      )}
      {applied && <p className="inline-notice" role="status">{t("agents.derive.applied")}</p>}
      {derive && (
        <Panel ariaLabel={t("agents.derive.title")} title={t("agents.derive.title")} eyebrow={t("agents.derive.eyebrow")}>
          <p className="state-hint">
            {t("agents.derive.note", {
              version: `v${derive.source_version_number}`,
              hash: derive.source_resolved_spec_hash.slice(0, 8),
            })}
          </p>
          <div className="approval-split">
            <div className="approval-state-block">
              <span className="approval-state-label">{t("agents.derive.systemPrompt")}</span>
              <p className="state-hint">{derive.values.system_prompt}</p>
            </div>
            <div className="approval-state-block">
              <span className="approval-state-label">{t("agents.derive.modelProfile")}</span>
              <code>{derive.values.model_profile_id}</code>
            </div>
          </div>
          <TechnicalDetails summary={t("common.technicalDetails")} value={derive.values} />
          <div className="form-actions">
            <button type="button" className="button button-primary" onClick={() => void applyDerive()} disabled={deriving}>
              {t("agents.derive.apply")}
            </button>
            <button type="button" className="button button-ghost" onClick={() => setDerive(null)} disabled={deriving}>
              {t("common.cancel")}
            </button>
          </div>
        </Panel>
      )}

      <Panel ariaLabel={t("agents.versionDetail")} title={t("agents.resolvedSpec")}>
        {versions.error && (
          <ErrorState
            code={versions.error.code}
            message={versions.error.message || t("errors.loadVersions")}
            hint={errorHintKey(versions.error) ? t(errorHintKey(versions.error)!) : undefined}
            onRetry={versions.reload}
          />
        )}
        {versions.loading && !versions.error && <LoadingState />}
        {versions.loaded && !versions.error && !version && (
          <EmptyState title={t("agents.versionNotFound")} />
        )}

        {version && (
          <>
            <dl className="key-values">
              <div className="key-value-row">
                <dt>{t("agents.version")}</dt>
                <dd>v{version.version_number}</dd>
              </div>
              <div className="key-value-row">
                <dt>{t("agents.specHash")}</dt>
                <dd>
                  <code className="hash-value">{version.resolved_spec_hash}</code>
                </dd>
              </div>
              <div className="key-value-row">
                <dt>{t("agents.schemaVersion")}</dt>
                <dd>{version.spec_schema_version}</dd>
              </div>
              <div className="key-value-row">
                <dt>{t("common.created")}</dt>
                <dd>{formatDateTime(version.created_at)}</dd>
              </div>
              <div className="key-value-row">
                <dt>{t("agents.createdBy")}</dt>
                <dd>{version.created_by ? <code>{version.created_by}</code> : t("common.none")}</dd>
              </div>
            </dl>
            <TechnicalDetails summary={t("agents.resolvedSpec")} value={version.resolved_spec} />
          </>
        )}
      </Panel>
    </div>
  );
}
