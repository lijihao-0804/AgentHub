"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";

import StatusBadge from "../../../components/status-badge";
import { EmptyState, ErrorState, LoadingState, Panel, SessionRequired } from "../../../components/states";
import { ApiError, toApiError } from "../../../lib/api-client";
import {
  EvaluationDataset,
  EvaluationDatasetVersion,
  EvaluationExperiment,
  createExperiment,
  listDatasets,
  listDatasetVersions,
  listExperiments,
} from "../../../lib/evaluation";
import { useFrontendSession } from "../../../components/session-provider";
import { useI18n } from "../../../i18n/provider";

const PURPOSES = ["DEVELOPMENT", "HOLDOUT_VALIDATION", "RELEASE_GATE"] as const;

/** Backend contract coupling; the backend remains the final authority. */
function splitForPurpose(purpose: string): string {
  return purpose === "DEVELOPMENT" ? "DEV" : "HOLDOUT";
}

export default function EvaluationExperimentsPage() {
  const { t, statusLabel, purposeLabel, formatDateTime } = useI18n();
  const { workspaceId, accessToken, connected, sessionId } = useFrontendSession();
  const [experiments, setExperiments] = useState<EvaluationExperiment[]>([]);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);

  const [showForm, setShowForm] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [datasets, setDatasets] = useState<EvaluationDataset[]>([]);
  const [datasetsLoaded, setDatasetsLoaded] = useState(false);
  const [datasetsLoading, setDatasetsLoading] = useState(false);
  const [datasetsError, setDatasetsError] = useState<ApiError | null>(null);
  const [datasetId, setDatasetId] = useState("");
  const [versions, setVersions] = useState<EvaluationDatasetVersion[]>([]);
  const [versionsLoading, setVersionsLoading] = useState(false);
  const [versionsError, setVersionsError] = useState<ApiError | null>(null);
  const [versionId, setVersionId] = useState("");
  const [purpose, setPurpose] = useState<(typeof PURPOSES)[number]>("DEVELOPMENT");
  const [repetitions, setRepetitions] = useState("1");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [createdNotice, setCreatedNotice] = useState(false);
  const activeSessionRef = useRef(sessionId);
  const versionsRequestGenerationRef = useRef(0);
  activeSessionRef.current = sessionId;

  useEffect(() => {
    setExperiments([]);
    setError(null);
    setLoading(false);
    setLoaded(false);
    setShowForm(false);
    setName("");
    setDescription("");
    setDatasets([]);
    setDatasetsLoaded(false);
    setDatasetsLoading(false);
    setDatasetsError(null);
    setDatasetId("");
    setVersions([]);
    setVersionsLoading(false);
    setVersionsError(null);
    setVersionId("");
    setPurpose("DEVELOPMENT");
    setRepetitions("1");
    setCreating(false);
    setCreateError(null);
    setCreatedNotice(false);
  }, [sessionId]);

  const input = { workspaceId, accessToken };

  const refresh = useCallback(async () => {
    setError(null);
    if (!connected) return;
    const requestSessionId = sessionId;
    setLoading(true);
    try {
      const nextExperiments = await listExperiments(input);
      if (activeSessionRef.current !== requestSessionId) return;
      setExperiments(nextExperiments);
      setLoaded(true);
    } catch (caught) {
      if (activeSessionRef.current === requestSessionId) setError(toApiError(caught, ""));
    } finally {
      if (activeSessionRef.current === requestSessionId) setLoading(false);
    }
  }, [connected, workspaceId, accessToken, sessionId]);

  useEffect(() => {
    if (connected && !loaded) void refresh();
  }, [connected, loaded, refresh]);

  const loadDatasets = useCallback(async () => {
    if (!connected) return;
    const requestSessionId = sessionId;
    setDatasetsLoading(true);
    setDatasetsError(null);
    try {
      const nextDatasets = await listDatasets(input);
      if (activeSessionRef.current !== requestSessionId) return;
      setDatasets(nextDatasets);
      setDatasetsLoaded(true);
    } catch (caught) {
      if (activeSessionRef.current === requestSessionId) setDatasetsError(toApiError(caught, ""));
    } finally {
      if (activeSessionRef.current === requestSessionId) setDatasetsLoading(false);
    }
  }, [connected, workspaceId, accessToken, sessionId]);

  useEffect(() => {
    if (connected && showForm && !datasetsLoaded) void loadDatasets();
  }, [connected, showForm, datasetsLoaded, loadDatasets]);

  const loadVersions = useCallback(async () => {
    const requestGeneration = versionsRequestGenerationRef.current + 1;
    versionsRequestGenerationRef.current = requestGeneration;
    if (!datasetId) {
      setVersions([]);
      setVersionId("");
      return;
    }
    const requestSessionId = sessionId;
    const isCurrentRequest = () =>
      activeSessionRef.current === requestSessionId && versionsRequestGenerationRef.current === requestGeneration;
    setVersionsLoading(true);
    setVersionsError(null);
    setVersionId("");
    try {
      const all = await listDatasetVersions(input, datasetId);
      if (!isCurrentRequest()) return;
      setVersions(all.filter((v) => v.status === "PUBLISHED"));
    } catch (caught) {
      if (isCurrentRequest()) setVersionsError(toApiError(caught, ""));
    } finally {
      if (isCurrentRequest()) setVersionsLoading(false);
    }
  }, [connected, workspaceId, accessToken, datasetId, sessionId]);

  useEffect(() => {
    void loadVersions();
  }, [loadVersions]);

  async function submitCreate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setCreateError(null);
    if (!name.trim() || !versionId) return;
    const requestSessionId = sessionId;
    setCreating(true);
    try {
      const experiment = await createExperiment(input, {
        name: name.trim(),
        description: description.trim() || null,
        dataset_version_id: versionId,
        split: splitForPurpose(purpose),
        purpose,
        repetitions: Number(repetitions) || 1,
      });
      if (activeSessionRef.current !== requestSessionId) return;
      setName("");
      setDescription("");
      setShowForm(false);
      setCreatedNotice(true);
      await refresh();
      window.location.assign(`/evaluations/experiments/${encodeURIComponent(experiment.id)}`);
    } catch (caught) {
      const apiError = toApiError(caught, "");
      if (activeSessionRef.current === requestSessionId) setCreateError(apiError.message || apiError.code);
    } finally {
      if (activeSessionRef.current === requestSessionId) setCreating(false);
    }
  }

  if (!connected) {
    return (
      <div className="page">
        <header className="page-header">
          <p className="eyebrow">{t("evaluation.experiments.eyebrow")}</p>
          <h1>{t("evaluation.experiments.title")}</h1>
        </header>
        <SessionRequired contextKey="session.context.evaluations" />
      </div>
    );
  }

  return (
    <div className="page">
      <header className="page-header">
        <p className="eyebrow">{t("evaluation.experiments.eyebrow")}</p>
        <h1>{t("evaluation.experiments.title")}</h1>
        <p className="page-lede">{t("evaluation.experiments.lede")}</p>
      </header>

      <Panel
        title={t("evaluation.experiments.title")}
        eyebrow={t("evaluation.experiments.eyebrow")}
        actions={
          <button type="button" className="button button-primary" onClick={() => setShowForm((v) => !v)}>
            {t("evaluation.experiments.create")}
          </button>
        }
      >
        {createdNotice && <p className="inline-notice">{t("evaluation.experiments.createdNotice")}</p>}

        {showForm && (
          <form className="eval-form" onSubmit={submitCreate} noValidate>
            <div className="eval-form-title">{t("evaluation.experiments.formTitle")}</div>
            {datasetsError && (
              <ErrorState
                code={datasetsError.code}
                message={datasetsError.message || t("errors.loadEvaluation")}
                onRetry={() => void loadDatasets()}
              />
            )}
            {datasetsLoading && !datasetsError && <LoadingState label={t("common.loading")} />}
            <div className="form-grid">
              <label>
                {t("evaluation.experiments.labels.name")}
                <input required value={name} onChange={(e) => setName(e.target.value)} spellCheck={false} />
              </label>
              <label>
                {t("evaluation.experiments.labels.datasetVersion")} · {t("evaluation.experiments.datasetSelector.dataset")}
                <select value={datasetId} onChange={(e) => setDatasetId(e.target.value)}>
                  <option value="">—</option>
                  {datasets.map((dataset) => (
                    <option value={dataset.id} key={dataset.id}>
                      {dataset.name}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                {t("evaluation.experiments.datasetSelector.version")}
                {versionsError ? (
                  <ErrorState
                    code={versionsError.code}
                    message={versionsError.message || t("errors.loadEvaluation")}
                    onRetry={() => void loadVersions()}
                  />
                ) : versionsLoading ? (
                  <span className="state-hint">{t("evaluation.experiments.datasetSelector.loadingVersions")}</span>
                ) : (
                  <select required value={versionId} onChange={(e) => setVersionId(e.target.value)} disabled={!datasetId}>
                    <option value="">—</option>
                    {versions.length === 0 && datasetId && !versionsError ? (
                      <option value="">{t("evaluation.experiments.datasetSelector.noPublished")}</option>
                    ) : (
                      versions.map((version) => (
                        <option value={version.id} key={version.id}>
                          v{version.version_number} · {version.item_count ?? "?"} items
                        </option>
                      ))
                    )}
                  </select>
                )}
                <span className="state-hint">{t("evaluation.experiments.datasetSelector.publishedOnly")}</span>
              </label>
              <label>
                {t("evaluation.experiments.labels.purpose")}
                <select value={purpose} onChange={(e) => setPurpose(e.target.value as (typeof PURPOSES)[number])}>
                  {PURPOSES.map((value) => (
                    <option value={value} key={value}>
                      {purposeLabel(value)}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                {t("evaluation.experiments.labels.split")}
                <input value={splitForPurpose(purpose)} readOnly />
              </label>
              <label>
                {t("evaluation.experiments.labels.repetitions")}
                <select value={repetitions} onChange={(e) => setRepetitions(e.target.value)}>
                  {[1, 2, 3, 4, 5].map((n) => (
                    <option value={n} key={n}>
                      {n}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                {t("evaluation.experiments.labels.description")}
                <textarea rows={2} value={description} onChange={(e) => setDescription(e.target.value)} />
              </label>
            </div>
            <p className="state-hint">{t("evaluation.experiments.purposeHint")}</p>
            {createError && <p className="session-error" role="alert">{createError}</p>}
            <div className="form-actions">
              <button type="submit" className="button button-primary" disabled={creating || !name.trim() || !versionId}>
                {creating ? t("evaluation.experiments.creating") : t("evaluation.experiments.create")}
              </button>
            </div>
          </form>
        )}

        {error && (
          <ErrorState code={error.code} message={error.message || t("errors.loadEvaluation")} onRetry={() => void refresh()} />
        )}
        {loading && experiments.length === 0 && !error && <LoadingState />}
        {loaded && experiments.length === 0 && !error && (
          <EmptyState title={t("evaluation.experiments.empty")} hint={t("evaluation.experiments.emptyHint")} />
        )}

        {experiments.length > 0 && (
          <div className="data-table">
            <table>
              <thead>
                <tr>
                  <th scope="col">{t("evaluation.experiments.columns.name")}</th>
                  <th scope="col">{t("evaluation.experiments.columns.status")}</th>
                  <th scope="col">{t("evaluation.experiments.columns.split")}</th>
                  <th scope="col">{t("evaluation.experiments.columns.purpose")}</th>
                  <th scope="col">{t("evaluation.experiments.columns.repetitions")}</th>
                  <th scope="col">{t("evaluation.experiments.columns.build")}</th>
                  <th scope="col">{t("evaluation.experiments.columns.holdoutExposure")}</th>
                  <th scope="col">{t("evaluation.experiments.columns.created")}</th>
                </tr>
              </thead>
              <tbody>
                {experiments.map((experiment) => (
                  <tr key={experiment.id}>
                    <td data-label={t("evaluation.experiments.columns.name")}>
                      <Link href={`/evaluations/experiments/${encodeURIComponent(experiment.id)}`}>{experiment.name}</Link>
                    </td>
                    <td data-label={t("evaluation.experiments.columns.status")}>
                      <StatusBadge status={experiment.status} />
                    </td>
                    <td data-label={t("evaluation.experiments.columns.split")}>{experiment.split}</td>
                    <td data-label={t("evaluation.experiments.columns.purpose")}>
                      {purposeLabel(experiment.purpose)}
                    </td>
                    <td data-label={t("evaluation.experiments.columns.repetitions")}>{experiment.repetitions}</td>
                    <td data-label={t("evaluation.experiments.columns.build")}>
                      <code>{experiment.build_sha.slice(0, 7)}</code>
                    </td>
                    <td data-label={t("evaluation.experiments.columns.holdoutExposure")}>
                      {experiment.holdout_exposure_count ?? "—"}
                    </td>
                    <td data-label={t("evaluation.experiments.columns.created")}>{formatDateTime(experiment.created_at)}</td>
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
