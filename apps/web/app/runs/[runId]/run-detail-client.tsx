"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import StatusBadge from "../../../components/status-badge";
import { statusTone, type StatusTone } from "../../../components/badge-tones";
import { EmptyState, ErrorState, LoadingState, Panel, SessionRequired } from "../../../components/states";
import TechnicalDetails from "../../../components/technical-details";
import { ApiError, errorHintKey, toApiError } from "../../../lib/api-client";
import { getRunDetail, getRunTimeline, RunDetail, RunTimelineEntry } from "../../../lib/runs";
import { useFrontendSession } from "../../../components/session-provider";
import { useI18n } from "../../../i18n/provider";

function timelineTone(entry: RunTimelineEntry): StatusTone {
  if (entry.status === "UNKNOWN_OUTCOME") return "attention";
  return statusTone(entry.status);
}

function shortId(value: string): string {
  return `${value.slice(0, 8)}…`;
}

export default function RunDetailClient({ runId }: { runId: string }) {
  const { t, statusLabel, failureCategoryLabel, timelineKindLabel, formatDateTime, formatNumber, formatCurrencyAmount } = useI18n();
  const { workspaceId, accessToken, connected } = useFrontendSession();
  const [run, setRun] = useState<RunDetail | null>(null);
  const [timeline, setTimeline] = useState<RunTimelineEntry[]>([]);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);

  const load = useCallback(async () => {
    setError(null);
    if (!connected) return;
    setLoading(true);
    try {
      const [detail, timelineResponse] = await Promise.all([
        getRunDetail(workspaceId, runId, accessToken),
        getRunTimeline(workspaceId, runId, accessToken),
      ]);
      setRun(detail);
      setTimeline(timelineResponse.items);
    } catch (caught) {
      setError(toApiError(caught, ""));
    } finally {
      setLoading(false);
      setLoaded(true);
    }
  }, [connected, workspaceId, accessToken, runId]);

  useEffect(() => {
    if (connected && !loaded) void load();
  }, [connected, loaded, load]);

  useEffect(() => {
    if (!connected) setLoaded(false);
  }, [connected]);

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
      <header className="page-header">
        <p className="eyebrow">{t("run.eyebrow")}</p>
        <h1>
          {t("run.title")} <code title={runId}>{shortId(runId)}</code>
        </h1>
        <p className="page-lede">
          <Link href="/runs">{t("run.backToRuns")}</Link>
        </p>
      </header>

      {error && (
        <ErrorState
          code={error.code}
          message={error.message || t("errors.loadRunDetail")}
          hint={hint ? t(hint) : undefined}
          onRetry={() => void load()}
        />
      )}
      {loading && !run && !error && <LoadingState />}
      {loaded && !run && !error && (
        <EmptyState title={t("run.notFound")} hint={t("run.notFoundHint")} />
      )}

      {run && (
        <>
          <Panel ariaLabel={t("run.eyebrow")}>
            <div className="approval-header">
              <div>
                <p className="eyebrow">{t("run.agentVersionEyebrow", { version: run.agent_version_number })}</p>
                <h2><StatusBadge status={run.status} /></h2>
              </div>
              {run.status === "WAITING_APPROVAL" && (
                <Link className="button button-ghost" href="/approvals">
                  {t("run.openApprovals")}
                </Link>
              )}
            </div>
            {run.status === "WAITING_APPROVAL" && <p className="state-hint">{t("run.waitingCallout")}</p>}
            {run.status === "NEEDS_ATTENTION" && <p className="state-hint">{t("run.attentionCallout")}</p>}
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
