"use client";

import Link from "next/link";
import { useCallback, useEffect, useState, type FormEvent } from "react";

import { EmptyState, ErrorState, LoadingState, Panel, SessionRequired } from "../../components/states";
import { useFrontendSession } from "../../components/session-provider";
import { useWorkspaceData, useWorkspaceMutation } from "../../components/use-workspace-data";
import { errorHintKey, type AuthInput } from "../../lib/api-client";
import { createKnowledgeBase, listKnowledgeBases, type KnowledgeBase } from "../../lib/knowledge";
import { useI18n } from "../../i18n/provider";

export default function KnowledgeBasesPage() {
  const { t, formatDateTime } = useI18n();
  const { connected, sessionId, workspaceId } = useFrontendSession();

  const load = useCallback((auth: AuthInput) => listKnowledgeBases(auth), []);
  const bases = useWorkspaceData<KnowledgeBase[]>(load, `knowledge-bases:${workspaceId}`);
  const mutation = useWorkspaceMutation(`knowledge-bases:${workspaceId}`);

  const [name, setName] = useState("");
  const [showForm, setShowForm] = useState(false);

  useEffect(() => {
    setName("");
    setShowForm(false);
  }, [sessionId]);

  if (!connected) {
    return (
      <div className="page">
        <header className="page-header">
          <p className="eyebrow">{t("knowledge.eyebrow")}</p>
          <h1>{t("knowledge.title")}</h1>
        </header>
        <SessionRequired contextKey="session.context.knowledge" />
      </div>
    );
  }

  const list = bases.data ?? [];

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const result = await mutation.run((auth) => createKnowledgeBase(auth, { name: name.trim() }));
    if (result) {
      setName("");
      setShowForm(false);
      bases.reload();
    }
  }

  return (
    <div className="page">
      <header className="page-header">
        <p className="eyebrow">{t("knowledge.eyebrow")}</p>
        <h1>{t("knowledge.title")}</h1>
        <p className="page-lede">{t("knowledge.lede")}</p>
      </header>

      <div className="page-toolbar">
        <Link className="button button-ghost" href="/knowledge/playground">
          {t("knowledge.openPlayground")}
        </Link>
      </div>

      <Panel
        ariaLabel={t("knowledge.bases")}
        title={t("knowledge.bases")}
        actions={
          <button
            type="button"
            className="button button-primary"
            onClick={() => {
              setShowForm((value) => !value);
              setName("");
            }}
          >
            {t("knowledge.createBase")}
          </button>
        }
      >
        {/* Documents live inside a knowledge base; say so where the list is. */}
        <p className="state-hint">{t("knowledge.uploadLocationHint")}</p>

        {showForm && (
          <form className="eval-form" onSubmit={submit} noValidate>
            <p className="eval-form-title">{t("knowledge.createBase")}</p>
            <label>
              {t("knowledge.baseName")}
              <input value={name} onChange={(event) => setName(event.target.value)} maxLength={200} />
            </label>
            {mutation.error && (
              <p className="session-error" role="alert">
                <code>{mutation.error.code}</code> {mutation.error.message || t("errors.requestFailed")}
              </p>
            )}
            <div className="form-actions">
              <button
                type="submit"
                className="button button-primary"
                disabled={mutation.pending || !name.trim()}
              >
                {t("knowledge.createBase")}
              </button>
              <button type="button" className="button button-ghost" onClick={() => setShowForm(false)}>
                {t("session.cancel")}
              </button>
            </div>
          </form>
        )}

        {bases.error && (
          <ErrorState
            code={bases.error.code}
            message={bases.error.message || t("errors.loadKnowledgeBases")}
            hint={errorHintKey(bases.error) ? t(errorHintKey(bases.error)!) : undefined}
            onRetry={bases.reload}
          />
        )}
        {bases.loading && !bases.error && <LoadingState />}
        {bases.loaded && !bases.error && list.length === 0 && (
          <EmptyState title={t("knowledge.noBases")} hint={t("knowledge.noBasesHint")} />
        )}

        {list.length > 0 && (
          <div className="data-table">
            <table>
              <thead>
                <tr>
                  <th scope="col">{t("knowledge.baseName")}</th>
                  <th scope="col">{t("common.id")}</th>
                  <th scope="col">{t("common.created")}</th>
                  <th scope="col">{t("common.actions")}</th>
                </tr>
              </thead>
              <tbody>
                {list.map((base) => (
                  <tr key={base.id}>
                    <td data-label={t("knowledge.baseName")}>
                      <Link href={`/knowledge/${base.id}`}>{base.name}</Link>
                    </td>
                    <td data-label={t("common.id")}>
                      <code>{base.id}</code>
                    </td>
                    <td data-label={t("common.created")}>
                      {base.created_at ? formatDateTime(base.created_at) : t("common.none")}
                    </td>
                    <td data-label={t("common.actions")}>
                      <Link className="button button-ghost" href={`/knowledge/${base.id}`}>
                        {t("knowledge.manageDocuments")}
                      </Link>
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
