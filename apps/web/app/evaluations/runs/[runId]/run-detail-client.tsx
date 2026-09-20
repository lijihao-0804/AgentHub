"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import HashValue from "@/components/evaluation/hash-value";
import StatusBadge from "@/components/ui/status-badge";
import Breadcrumbs from "@/components/layout/breadcrumbs";
import { EmptyState, ErrorState, InlineError, LoadingState, Panel, SessionRequired } from "@/components/ui/states";
import { ApiError, toApiError } from "@/lib/api/client";
import {
  CANCELLABLE_RUN_STATUSES,
  EvaluationExperimentDetail,
  EvaluationExperimentRun,
  EvaluationExperimentRunProgress,
  TERMINAL_RUN_STATUSES,
  cancelExperimentRun,
  getExperiment,
  getExperimentRun,
  getExperimentRunProgress,
} from "@/lib/api/evaluation";
import { useFrontendSession } from "@/components/providers/session-provider";
import { useI18n } from "@/i18n/provider";
import RunWorkflow from "@/app/evaluations/runs/[runId]/workflow-sections";

const POLL_INTERVAL_MS = 2500;

export default function RunDetailClient({ runId }: { runId: string }) {
  const { t, statusLabel, purposeLabel, formatDateTime, formatNumber } = useI18n();
  const { workspaceId, accessToken, connected, sessionId } = useFrontendSession();
  const inputRef = useRef({ workspaceId, accessToken });
  inputRef.current = { workspaceId, accessToken };
  const activeSessionRef = useRef(sessionId);
  activeSessionRef.current = sessionId;

  const [run, setRun] = useState<EvaluationExperimentRun | null>(null);
  const [progress, setProgress] = useState<EvaluationExperimentRunProgress | null>(null);
  const [experiment, setExperiment] = useState<EvaluationExperimentDetail | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [experimentError, setExperimentError] = useState<ApiError | null>(null);
  const [cancelling, setCancelling] = useState(false);
  const [cancelError, setCancelError] = useState<ApiError | null>(null);

  useEffect(() => {
    setRun(null);
    setProgress(null);
    setExperiment(null);
    setError(null);
    setExperimentError(null);
    setLoading(false);
    setLoaded(false);
    setCancelling(false);
    setCancelError(null);
  }, [sessionId]);

  const load = useCallback(async () => {
    setError(null);
    if (!connected) return;
    const requestSessionId = sessionId;
    setLoading(true);
    try {
      const nextRun = await getExperimentRun({ workspaceId, accessToken }, runId);
      if (activeSessionRef.current !== requestSessionId) return;
      setRun(nextRun);
      if (CANCELLABLE_RUN_STATUSES.has(nextRun.status)) {
        const nextProgress = await getExperimentRunProgress({ workspaceId, accessToken }, runId);
        if (activeSessionRef.current !== requestSessionId) return;
        setProgress(nextProgress);
      } else {
        setProgress(null);
      }
      setLoaded(true);
    } catch (caught) {
      if (activeSessionRef.current === requestSessionId) setError(toApiError(caught, ""));
    } finally {
      if (activeSessionRef.current === requestSessionId) setLoading(false);
    }
  }, [connected, workspaceId, accessToken, runId, sessionId]);

  // Initial load + experiment metadata (variant labels for the workflow).
  useEffect(() => {
    if (connected && !loaded) void load();
  }, [connected, loaded, load]);

  useEffect(() => {
    if (!connected || !run) return;
    const requestSessionId = sessionId;
    setExperimentError(null);
    getExperiment({ workspaceId, accessToken }, run.experiment_id)
      .then((nextExperiment) => {
        if (activeSessionRef.current === requestSessionId) setExperiment(nextExperiment);
      })
      .catch((caught) => {
        if (activeSessionRef.current === requestSessionId) setExperimentError(toApiError(caught, ""));
      });
  }, [connected, run, workspaceId, accessToken, sessionId]);

  const active = run !== null && !TERMINAL_RUN_STATUSES.has(run.status);

  // Poll run + progress while the run is active and the page is visible.
  useEffect(() => {
    if (!connected || !active) return;
    let stopped = false;
    const requestSessionId = sessionId;
    const poll = async () => {
      if (document.hidden || stopped) return;
      try {
        const nextRun = await getExperimentRun(inputRef.current, runId);
        if (stopped || activeSessionRef.current !== requestSessionId) return;
        setRun(nextRun);
        if (CANCELLABLE_RUN_STATUSES.has(nextRun.status)) {
          const nextProgress = await getExperimentRunProgress(inputRef.current, runId);
          if (stopped || activeSessionRef.current !== requestSessionId) return;
          setProgress(nextProgress);
        } else {
          setProgress(null);
        }
      } catch {
        // Transient polling errors are tolerated; the next tick retries.
      }
    };
    const interval = window.setInterval(() => void poll(), POLL_INTERVAL_MS);
    const onVisible = () => {
      if (!document.hidden) void poll();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      stopped = true;
      window.clearInterval(interval);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [connected, active, runId, sessionId]);

  async function requestCancel() {
    setCancelError(null);
    const requestSessionId = sessionId;
    setCancelling(true);
    try {
      const nextRun = await cancelExperimentRun({ workspaceId, accessToken }, runId);
      if (activeSessionRef.current !== requestSessionId) return;
      setRun(nextRun);
    } catch (caught) {
      const apiError = toApiError(caught, "");
      if (activeSessionRef.current === requestSessionId) setCancelError(apiError);
    } finally {
      if (activeSessionRef.current === requestSessionId) setCancelling(false);
    }
  }

  if (!connected) {
    return (
      <div className="page">
        <header className="page-header">
          <p className="eyebrow">{t("evaluation.run.eyebrow")}</p>
          <h1>
            {t("evaluation.run.title")} <code>{runId.slice(0, 8)}…</code>
          </h1>
        </header>
        <SessionRequired contextKey="session.context.evaluations" />
      </div>
    );
  }

  const terminal = run !== null && TERMINAL_RUN_STATUSES.has(run.status);
  const cancelRequested = run?.status === "CANCEL_REQUESTED";

  return (
    <div className="page">
      <Breadcrumbs
        items={[
          { label: t("nav.evaluations"), href: "/evaluations" },
          // The owning experiment is only linkable once the run has loaded.
          {
            label: t("evaluation.overview.experiments"),
            href: run
              ? `/evaluations/experiments/${encodeURIComponent(run.experiment_id)}`
              : "/evaluations/experiments",
          },
          { label: `${runId.slice(0, 8)}…` },
        ]}
      />
      <header className="page-header">
        <p className="eyebrow">{t("evaluation.run.eyebrow")}</p>
        <h1>
          {t("evaluation.run.title")} <code title={runId}>{runId.slice(0, 8)}…</code>{" "}
          {run && <StatusBadge status={run.status} />}
        </h1>
      </header>

      {error && (
        <ErrorState code={error.code} message={error.message || t("errors.loadEvaluation")} onRetry={() => void load()} />
      )}
      {loading && !run && !error && <LoadingState />}
      {loaded && !run && !error && (
        <EmptyState title={t("evaluation.run.notFound")} hint={t("evaluation.run.notFoundHint")} />
      )}

      {run && (
        <>
          <Panel ariaLabel={t("evaluation.run.eyebrow")}>
            <div className="run-facts">
              <span>{t("evaluation.run.facts.status")}<strong><StatusBadge status={run.status} /></strong></span>
              <span>{t("evaluation.run.facts.purpose")}<strong>{purposeLabel(run.purpose)}</strong></span>
              <span>{t("evaluation.run.facts.split")}<strong>{run.split}</strong></span>
              <span>{t("evaluation.run.facts.repetitions")}<strong>{run.repetitions}</strong></span>
              <span>
                {t("evaluation.run.facts.gitCommit")}
                <strong><code>{run.git_commit.slice(0, 12)}</code></strong>
              </span>
              <span>
                {t("evaluation.run.facts.datasetHash")}
                <strong><HashValue value={run.dataset_hash} label={t("evaluation.run.facts.datasetHash")} /></strong>
              </span>
              <span>
                {t("evaluation.run.facts.specHash")}
                <strong><HashValue value={run.experiment_spec_hash} label={t("evaluation.run.facts.specHash")} /></strong>
              </span>
              <span>{t("evaluation.run.facts.holdoutExposure")}<strong>{run.holdout_exposure_index ?? "—"}</strong></span>
              <span>
                {t("evaluation.run.facts.started")}
                <strong>{run.started_at ? formatDateTime(run.started_at) : "—"}</strong>
              </span>
              <span>
                {t("evaluation.run.facts.completed")}
                <strong>{run.completed_at ? formatDateTime(run.completed_at) : "—"}</strong>
              </span>
              <span>
                {t("evaluation.run.facts.failure")}
                <strong>{run.failure_code ?? "—"}</strong>
              </span>
            </div>
            {run.safe_failure_message && <p className="state-hint">{run.safe_failure_message}</p>}
            {CANCELLABLE_RUN_STATUSES.has(run.status) && (
              <div className="form-actions">
                <button type="button" className="button button-ghost" onClick={() => void requestCancel()} disabled={cancelling}>
                  {cancelling ? t("evaluation.run.cancelling") : t("evaluation.run.cancel")}
                </button>
              </div>
            )}
            {cancelRequested && <p className="inline-notice">{t("evaluation.run.cancelRequestedNotice")}</p>}
            <InlineError error={cancelError} fallback={t("errors.requestFailed")} />
          </Panel>

          {progress && (
            <Panel title={t("evaluation.run.progress.title")} eyebrow={t("evaluation.run.eyebrow")}>
              <div
                className="progress-track"
                role="progressbar"
                aria-valuenow={Math.round(progress.progress * 100)}
                aria-valuemin={0}
                aria-valuemax={100}
                aria-label={t("evaluation.run.progress.percent", { percent: Math.round(progress.progress * 100) })}
              >
                <div className="progress-fill" style={{ width: `${Math.max(progress.progress * 100, 1)}%` }} />
              </div>
              <p className="state-hint">
                {t("evaluation.run.progress.percent", { percent: Math.round(progress.progress * 100) })} ·{" "}
                {t("evaluation.run.progress.total")} {formatNumber(progress.total)} ·{" "}
                {t("evaluation.run.progress.pending")} {formatNumber(progress.pending)} ·{" "}
                {t("evaluation.run.progress.running")} {formatNumber(progress.running)} ·{" "}
                {t("evaluation.run.progress.completed")} {formatNumber(progress.completed)} ·{" "}
                {t("evaluation.run.progress.failed")} {formatNumber(progress.failed)} ·{" "}
                {t("evaluation.run.progress.cancelled")} {formatNumber(progress.cancelled)}
              </p>
            </Panel>
          )}

          {experimentError && (
            <ErrorState
              code={experimentError.code}
              message={experimentError.message || t("errors.loadEvaluation")}
              onRetry={() => {
                if (activeSessionRef.current !== sessionId || !run) return;
                setExperimentError(null);
                getExperiment({ workspaceId, accessToken }, run.experiment_id)
                  .then((nextExperiment) => {
                    if (activeSessionRef.current === sessionId) setExperiment(nextExperiment);
                  })
                  .catch((caught) => {
                    if (activeSessionRef.current === sessionId) setExperimentError(toApiError(caught, ""));
                  });
              }}
            />
          )}
          <RunWorkflow
            key={`${sessionId}:${run.id}`}
            input={{ workspaceId, accessToken, sessionId }}
            run={run}
            variants={experiment?.variants ?? []}
            terminal={terminal}
          />
        </>
      )}
    </div>
  );
}
