"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { listRuns, RunListItem, RunsApiError } from "../../lib/runs";

function statusClass(status: string): string {
  if (status === "WAITING_APPROVAL") return "run-status waiting";
  if (status === "NEEDS_ATTENTION") return "run-status attention";
  if (status === "FAILED") return "run-status failed";
  return "run-status";
}

function formatCost(run: RunListItem): string {
  if (run.total_cost_amount === null) return "—";
  return `${run.total_cost_amount} ${run.cost_currency ?? ""}`.trim();
}

export default function RunsPage() {
  const [workspaceId, setWorkspaceId] = useState("");
  const [accessToken, setAccessToken] = useState("");
  const [status, setStatus] = useState("");
  const [agentVersionId, setAgentVersionId] = useState("");
  const [runs, setRuns] = useState<RunListItem[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    setStatus(params.get("status") ?? "");
    setAgentVersionId(params.get("agent_version_id") ?? "");
  }, []);

  async function refresh(cursor: string | null = null) {
    setMessage(null);
    if (!workspaceId.trim() || !accessToken.trim()) {
      setMessage("Workspace ID and access token are required.");
      return;
    }
    setLoading(true);
    try {
      const result = await listRuns({
        workspaceId,
        accessToken,
        status,
        agentVersionId,
        cursor,
        limit: 25,
      });
      setRuns((current) => (cursor ? [...current, ...result.items] : result.items));
      setNextCursor(result.next_cursor);
    } catch (error) {
      setMessage(error instanceof RunsApiError ? error.message : "Could not load runs.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="observability-shell">
      <header className="observability-header">
        <div>
          <p className="eyebrow">M6-A · OBSERVABILITY</p>
          <h1>Runs</h1>
          <p className="observability-lede">
            Workspace-scoped runtime history with safe status, usage, cost and approval projections.
          </p>
        </div>
        <Link className="back-link" href="/">
          Back to AgentHub
        </Link>
      </header>
      <section className="observability-panel" aria-labelledby="runs-query-title">
        <div className="panel-heading">
          <div>
            <p className="eyebrow">READ-ONLY QUERY</p>
            <h2 id="runs-query-title">Find runs</h2>
          </div>
          <span className="badge">NO TOKEN STORAGE</span>
        </div>
        <p className="observability-note">
          The access token remains in this page&apos;s React state and disappears on refresh.
        </p>
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
          <label>
            Status
            <select value={status} onChange={(event) => setStatus(event.target.value)}>
              <option value="">All statuses</option>
              <option value="RUNNING">RUNNING</option>
              <option value="WAITING_APPROVAL">WAITING_APPROVAL</option>
              <option value="SUCCEEDED">SUCCEEDED</option>
              <option value="FAILED">FAILED</option>
              <option value="NEEDS_ATTENTION">NEEDS_ATTENTION</option>
              <option value="CANCEL_REQUESTED">CANCEL_REQUESTED</option>
              <option value="CANCELLED">CANCELLED</option>
            </select>
          </label>
          <button type="button" onClick={() => refresh()} disabled={loading}>
            {loading ? "Loading…" : "Load runs"}
          </button>
        </div>
        {message && <p className="state-message">{message}</p>}
      </section>
      <section className="run-list" aria-live="polite">
        {runs.length === 0 && !message && <p className="muted">No runs loaded.</p>}
        {runs.map((run) => (
          <article className="run-card" key={run.id}>
            <div className="run-card-header">
              <div>
                <p className="eyebrow">RUN {run.id}</p>
                <h2>AgentVersion v{run.agent_version_number}</h2>
              </div>
              <span className={statusClass(run.status)}>{run.status}</span>
            </div>
            <div className="run-metrics">
              <span>Duration {run.duration_ms === null ? "—" : `${run.duration_ms} ms`}</span>
              <span>Tokens {run.total_tokens ?? "—"}</span>
              <span>Cost {formatCost(run)}</span>
              <span>Tools {run.tool_call_count}</span>
            </div>
            {run.failure_code && (
              <p className={run.status === "NEEDS_ATTENTION" ? "needs-attention" : "state-message"}>
                {run.failure_category ?? "UNKNOWN"}: {run.failure_code}
              </p>
            )}
            <Link className="run-detail-link" href={`/runs/${encodeURIComponent(run.id)}`}>
              Open run detail →
            </Link>
          </article>
        ))}
        {nextCursor && (
          <button type="button" className="load-more" onClick={() => refresh(nextCursor)} disabled={loading}>
            Load more
          </button>
        )}
      </section>
    </main>
  );
}
