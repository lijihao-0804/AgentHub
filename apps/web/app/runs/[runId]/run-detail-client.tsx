"use client";

import Link from "next/link";
import { useState } from "react";

import {
  getRunDetail,
  getRunTimeline,
  RunDetail,
  RunTimelineEntry,
  RunsApiError,
} from "../../../lib/runs";

function statusClass(status: string): string {
  if (status === "WAITING_APPROVAL") return "run-status waiting";
  if (status === "NEEDS_ATTENTION") return "run-status attention";
  if (status === "FAILED") return "run-status failed";
  return "run-status";
}

function formatCost(run: RunDetail): string {
  if (run.total_cost_amount === null) return "—";
  return `${run.total_cost_amount} ${run.cost_currency ?? ""}`.trim();
}

export default function RunDetailClient({ runId }: { runId: string }) {
  const [workspaceId, setWorkspaceId] = useState("");
  const [accessToken, setAccessToken] = useState("");
  const [run, setRun] = useState<RunDetail | null>(null);
  const [timeline, setTimeline] = useState<RunTimelineEntry[]>([]);
  const [message, setMessage] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function load() {
    setMessage(null);
    if (!workspaceId.trim() || !accessToken.trim()) {
      setMessage("Workspace ID and access token are required.");
      return;
    }
    setLoading(true);
    try {
      const [detail, timelineResponse] = await Promise.all([
        getRunDetail(workspaceId, runId, accessToken),
        getRunTimeline(workspaceId, runId, accessToken),
      ]);
      setRun(detail);
      setTimeline(timelineResponse.items);
    } catch (error) {
      setMessage(error instanceof RunsApiError ? error.message : "Could not load run detail.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="observability-shell">
      <header className="observability-header">
        <div>
          <p className="eyebrow">M6-A · RUN DETAIL</p>
          <h1>Run timeline</h1>
          <p className="observability-lede">{runId}</p>
        </div>
        <Link className="back-link" href="/runs">
          Back to runs
        </Link>
      </header>
      <section className="observability-panel">
        <div className="observability-form">
          <label>
            Workspace ID
            <input value={workspaceId} onChange={(event) => setWorkspaceId(event.target.value)} />
          </label>
          <label>
            Access token
            <input
              type="password"
              value={accessToken}
              onChange={(event) => setAccessToken(event.target.value)}
            />
          </label>
          <button type="button" onClick={load} disabled={loading}>
            {loading ? "Loading…" : "Load detail"}
          </button>
        </div>
        {message && <p className="state-message">{message}</p>}
      </section>
      {run && (
        <>
          <section className="run-detail-summary">
            <div className="run-card-header">
              <div>
                <p className="eyebrow">AGENT VERSION</p>
                <h2>v{run.agent_version_number}</h2>
              </div>
              <span className={statusClass(run.status)}>{run.status}</span>
            </div>
            {run.status === "WAITING_APPROVAL" && (
              <p className="waiting-callout">Waiting for approval before action execution.</p>
            )}
            {run.status === "NEEDS_ATTENTION" && (
              <p className="needs-attention">Needs attention: the run requires operator follow-up.</p>
            )}
            <div className="run-metrics">
              <span>Duration {run.duration_ms === null ? "—" : `${run.duration_ms} ms`}</span>
              <span>Tokens {run.total_tokens ?? "—"}</span>
              <span>Cost {formatCost(run)}</span>
              <span>Model steps {run.model_step_count}</span>
              <span>Tool calls {run.tool_call_count}</span>
              <span>Failure {run.failure_code ?? "—"}</span>
            </div>
            <div className="approval-summary">
              <strong>Approval summary</strong>
              <span>Pending {run.approval_summary.pending}</span>
              <span>Approved {run.approval_summary.approved}</span>
              <span>Decision denied {run.approval_summary.denied}</span>
              <span>Action failed {run.approval_summary.failed}</span>
              <span>Unknown outcome {run.approval_summary.unknown_outcome}</span>
            </div>
          </section>
          <section className="timeline-panel">
            <div className="panel-heading">
              <div>
                <p className="eyebrow">SAFE PROVIDER-NEUTRAL EVENTS</p>
                <h2>Timeline</h2>
              </div>
            </div>
            <div className="timeline-list">
              {timeline.map((entry) => (
                <article className="timeline-entry" key={`${entry.sequence}-${entry.kind}`}>
                  <div className="timeline-sequence">{entry.sequence}</div>
                  <div>
                    <div className="timeline-entry-header">
                      <strong>{entry.kind}</strong>
                      <span>{entry.status}</span>
                    </div>
                    <p className="muted">{new Date(entry.occurred_at).toLocaleString()}</p>
                    {entry.failure_code && (
                      <p className="needs-attention">
                        {entry.kind === "APPROVAL_DECISION"
                          ? "Approval decision"
                          : entry.kind === "ACTION_EXECUTION"
                            ? "Action execution"
                            : entry.failure_category ?? "UNKNOWN"}: {entry.failure_code}
                      </p>
                    )}
                    {typeof entry.metadata.safe_failure_message === "string" && (
                      <p className="muted">{entry.metadata.safe_failure_message}</p>
                    )}
                    {Object.keys(entry.metadata).length > 0 && (
                      <pre>{JSON.stringify(entry.metadata, null, 2)}</pre>
                    )}
                  </div>
                </article>
              ))}
            </div>
          </section>
        </>
      )}
    </main>
  );
}
