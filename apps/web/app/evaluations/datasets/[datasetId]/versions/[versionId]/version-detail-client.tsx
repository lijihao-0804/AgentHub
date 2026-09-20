"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import HashValue, { InlineConfirm } from "../../../../../../components/evaluation/hash-value";
import StatusBadge from "../../../../../../components/status-badge";
import { EmptyState, ErrorState, LoadingState, Panel, SessionRequired } from "../../../../../../components/states";
import TechnicalDetails from "../../../../../../components/technical-details";
import { ApiError, toApiError } from "../../../../../../lib/api-client";
import {
  EvaluationDatasetItem,
  EvaluationDatasetVersionDetail,
  getDatasetVersion,
  publishDatasetVersion,
} from "../../../../../../lib/evaluation";
import { useFrontendSession } from "../../../../../../components/session-provider";
import { useI18n } from "../../../../../../i18n/provider";

function inputSummary(item: EvaluationDatasetItem): string {
  const input = item.input ?? {};
  const values = Object.entries(input)
    .filter(([, value]) => typeof value === "string" || typeof value === "number")
    .slice(0, 3)
    .map(([key, value]) => `${key}: ${String(value)}`);
  return values.join(" · ") || "—";
}

export default function DatasetVersionDetailClient({
  datasetId,
  versionId,
}: {
  datasetId: string;
  versionId: string;
}) {
  const { t, formatDateTime } = useI18n();
  const { workspaceId, accessToken, connected, sessionId } = useFrontendSession();
  const [version, setVersion] = useState<EvaluationDatasetVersionDetail | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);

  const [showPublishConfirm, setShowPublishConfirm] = useState(false);
  const [publishing, setPublishing] = useState(false);
  const [publishError, setPublishError] = useState<string | null>(null);
  const [publishedNotice, setPublishedNotice] = useState(false);
  const activeSessionRef = useRef(sessionId);
  activeSessionRef.current = sessionId;

  useEffect(() => {
    setVersion(null);
    setError(null);
    setLoading(false);
    setLoaded(false);
    setShowPublishConfirm(false);
    setPublishing(false);
    setPublishError(null);
    setPublishedNotice(false);
  }, [sessionId]);

  const input = { workspaceId, accessToken };

  const load = useCallback(async () => {
    setError(null);
    if (!connected) return;
    const requestSessionId = sessionId;
    setLoading(true);
    try {
      const nextVersion = await getDatasetVersion(input, datasetId, versionId);
      if (activeSessionRef.current !== requestSessionId) return;
      setVersion(nextVersion);
      setLoaded(true);
    } catch (caught) {
      if (activeSessionRef.current === requestSessionId) setError(toApiError(caught, ""));
    } finally {
      if (activeSessionRef.current === requestSessionId) setLoading(false);
    }
  }, [connected, workspaceId, accessToken, datasetId, versionId, sessionId]);

  useEffect(() => {
    if (connected && !loaded) void load();
  }, [connected, loaded, load]);

  async function publish() {
    setPublishError(null);
    const requestSessionId = sessionId;
    setPublishing(true);
    try {
      const next = await publishDatasetVersion(input, datasetId, versionId);
      if (activeSessionRef.current !== requestSessionId) return;
      setVersion((current) => (current ? { ...current, ...next } : current));
      setShowPublishConfirm(false);
      setPublishedNotice(true);
    } catch (caught) {
      const apiError = toApiError(caught, "");
      if (activeSessionRef.current === requestSessionId) setPublishError(apiError.message || apiError.code);
    } finally {
      if (activeSessionRef.current === requestSessionId) setPublishing(false);
    }
  }

  if (!connected) {
    return (
      <div className="page">
        <header className="page-header">
          <p className="eyebrow">{t("evaluation.datasets.version.eyebrow")}</p>
        </header>
        <SessionRequired contextKey="session.context.evaluations" />
      </div>
    );
  }

  const isPublished = version?.status === "PUBLISHED";

  return (
    <div className="page">
      <header className="page-header">
        <p className="eyebrow">{t("evaluation.datasets.version.eyebrow")}</p>
        <h1>
          {t("evaluation.datasets.version.title", {
            number: version?.version_number ?? "",
          })}{" "}
          {version && <StatusBadge status={version.status} />}
          {isPublished && <span className="immutable-tag">{t("evaluation.immutable")}</span>}
        </h1>
        <p className="page-lede">
          <Link href={`/evaluations/datasets/${encodeURIComponent(datasetId)}`}>
            {t("evaluation.overview.datasets")} /
          </Link>{" "}
          <code>{datasetId}</code>
        </p>
      </header>

      {error && (
        <ErrorState code={error.code} message={error.message || t("errors.loadEvaluation")} onRetry={() => void load()} />
      )}
      {loading && !version && !error && <LoadingState />}
      {loaded && !version && !error && (
        <EmptyState title={t("evaluation.datasets.version.notFound")} hint={t("evaluation.datasets.version.notFoundHint")} />
      )}

      {version && (
        <>
          <Panel ariaLabel={t("evaluation.datasets.version.eyebrow")}>
            <div className="run-facts">
              <span>{t("evaluation.datasets.version.facts.status")}<strong><StatusBadge status={version.status} /></strong></span>
              <span>{t("evaluation.datasets.version.facts.schemaVersion")}<strong>{version.schema_version}</strong></span>
              <span>{t("evaluation.datasets.version.facts.itemCount")}<strong>{version.item_count ?? version.items.length}</strong></span>
              <span>{t("evaluation.datasets.version.facts.created")}<strong>{formatDateTime(version.created_at)}</strong></span>
              <span>
                {t("evaluation.datasets.version.facts.publishedAt")}
                <strong>{version.published_at ? formatDateTime(version.published_at) : "—"}</strong>
              </span>
              <span>
                {t("evaluation.datasets.version.facts.contentHash")}
                <strong><HashValue value={version.content_hash} label={t("evaluation.datasets.version.facts.contentHash")} /></strong>
              </span>
            </div>
            {!isPublished && !showPublishConfirm && (
              <div className="form-actions">
                <button type="button" className="button button-primary" onClick={() => setShowPublishConfirm(true)}>
                  {t("evaluation.datasets.version.publish")}
                </button>
              </div>
            )}
            {showPublishConfirm && (
              <InlineConfirm
                title={t("evaluation.datasets.version.publishConfirmTitle")}
                text={t("evaluation.datasets.version.publishConfirmText")}
                confirmLabel={t("evaluation.datasets.version.publish")}
                pendingLabel={t("evaluation.datasets.version.publishing")}
                cancelLabel={t("evaluation.datasets.version.cancel")}
                onConfirm={() => void publish()}
                onCancel={() => setShowPublishConfirm(false)}
                pending={publishing}
              />
            )}
            {publishError && <p className="session-error" role="alert">{publishError}</p>}
            {publishedNotice && <p className="inline-notice">{t("evaluation.datasets.version.publishedNotice")}</p>}
          </Panel>

          <Panel title={t("evaluation.datasets.version.itemsTitle")} eyebrow={t("evaluation.datasets.eyebrow")}>
            {version.items.length === 0 ? (
              <EmptyState title={t("evaluation.datasets.versions.empty")} />
            ) : (
              <div className="data-table">
                <table>
                  <thead>
                    <tr>
                      <th scope="col">{t("evaluation.datasets.version.itemColumns.category")}</th>
                      <th scope="col">{t("evaluation.datasets.version.itemColumns.split")}</th>
                      <th scope="col">{t("evaluation.datasets.version.itemColumns.ordinal")}</th>
                      <th scope="col">{t("evaluation.datasets.version.itemColumns.caseKey")}</th>
                      <th scope="col">{t("evaluation.datasets.version.itemColumns.input")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {version.items
                      .slice()
                      .sort((a, b) => a.ordinal - b.ordinal)
                      .map((item) => {
                        const holdout = item.split === "HOLDOUT";
                        return (
                          <tr key={item.id} className={holdout ? "holdout-row" : undefined}>
                            <td data-label={t("evaluation.datasets.version.itemColumns.category")}>{item.category}</td>
                            <td data-label={t("evaluation.datasets.version.itemColumns.split")}>{item.split}</td>
                            <td data-label={t("evaluation.datasets.version.itemColumns.ordinal")}>{item.ordinal}</td>
                            <td data-label={t("evaluation.datasets.version.itemColumns.caseKey")}>
                              <code>{item.case_key}</code>
                            </td>
                            <td data-label={t("evaluation.datasets.version.itemColumns.input")}>
                              <span className="muted">{inputSummary(item)}</span>
                              {holdout ? (
                                <p className="state-hint">{t("evaluation.datasets.version.expectedHidden")}</p>
                              ) : (
                                <TechnicalDetails
                                  summary={t("evaluation.datasets.version.expectedTitle")}
                                  value={item.expected}
                                />
                              )}
                            </td>
                          </tr>
                        );
                      })}
                  </tbody>
                </table>
              </div>
            )}
            {/* Holdout note: if the API already ships holdout expected payloads to the browser,
                hiding them here is presentation only — server-side redaction is the real boundary. */}
          </Panel>
        </>
      )}
    </div>
  );
}
