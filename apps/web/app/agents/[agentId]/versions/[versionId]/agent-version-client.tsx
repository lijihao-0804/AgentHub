"use client";

import Link from "next/link";
import { useCallback } from "react";

import { EmptyState, ErrorState, LoadingState, Panel, SessionRequired } from "../../../../../components/states";
import TechnicalDetails from "../../../../../components/technical-details";
import { useFrontendSession } from "../../../../../components/session-provider";
import { useWorkspaceData } from "../../../../../components/use-workspace-data";
import { errorHintKey, type AuthInput } from "../../../../../lib/api-client";
import { getAgentVersion, type AgentVersion } from "../../../../../lib/agents";
import { useI18n } from "../../../../../i18n/provider";

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
  const { connected, workspaceId } = useFrontendSession();

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
      <header className="page-header">
        <p className="eyebrow">{t("agents.eyebrow")}</p>
        <h1>{version ? `v${version.version_number}` : t("agents.versionDetail")}</h1>
        <p className="page-lede">
          <code>{versionId}</code>
        </p>
      </header>

      <div className="page-toolbar">
        <Link className="button button-ghost" href={`/agents/${agentId}`}>
          {t("agents.backToAgent")}
        </Link>
      </div>

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
