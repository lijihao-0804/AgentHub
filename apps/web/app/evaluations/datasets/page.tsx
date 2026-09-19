"use client";

import Link from "next/link";
import { useCallback, useEffect, useState, type FormEvent } from "react";

import HashValue from "../../../components/evaluation/hash-value";
import { EmptyState, ErrorState, LoadingState, Panel, SessionRequired } from "../../../components/states";
import { ApiError, toApiError } from "../../../lib/api-client";
import { EvaluationDataset, createDataset, listDatasets } from "../../../lib/evaluation";
import { useFrontendSession } from "../../../components/session-provider";
import { useI18n } from "../../../i18n/provider";

export default function EvaluationDatasetsPage() {
  const { t, formatDateTime } = useI18n();
  const { workspaceId, accessToken, connected } = useFrontendSession();
  const [datasets, setDatasets] = useState<EvaluationDataset[]>([]);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);

  const [showForm, setShowForm] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [createdNotice, setCreatedNotice] = useState(false);

  const refresh = useCallback(async () => {
    setError(null);
    if (!connected) return;
    setLoading(true);
    try {
      setDatasets(await listDatasets({ workspaceId, accessToken }));
      setLoaded(true);
    } catch (caught) {
      setError(toApiError(caught, ""));
    } finally {
      setLoading(false);
    }
  }, [connected, workspaceId, accessToken]);

  useEffect(() => {
    if (connected && !loaded) void refresh();
  }, [connected, loaded, refresh]);

  useEffect(() => {
    if (!connected) setLoaded(false);
  }, [connected]);

  async function submitCreate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setCreateError(null);
    if (!name.trim()) return;
    setCreating(true);
    try {
      await createDataset({ workspaceId, accessToken }, { name: name.trim(), description: description.trim() || null });
      setName("");
      setDescription("");
      setShowForm(false);
      setCreatedNotice(true);
      await refresh();
    } catch (caught) {
      const apiError = toApiError(caught, "");
      setCreateError(apiError.message || apiError.code);
    } finally {
      setCreating(false);
    }
  }

  if (!connected) {
    return (
      <div className="page">
        <header className="page-header">
          <p className="eyebrow">{t("evaluation.datasets.eyebrow")}</p>
          <h1>{t("evaluation.datasets.title")}</h1>
        </header>
        <SessionRequired contextKey="session.context.evaluations" />
      </div>
    );
  }

  return (
    <div className="page">
      <header className="page-header">
        <p className="eyebrow">{t("evaluation.datasets.eyebrow")}</p>
        <h1>{t("evaluation.datasets.title")}</h1>
        <p className="page-lede">{t("evaluation.datasets.lede")}</p>
      </header>

      <Panel
        ariaLabel={t("evaluation.datasets.title")}
        title={t("evaluation.datasets.title")}
        eyebrow={t("evaluation.datasets.eyebrow")}
        actions={
          <button type="button" className="button button-primary" onClick={() => setShowForm((v) => !v)}>
            {t("evaluation.datasets.create")}
          </button>
        }
      >
        {createdNotice && <p className="inline-notice">{t("evaluation.datasets.createdNotice")}</p>}

        {showForm && (
          <form className="eval-form" onSubmit={submitCreate} noValidate>
            <label>
              {t("evaluation.datasets.nameLabel")}
              <input required value={name} onChange={(e) => setName(e.target.value)} spellCheck={false} />
            </label>
            <label>
              {t("evaluation.datasets.descriptionLabel")}
              <textarea
                rows={2}
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder={t("evaluation.datasets.descriptionPlaceholder")}
              />
            </label>
            {createError && <p className="session-error" role="alert">{createError}</p>}
            <div className="form-actions">
              <button type="submit" className="button button-primary" disabled={creating || !name.trim()}>
                {creating ? t("evaluation.datasets.creating") : t("evaluation.datasets.create")}
              </button>
            </div>
          </form>
        )}

        {error && (
          <ErrorState code={error.code} message={error.message || t("errors.loadEvaluation")} onRetry={() => void refresh()} />
        )}
        {loading && datasets.length === 0 && !error && <LoadingState />}
        {loaded && datasets.length === 0 && !error && (
          <EmptyState title={t("evaluation.datasets.empty")} hint={t("evaluation.datasets.emptyHint")} />
        )}

        {datasets.length > 0 && (
          <div className="data-table">
            <table>
              <thead>
                <tr>
                  <th scope="col">{t("evaluation.datasets.columns.name")}</th>
                  <th scope="col">{t("evaluation.datasets.columns.description")}</th>
                  <th scope="col">{t("evaluation.datasets.columns.created")}</th>
                  <th scope="col">{t("evaluation.datasets.columns.id")}</th>
                </tr>
              </thead>
              <tbody>
                {datasets.map((dataset) => (
                  <tr key={dataset.id}>
                    <td data-label={t("evaluation.datasets.columns.name")}>
                      <Link href={`/evaluations/datasets/${encodeURIComponent(dataset.id)}`}>{dataset.name}</Link>
                    </td>
                    <td data-label={t("evaluation.datasets.columns.description")} className="muted">
                      {dataset.description ?? "—"}
                    </td>
                    <td data-label={t("evaluation.datasets.columns.created")}>{formatDateTime(dataset.created_at)}</td>
                    <td data-label={t("evaluation.datasets.columns.id")}>
                      <HashValue value={dataset.id} label={t("evaluation.datasets.columns.id")} />
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
