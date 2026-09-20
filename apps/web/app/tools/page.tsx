"use client";

import Link from "next/link";
import { useCallback, useEffect, useState, type FormEvent } from "react";

import { EmptyState, ErrorState, InlineError, LoadingState, Panel, SessionRequired } from "@/components/ui/states";
import StatusBadge from "@/components/ui/status-badge";
import { useFrontendSession } from "@/components/providers/session-provider";
import { useWorkspaceData, useWorkspaceMutation } from "@/hooks/use-workspace-data";
import { errorHintKey, type AuthInput } from "@/lib/api/client";
import {
  createTool,
  listToolCatalog,
  listTools,
  type Tool,
  type ToolCatalogItem,
} from "@/lib/api/tools";
import { useI18n } from "@/i18n/provider";

/** Effect and risk are server facts; the UI only colours what it is told. */
function riskTone(risk: string | null | undefined): "neutral" | "info" | "warning" | "danger" {
  switch ((risk ?? "").toUpperCase()) {
    case "LOW":
      return "info";
    case "MEDIUM":
      return "warning";
    case "HIGH":
      return "danger";
    default:
      return "neutral";
  }
}

export default function ToolsPage() {
  const { t } = useI18n();
  const { connected, sessionId, workspaceId } = useFrontendSession();

  const loadTools = useCallback((auth: AuthInput) => listTools(auth), []);
  const loadCatalog = useCallback((auth: AuthInput) => listToolCatalog(auth), []);

  const tools = useWorkspaceData<Tool[]>(loadTools, `tools:${workspaceId}`);
  const catalog = useWorkspaceData<ToolCatalogItem[]>(loadCatalog, `tool-catalog:${workspaceId}`);
  const mutation = useWorkspaceMutation(`tools:${workspaceId}`);

  const [selectedIdentity, setSelectedIdentity] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    setSelectedIdentity(null);
    setName("");
    setDescription("");
    setNotice(null);
  }, [sessionId]);

  if (!connected) {
    return (
      <div className="page">
        <header className="page-header">
          <p className="eyebrow">{t("tools.eyebrow")}</p>
          <h1>{t("tools.title")}</h1>
        </header>
        <SessionRequired contextKey="session.context.tools" />
      </div>
    );
  }

  const toolList = tools.data ?? [];
  const catalogList = catalog.data ?? [];

  async function submitAdd(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedIdentity) return;
    const identity = selectedIdentity;
    setNotice(null);
    const result = await mutation.run((auth) =>
      createTool(auth, {
        identity,
        name: name.trim(),
        description: description.trim() || null,
      }),
    );
    if (result) {
      setSelectedIdentity(null);
      setName("");
      setDescription("");
      setNotice(t("tools.toolAdded"));
      tools.reload();
    }
  }

  return (
    <div className="page">
      <header className="page-header">
        <p className="eyebrow">{t("tools.eyebrow")}</p>
        <h1>{t("tools.title")}</h1>
        <p className="page-lede">{t("tools.lede")}</p>
      </header>

      {notice && <p className="inline-notice">{notice}</p>}

      <Panel ariaLabel={t("tools.workspaceTools")} title={t("tools.workspaceTools")}>
        {tools.error && (
          <ErrorState
            code={tools.error.code}
            message={tools.error.message || t("errors.loadTools")}
            hint={errorHintKey(tools.error) ? t(errorHintKey(tools.error)!) : undefined}
            onRetry={tools.reload}
          />
        )}
        {tools.loading && !tools.error && <LoadingState />}
        {tools.loaded && !tools.error && toolList.length === 0 && (
          <EmptyState title={t("tools.noTools")} hint={t("tools.noToolsHint")} />
        )}

        {toolList.length > 0 && (
          <div className="data-table">
            <table>
              <thead>
                <tr>
                  <th scope="col">{t("tools.name")}</th>
                  <th scope="col">{t("tools.identity")}</th>
                  <th scope="col">{t("tools.effect")}</th>
                  <th scope="col">{t("tools.risk")}</th>
                  <th scope="col">{t("tools.approval")}</th>
                  <th scope="col">{t("tools.revision")}</th>
                  <th scope="col">{t("settings.models.status")}</th>
                </tr>
              </thead>
              <tbody>
                {toolList.map((tool) => (
                  <tr key={tool.id}>
                    <td data-label={t("tools.name")}>
                      <Link href={`/tools/${tool.id}`}>{tool.name}</Link>
                    </td>
                    <td data-label={t("tools.identity")}>
                      <code>{tool.identity ?? "—"}</code>
                    </td>
                    <td data-label={t("tools.effect")}>
                      {tool.effect ? <StatusBadge status={tool.effect} /> : "—"}
                    </td>
                    <td data-label={t("tools.risk")}>
                      {tool.risk_level ? (
                        <StatusBadge status={tool.risk_level} tone={riskTone(tool.risk_level)} />
                      ) : (
                        "—"
                      )}
                    </td>
                    <td data-label={t("tools.approval")}>
                      <code>{tool.approval_policy ?? "—"}</code>
                    </td>
                    <td data-label={t("tools.revision")}>
                      {tool.current_revision_number != null ? `#${tool.current_revision_number}` : "—"}
                    </td>
                    <td data-label={t("settings.models.status")}>
                      <StatusBadge
                        status={tool.enabled ? "ENABLED" : "DISABLED"}
                        tone={tool.enabled ? "success" : "neutral"}
                        label={tool.enabled ? t("common.enabled") : t("common.disabled")}
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      <Panel
        ariaLabel={t("tools.catalog")}
        title={t("tools.catalog")}
        eyebrow={t("tools.catalogEyebrow")}
      >
        <p className="state-hint">{t("tools.catalogNote")}</p>

        {catalog.error && (
          <ErrorState
            code={catalog.error.code}
            message={catalog.error.message || t("errors.loadCatalog")}
            hint={errorHintKey(catalog.error) ? t(errorHintKey(catalog.error)!) : undefined}
            onRetry={catalog.reload}
          />
        )}
        {catalog.loading && !catalog.error && <LoadingState />}
        {catalog.loaded && !catalog.error && catalogList.length === 0 && (
          <EmptyState title={t("tools.noCatalog")} />
        )}

        {catalogList.length > 0 && (
          <div className="data-table">
            <table>
              <thead>
                <tr>
                  <th scope="col">{t("tools.identity")}</th>
                  <th scope="col">{t("tools.kind")}</th>
                  <th scope="col">{t("tools.effect")}</th>
                  <th scope="col">{t("tools.risk")}</th>
                  <th scope="col">{t("tools.approval")}</th>
                  <th scope="col">{t("settings.models.actions")}</th>
                </tr>
              </thead>
              <tbody>
                {catalogList.map((item) => (
                  <tr key={item.identity}>
                    <td data-label={t("tools.identity")}>
                      <code>{item.identity}</code>
                      {item.description && <span className="state-hint">{item.description}</span>}
                    </td>
                    <td data-label={t("tools.kind")}>
                      <code>{item.execution_kind}</code>
                    </td>
                    <td data-label={t("tools.effect")}>
                      <StatusBadge status={item.effect} />
                    </td>
                    <td data-label={t("tools.risk")}>
                      <StatusBadge status={item.risk_level} tone={riskTone(item.risk_level)} />
                    </td>
                    <td data-label={t("tools.approval")}>
                      <code>{item.approval_policy}</code>
                    </td>
                    <td data-label={t("settings.models.actions")}>
                      <button
                        type="button"
                        className="button button-ghost"
                        onClick={() => {
                          setSelectedIdentity(item.identity);
                          setName(item.identity);
                          setDescription(item.description ?? "");
                        }}
                      >
                        {t("tools.addToWorkspace")}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {selectedIdentity && (
          <form className="eval-form" onSubmit={submitAdd} noValidate>
            <p className="eval-form-title">
              {t("tools.addToWorkspace")} · <code>{selectedIdentity}</code>
            </p>
            <div className="form-grid">
              <label>
                {t("tools.name")}
                <input value={name} onChange={(event) => setName(event.target.value)} maxLength={200} />
              </label>
              <label>
                {t("tools.description")}
                <input
                  value={description}
                  onChange={(event) => setDescription(event.target.value)}
                  maxLength={500}
                />
              </label>
            </div>
            <InlineError error={mutation.error} fallback={t("errors.requestFailed")} />
            <div className="form-actions">
              <button
                type="submit"
                className="button button-primary"
                disabled={mutation.pending || !name.trim()}
              >
                {t("tools.addToWorkspace")}
              </button>
              <button
                type="button"
                className="button button-ghost"
                onClick={() => setSelectedIdentity(null)}
              >
                {t("session.cancel")}
              </button>
            </div>
          </form>
        )}
      </Panel>
    </div>
  );
}
