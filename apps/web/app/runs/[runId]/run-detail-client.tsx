"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import StatusBadge from "@/components/ui/status-badge";
import { statusTone, type StatusTone } from "@/components/ui/badge-tones";
import Breadcrumbs from "@/components/layout/breadcrumbs";
import { EmptyState, ErrorState, InlineError, LoadingState, Panel, SessionRequired } from "@/components/ui/states";
import TechnicalDetails from "@/components/ui/technical-details";
import AddRunToDatasetPanel from "@/components/evaluation/add-run-to-dataset-panel";
import { errorHintKey, type AuthInput } from "@/lib/api/client";
import { createAgentRun, getAgentRun, type AgentRun } from "@/lib/api/agent-runtime";
import { getRunDetail, getRunTimeline, RunDetail, RunTimelineEntry } from "@/lib/api/runs";
import { useFrontendSession } from "@/components/providers/session-provider";
import { useWorkspaceData, useWorkspaceMutation } from "@/hooks/use-workspace-data";
import { useI18n } from "@/i18n/provider";

function timelineTone(entry: RunTimelineEntry): StatusTone {
  if (entry.status === "UNKNOWN_OUTCOME") return "attention";
  return statusTone(entry.status);
}

function shortId(value: string): string {
  return `${value.slice(0, 8)}…`;
}

type RunView = { detail: RunDetail; timeline: RunTimelineEntry[] };

export default function RunDetailClient({ runId }: { runId: string }) {
  const { t, statusLabel, failureCategoryLabel, timelineKindLabel, formatDateTime, formatNumber, formatCurrencyAmount } = useI18n();
  const { workspaceId, connected, sessionId } = useFrontendSession();
  const [replay, setReplay] = useState<AgentRun | null>(null);
  const [addToEvaluationOpen, setAddToEvaluationOpen] = useState(false);
  const replayMutation = useWorkspaceMutation(`run-replay:${workspaceId}:${runId}`);

  /**
   * Detail and timeline load as one resource so they always belong to the
   * same request generation: a detail from one workspace can never be
   * shown beside a timeline from another.
   */
  const load = useCallback(
    async (auth: AuthInput): Promise<RunView> => {
      const [detail, timelineResponse] = await Promise.all([
        getRunDetail(auth.workspaceId, runId, auth.accessToken),
        getRunTimeline(auth.workspaceId, runId, auth.accessToken),
      ]);
      return { detail, timeline: timelineResponse.items };
    },
    [runId],
  );
  const view = useWorkspaceData<RunView>(load, `run-detail:${workspaceId}:${runId}`);
  const run = view.data?.detail ?? null;
  const timeline = view.data?.timeline ?? [];
  const error = view.error;

  // A replay belongs to the workspace session that created it; switching
  // workspaces or runs must not leave a stale replay pointer on screen.
  useEffect(() => {
    setReplay(null);
    setAddToEvaluationOpen(false);
  }, [sessionId, runId]);

  /**
   * Replay reads the authoritative runtime record first: the
   * observability projection carries no input text, and the input must be
   * the one the backend actually stored, not a reconstruction.
   */
  const handleReplay = useCallback(async () => {
    await replayMutation.run(
      async (auth) => {
        const source = await getAgentRun(auth, runId);
        return createAgentRun(auth, {
          agentVersionId: source.agent_version_id,
          inputText: source.input_text,
        });
      },
      (created) => setReplay(created),
    );
  }, [replayMutation, runId]);

  // A missing run is a normal outcome, not a failure to report as one.
  // With a single guarded load, `loaded` implies data, so not-found can
  // only arrive as the backend's 404.
  const notFound = error?.status === 404;
  const hint = error ? errorHintKey(error) : null;

  if (!connected) {
    return (
      <div className="page">
        <header className="page-header">
          <p className="eyebrow">{t("run.eyebrow")}</p>
          <h1>
            {t("run.title")} <code title={runId}>{shortId(runId)}</code>
          </h1>
        </header>
        <SessionRequired contextKey="session.context.run" />
      </div>
    );
  }

  function entrySubtitle(entry: RunTimelineEntry): string | null {
    const meta = entry.metadata;
    switch (entry.kind) {
      case "APPROVAL_DECISION": {
        const decision = typeof meta.decision_status === "string" ? meta.decision_status : entry.status;
        return t("timeline.subtitle.decision", { value: statusLabel(decision) });
      }
      case "ACTION_EXECUTION": {
        const execution = typeof meta.execution_status === "string" ? meta.execution_status : entry.status;
        return execution === "UNKNOWN_OUTCOME"
          ? t("timeline.subtitle.executionUnknown")
          : t("timeline.subtitle.execution", { value: statusLabel(execution) });
      }
      case "APPROVAL_WAIT": {
        const tool = typeof meta.tool_identity === "string" ? meta.tool_identity : null;
        return tool ? t("timeline.subtitle.waitingFor", { tool }) : t("timeline.subtitle.waitingGeneric");
      }
      case "TOOL":
      case "RETRIEVAL": {
        const identities = Array.isArray(meta.tool_identities)
          ? meta.tool_identities.filter((v): v is string => typeof v === "string")
          : [];
        const single = typeof meta.tool_identity === "string" ? meta.tool_identity : identities[0];
        return single ?? null;
      }
      case "RUN_STARTED": {
        const versionId = typeof meta.agent_version_id === "string" ? meta.agent_version_id : null;
        return versionId ? t("timeline.subtitle.agentVersion", { id: shortId(versionId) }) : null;
      }
      case "FAILURE":
        return entry.failure_code ? t("timeline.subtitle.failureCode", { code: entry.failure_code }) : null;
      default:
        return null;
    }
  }

  function entrySummary(entry: RunTimelineEntry): string | null {
    const meta = entry.metadata;
    const safeMessage = typeof meta.safe_failure_message === "string" ? meta.safe_failure_message : null;
    if (safeMessage) return safeMessage;
    if (entry.failure_code && entry.failure_category) {
      return `${failureCategoryLabel(entry.failure_category)}: ${entry.failure_code}`;
    }
    return null;
  }

  return (
    <div className="page">
      <Breadcrumbs items={[{ label: t("nav.runs"), href: "/runs" }, { label: shortId(runId) }]} />
      <header className="page-header">
        <p className="eyebrow">{t("run.eyebrow")}</p>
        <h1>
          {t("run.title")} <code title={runId}>{shortId(runId)}</code>
        </h1>
      </header>

      {notFound && <EmptyState title={t("run.notFound")} hint={t("run.notFoundHint")} />}
      {error && !notFound && (
        <ErrorState
          code={error.code}
          message={error.message || t("errors.loadRunDetail")}
          hint={hint ? t(hint) : undefined}
          onRetry={view.reload}
        />
      )}
      {view.loading && !run && !error && <LoadingState />}

      {run && (
        <>
          <Panel ariaLabel={t("run.eyebrow")}>
            <div className="approval-header">
              <div>
                <p className="eyebrow">{t("run.agentVersionEyebrow", { version: run.agent_version_number })}</p>
                <h2><StatusBadge status={run.status} /></h2>
              </div>
              <div className="approval-actions">
                {run.status === "WAITING_APPROVAL" && (
                  <Link className="button button-ghost" href="/approvals">
                    {t("run.openApprovals")}
                  </Link>
                )}
                <Link
                  className="button button-ghost"
                  href={`/runs/compare?left=${encodeURIComponent(runId)}`}
                >
                  {t("run.compareEntry")}
                </Link>
                <button
                  className="button button-ghost"
                  type="button"
                  onClick={() => setAddToEvaluationOpen((current) => !current)}
                >
                  {t("run.addToEvaluation.button")}
                </button>
                <button
                  className="button button-primary"
                  type="button"
                  disabled={replayMutation.pending}
                  onClick={() => void handleReplay()}
                >
                  {replayMutation.pending ? t("run.replay.running") : t("run.replay.button")}
                </button>
              </div>
            </div>
            {run.status === "WAITING_APPROVAL" && <p className="state-hint">{t("run.waitingCallout")}</p>}
            {run.status === "NEEDS_ATTENTION" && <p className="state-hint">{t("run.attentionCallout")}</p>}
            {/* Stated before the click: a replay really does execute again. */}
            <p className="state-hint">{t("run.replay.warning")}</p>
            <InlineError error={replayMutation.error} fallback={t("run.replay.failed")} />
            {replay && (
              <div className="inline-notice replay-notice">
                <p>
                  <strong>{t("run.replay.created")}</strong>
                </p>
                <p className="state-hint">
                  {t("run.replay.original")}: <code title={runId}>{shortId(runId)}</code>
                </p>
                <p className="state-hint">
                  {t("run.replay.replay")}: <code title={replay.id}>{shortId(replay.id)}</code>{" "}
                  <StatusBadge status={replay.status} />
                </p>
                {replay.status === "WAITING_APPROVAL" && (
                  <p className="state-hint">{t("run.replay.waitingApproval")}</p>
                )}
                <div className="approval-actions">
                  <Link
                    className="button button-ghost"
                    href={`/runs/compare?left=${encodeURIComponent(runId)}&right=${encodeURIComponent(replay.id)}`}
                  >
                    {t("run.replay.compare")}
                  </Link>
                  <Link className="button button-ghost" href={`/runs/${replay.id}`}>
                    {t("run.replay.open")}
                  </Link>
                </div>
              </div>
            )}
            <div className="run-facts">
              <span>{t("run.facts.duration")}<strong>{run.duration_ms === null ? "—" : `${formatNumber(run.duration_ms)} ms`}</strong></span>
              <span>{t("run.facts.tokens")}<strong>{run.total_tokens ?? "—"}</strong></span>
              <span>
                {t("run.facts.cost")}
                <strong>
                  {run.total_cost_amount === null
                    ? "—"
                    : formatCurrencyAmount(run.total_cost_amount, run.cost_currency)}
                </strong>
              </span>
              <span>{t("run.facts.modelSteps")}<strong>{run.model_step_count}</strong></span>
              <span>{t("run.facts.toolCalls")}<strong>{run.tool_call_count}</strong></span>
              <span>{t("run.facts.failure")}<strong>{run.failure_code ?? "—"}</strong></span>
            </div>
            <div className="approval-chips">
              <span className="state-hint">{t("run.approvalsLabel")}</span>
              <StatusBadge status="PENDING" label={t("run.chips.pending", { count: run.approval_summary.pending })} />
              <StatusBadge status="APPROVED" label={t("run.chips.approved", { count: run.approval_summary.approved })} />
              <StatusBadge status="DENIED" label={t("run.chips.denied", { count: run.approval_summary.denied })} />
              <StatusBadge status="FAILED" label={t("run.chips.executionFailed", { count: run.approval_summary.failed })} />
              <StatusBadge status="UNKNOWN_OUTCOME" label={t("run.chips.unknownOutcome", { count: run.approval_summary.unknown_outcome })} />
            </div>
          </Panel>

          <AddRunToDatasetPanel
            runId={runId}
            open={addToEvaluationOpen}
            onClose={() => setAddToEvaluationOpen(false)}
          />

          <Panel title={t("timeline.title")} eyebrow={t("timeline.eyebrow")}>
            {timeline.length === 0 ? (
              <EmptyState title={t("timeline.empty")} hint={t("timeline.emptyHint")} />
            ) : (
              <div className="timeline">
                {timeline.map((entry) => (
                  <article className="timeline-entry" key={`${entry.sequence}-${entry.kind}`}>
                    <span className={`timeline-dot tone-${timelineTone(entry)}`} aria-hidden="true" />
                    <div className="timeline-body">
                      <div className="timeline-title">
                        <strong>{timelineKindLabel(entry.kind)}</strong>
                        <StatusBadge status={entry.status} />
                        {entry.duration_ms !== null && (
                          <span className="timeline-meta">{formatNumber(Math.round(entry.duration_ms))} ms</span>
                        )}
                      </div>
                      {entrySubtitle(entry) && <p className="timeline-summary">{entrySubtitle(entry)}</p>}
                      {entrySummary(entry) && <p className="timeline-summary">{entrySummary(entry)}</p>}
                      <p className="timeline-meta">{formatDateTime(entry.occurred_at)}</p>
                      <TechnicalDetails summary={t("run.technicalMetadata")} value={entry.metadata} />
                    </div>
                  </article>
                ))}
              </div>
            )}
          </Panel>
        </>
      )}
    </div>
  );
}
