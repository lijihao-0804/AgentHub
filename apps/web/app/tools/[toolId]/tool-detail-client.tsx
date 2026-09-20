"use client";

import Link from "next/link";
import { useCallback, useEffect, useState, type FormEvent } from "react";

import { EmptyState, ErrorState, LoadingState, Panel, SessionRequired } from "../../../components/states";
import StatusBadge from "../../../components/status-badge";
import TechnicalDetails from "../../../components/technical-details";
import { useFrontendSession } from "../../../components/session-provider";
import { useWorkspaceData, useWorkspaceMutation } from "../../../components/use-workspace-data";
import { errorHintKey, type AuthInput } from "../../../lib/api-client";
import { getTool, listToolRevisions, patchTool, type Tool, type ToolRevision } from "../../../lib/tools";
import { useI18n } from "../../../i18n/provider";

export default function ToolDetailClient({ toolId }: { toolId: string }) {
  const { t, formatDateTime } = useI18n();
  const { connected, sessionId, workspaceId } = useFrontendSession();

  const scope = `${workspaceId}:${toolId}`;
  const loadTool = useCallback((auth: AuthInput) => getTool(auth, toolId), [toolId]);
  const loadRevisions = useCallback((auth: AuthInput) => listToolRevisions(auth, toolId), [toolId]);

  const tool = useWorkspaceData<Tool>(loadTool, `tool:${scope}`);
  const revisions = useWorkspaceData<ToolRevision[]>(loadRevisions, `tool-revisions:${scope}`);
  const mutation = useWorkspaceMutation(`tool:${scope}`);

  const [editing, setEditing] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [selectedRevisionId, setSelectedRevisionId] = useState<string | null>(null);

  useEffect(() => {
    setEditing(false);
    setName("");
    setDescription("");
    setSelectedRevisionId(null);
  }, [sessionId, toolId]);

  if (!connected) {
    return (
      <div className="page">
        <header className="page-header">
          <p className="eyebrow">{t("tools.eyebrow")}</p>
          <h1>{t("tools.detail")}</h1>
        </header>
        <SessionRequired contextKey="session.context.tools" />
      </div>
    );
  }

  const current = tool.data;
  const revisionList = revisions.data ?? [];
  // Revisions come back oldest first, so the default is the tool's
  // current revision — falling back to the newest, never the first.
  const selectedRevision =
    revisionList.find((revision) => revision.id === selectedRevisionId) ??
    revisionList.find((revision) => revision.id === current?.current_revision_id) ??
    revisionList[revisionList.length - 1] ??
    null;

  async function submitEdit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const result = await mutation.run((auth) =>
      patchTool(auth, toolId, { name: name.trim(), description: description.trim() || null }),
    );
    if (result) {
      setEditing(false);
      tool.reload();
    }
  }

  async function toggleEnabled() {
    if (!current) return;
    const result = await mutation.run((auth) => patchTool(auth, toolId, { enabled: !current.enabled }));
    if (result) tool.reload();
  }

  return (
    <div className="page">
      <header className="page-header">
        <p className="eyebrow">{t("tools.eyebrow")}</p>
        <h1>{current?.name ?? t("tools.detail")}</h1>
        <p className="page-lede">
          <code>{toolId}</code>
        </p>
      </header>

      <div className="page-toolbar">
        <Link className="button button-ghost" href="/tools">
          {t("tools.backToTools")}
        </Link>
      </div>

      <Panel
        ariaLabel={t("tools.overview")}
        title={t("tools.overview")}
        actions={
          current ? (
            <>
              <button
                type="button"
                className="button button-ghost"
                onClick={() => {
                  setEditing((value) => !value);
                  setName(current.name);
                  setDescription(current.description ?? "");
                }}
              >
                {t("common.edit")}
              </button>
              <button
                type="button"
                className="button button-ghost"
                onClick={() => void toggleEnabled()}
                disabled={mutation.pending}
              >
                {current.enabled ? t("common.disable") : t("common.enable")}
              </button>
            </>
          ) : null
        }
      >
        {tool.error && (
          <ErrorState
            code={tool.error.code}
            message={tool.error.message || t("errors.loadTool")}
            hint={errorHintKey(tool.error) ? t(errorHintKey(tool.error)!) : undefined}
            onRetry={tool.reload}
          />
        )}
        {tool.loading && !tool.error && <LoadingState />}

        {current && (
          <>
            <dl className="key-values">
              <div className="key-value-row">
                <dt>{t("tools.identity")}</dt>
                <dd>
                  <code>{current.identity ?? "—"}</code>
                </dd>
              </div>
              <div className="key-value-row">
                <dt>{t("tools.description")}</dt>
                <dd>{current.description || t("common.none")}</dd>
              </div>
              <div className="key-value-row">
                <dt>{t("tools.effect")}</dt>
                <dd>{current.effect ? <StatusBadge status={current.effect} /> : "—"}</dd>
              </div>
              <div className="key-value-row">
                <dt>{t("tools.risk")}</dt>
                <dd>{current.risk_level ? <StatusBadge status={current.risk_level} /> : "—"}</dd>
              </div>
              <div className="key-value-row">
                <dt>{t("tools.approval")}</dt>
                <dd>
                  <code>{current.approval_policy ?? "—"}</code>
                </dd>
              </div>
              <div className="key-value-row">
                <dt>{t("tools.kind")}</dt>
                <dd>
                  <code>{current.execution_kind ?? "—"}</code>
                </dd>
              </div>
              <div className="key-value-row">
                <dt>{t("tools.currentRevision")}</dt>
                <dd>
                  {current.current_revision_number != null ? `#${current.current_revision_number}` : "—"}
                </dd>
              </div>
              <div className="key-value-row">
                <dt>{t("tools.specHash")}</dt>
                <dd>
                  {current.current_spec_hash ? (
                    <code className="hash-value">{current.current_spec_hash}</code>
                  ) : (
                    "—"
                  )}
                </dd>
              </div>
              <div className="key-value-row">
                <dt>{t("settings.models.status")}</dt>
                <dd>
                  <StatusBadge
                    status={current.enabled ? "ENABLED" : "DISABLED"}
                    tone={current.enabled ? "success" : "neutral"}
                    label={current.enabled ? t("common.enabled") : t("common.disabled")}
                  />
                </dd>
              </div>
            </dl>

            {mutation.error && (
              <p className="session-error" role="alert">
                <code>{mutation.error.code}</code> {mutation.error.message || t("errors.requestFailed")}
              </p>
            )}

            {editing && (
              <form className="eval-form" onSubmit={submitEdit} noValidate>
                <p className="eval-form-title">{t("tools.editTool")}</p>
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
                <div className="form-actions">
                  <button
                    type="submit"
                    className="button button-primary"
                    disabled={mutation.pending || !name.trim()}
                  >
                    {t("common.save")}
                  </button>
                  <button type="button" className="button button-ghost" onClick={() => setEditing(false)}>
                    {t("session.cancel")}
                  </button>
                </div>
              </form>
            )}
          </>
        )}
      </Panel>

      <Panel ariaLabel={t("tools.revisions")} title={t("tools.revisions")}>
        {revisions.error && (
          <ErrorState
            code={revisions.error.code}
            message={revisions.error.message || t("errors.loadRevisions")}
            hint={errorHintKey(revisions.error) ? t(errorHintKey(revisions.error)!) : undefined}
            onRetry={revisions.reload}
          />
        )}
        {revisions.loading && !revisions.error && <LoadingState />}
        {revisions.loaded && !revisions.error && revisionList.length === 0 && (
          <EmptyState title={t("tools.noRevisions")} />
        )}

        {revisionList.length > 0 && (
          <div className="data-table">
            <table>
              <thead>
                <tr>
                  <th scope="col">{t("tools.revision")}</th>
                  <th scope="col">{t("tools.specHash")}</th>
                  <th scope="col">{t("common.created")}</th>
                  <th scope="col">{t("settings.models.actions")}</th>
                </tr>
              </thead>
              <tbody>
                {revisionList.map((revision) => (
                  <tr key={revision.id}>
                    <td data-label={t("tools.revision")}>#{revision.revision_number}</td>
                    <td data-label={t("tools.specHash")}>
                      <code className="hash-value">{revision.spec_hash}</code>
                    </td>
                    <td data-label={t("common.created")}>{formatDateTime(revision.created_at)}</td>
                    <td data-label={t("settings.models.actions")}>
                      <button
                        type="button"
                        className="button button-ghost"
                        onClick={() => setSelectedRevisionId(revision.id)}
                      >
                        {t("tools.viewSpec")}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {selectedRevision && (
          <>
            <p className="eval-form-title">
              {t("tools.inputSchema")} · #{selectedRevision.revision_number}
            </p>
            <TechnicalDetails
              summary={t("tools.inputSchema")}
              value={selectedRevision.spec?.input_schema ?? null}
            />
            <TechnicalDetails summary={t("tools.fullSpec")} value={selectedRevision.spec} />
          </>
        )}
      </Panel>
    </div>
  );
}
