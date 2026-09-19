"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import StatusBadge, { statusTone, type StatusTone } from "../../../components/status-badge";
import { EmptyState, ErrorState, LoadingState, Panel, SessionRequired } from "../../../components/states";
import TechnicalDetails from "../../../components/technical-details";
import { ApiError, errorHint, toApiError } from "../../../lib/api-client";
import { getRunDetail, getRunTimeline, RunDetail, RunTimelineEntry } from "../../../lib/runs";
import { useFrontendSession } from "../../../components/session-provider";

function formatCost(run: RunDetail): string {
  if (run.total_cost_amount === null) return "—";
  return `${run.total_cost_amount} ${run.cost_currency ?? ""}`.trim();
}

const TIMELINE_LABELS: Record<string, string> = {
  RUN_STARTED: "Run started",
  MODEL: "Model step",
  RETRIEVAL: "Retrieval",
  TOOL: "Tool call",
  APPROVAL_WAIT: "Approval required",
  APPROVAL_DECISION: "Approval decision",
  ACTION_EXECUTION: "Action execution",
  RESUME: "Resumed",
  FINISH: "Run completed",
  FAILURE: "Run failed",
};

function timelineTone(entry: RunTimelineEntry): StatusTone {
  if (entry.status === "UNKNOWN_OUTCOME") return "attention";
  return statusTone(entry.status);
}

function entryTitle(entry: RunTimelineEntry): string {
  return TIMELINE_LABELS[entry.kind] ?? entry.kind;
}

function entrySubtitle(entry: RunTimelineEntry): string | null {
  const meta = entry.metadata;
  switch (entry.kind) {
    case "APPROVAL_DECISION": {
      const decision = typeof meta.decision_status === "string" ? meta.decision_status : entry.status;
      return `Decision: ${decision}`;
    }
    case "ACTION_EXECUTION": {
      const execution = typeof meta.execution_status === "string" ? meta.execution_status : entry.status;
      return execution === "UNKNOWN_OUTCOME"
        ? "Execution: UNKNOWN_OUTCOME — the side effect could not be confirmed and needs attention"
        : `Execution: ${execution}`;
    }
    case "APPROVAL_WAIT": {
      const tool = typeof meta.tool_identity === "string" ? meta.tool_identity : null;
      return tool ? `Waiting for ${tool}` : "Waiting for a decision before action execution";
    }
    case "TOOL":
    case "RETRIEVAL": {
      const identities = Array.isArray(meta.tool_identities) ? meta.tool_identities.filter((v): v is string => typeof v === "string") : [];
      const single = typeof meta.tool_identity === "string" ? meta.tool_identity : identities[0];
      return single ?? null;
    }
    case "RUN_STARTED": {
      const versionId = typeof meta.agent_version_id === "string" ? meta.agent_version_id : null;
      return versionId ? `AgentVersion ${versionId.slice(0, 8)}…` : null;
    }
    case "FAILURE":
      return entry.failure_code ? `Failure code: ${entry.failure_code}` : null;
    default:
      return null;
  }
}

function entrySummary(entry: RunTimelineEntry): string | null {
  const meta = entry.metadata;
  const safeMessage = typeof meta.safe_failure_message === "string" ? meta.safe_failure_message : null;
  if (safeMessage) return safeMessage;
  if (entry.failure_code && entry.failure_category) {
    return `${entry.failure_category}: ${entry.failure_code}`;
  }
  return null;
}

function shortId(value: string): string {
  return `${value.slice(0, 8)}…`;
}

export default function RunDetailClient({ runId }: { runId: string }) {
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
      setError(toApiError(caught, "Could not load run detail."));
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

  if (!connected) {
    return (
      <div className="page">
        <header className="page-header">
          <p className="eyebrow">RUN DETAIL</p>
          <h1>Run {shortId(runId)}</h1>
        </header>
        <SessionRequired context="this run" />
      </div>
    );
  }

  return (
    <div className="page">
      <header className="page-header">
        <p className="eyebrow">RUN DETAIL</p>
        <h1>
          Run <code title={runId}>{shortId(runId)}</code>
        </h1>
        <p className="page-lede">
          <Link href="/runs">← Back to runs</Link>
        </p>
      </header>

      {error && (
        <ErrorState code={error.code} message={error.message} hint={errorHint(error)} onRetry={() => void load()} />
      )}
      {loading && !run && !error && <LoadingState label="Loading run detail…" />}
      {loaded && !run && !error && <EmptyState title="Run not found." hint="This run does not exist in the connected workspace." />}

      {run && (
        <>
          <Panel ariaLabel="Run summary">
            <div className="approval-header">
              <div>
                <p className="eyebrow">AGENTVERSION v{run.agent_version_number}</p>
                <h2><StatusBadge status={run.status} /></h2>
              </div>
              {run.status === "WAITING_APPROVAL" && (
                <Link className="button button-ghost" href="/approvals">
                  Open approvals
                </Link>
              )}
            </div>
            {run.status === "WAITING_APPROVAL" && (
              <p className="state-hint">Waiting for an approval decision before action execution.</p>
            )}
            {run.status === "NEEDS_ATTENTION" && (
              <p className="state-hint">
                Needs attention: the run requires operator follow-up (for example an unconfirmed action outcome).
              </p>
            )}
            <div className="run-facts">
              <span>Duration<strong>{run.duration_ms === null ? "—" : `${run.duration_ms} ms`}</strong></span>
              <span>Tokens<strong>{run.total_tokens ?? "—"}</strong></span>
              <span>Cost<strong>{formatCost(run)}</strong></span>
              <span>Model steps<strong>{run.model_step_count}</strong></span>
              <span>Tool calls<strong>{run.tool_call_count}</strong></span>
              <span>Failure<strong>{run.failure_code ?? "—"}</strong></span>
            </div>
            <div className="approval-chips">
              <span className="state-hint">Approvals:</span>
              <StatusBadge status="PENDING" label={`Pending ${run.approval_summary.pending}`} />
              <StatusBadge status="APPROVED" label={`Approved ${run.approval_summary.approved}`} />
              <StatusBadge status="DENIED" label={`Denied ${run.approval_summary.denied}`} />
              <StatusBadge status="FAILED" label={`Execution failed ${run.approval_summary.failed}`} />
              <StatusBadge status="UNKNOWN_OUTCOME" label={`Unknown outcome ${run.approval_summary.unknown_outcome}`} />
            </div>
          </Panel>

          <Panel title="Timeline" eyebrow="SAFE PROVIDER-NEUTRAL EVENTS">
            {timeline.length === 0 ? (
              <EmptyState title="No timeline events." hint="Events appear once the run executes." />
            ) : (
              <div className="timeline">
                {timeline.map((entry) => (
                  <article className="timeline-entry" key={`${entry.sequence}-${entry.kind}`}>
                    <span className={`timeline-dot tone-${timelineTone(entry)}`} aria-hidden="true" />
                    <div className="timeline-body">
                      <div className="timeline-title">
                        <strong>{entryTitle(entry)}</strong>
                        <StatusBadge status={entry.status} />
                        {entry.duration_ms !== null && (
                          <span className="timeline-meta">{entry.duration_ms.toFixed(0)} ms</span>
                        )}
                      </div>
                      {entrySubtitle(entry) && <p className="timeline-summary">{entrySubtitle(entry)}</p>}
                      {entrySummary(entry) && <p className="timeline-summary">{entrySummary(entry)}</p>}
                      <p className="timeline-meta">{new Date(entry.occurred_at).toLocaleString()}</p>
                      <TechnicalDetails summary="Technical metadata" value={entry.metadata} />
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
