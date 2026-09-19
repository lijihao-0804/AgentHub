"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import StatusBadge from "../../components/status-badge";
import { EmptyState, ErrorState, LoadingState, SessionRequired } from "../../components/states";
import { ApiError, errorHint, toApiError } from "../../lib/api-client";
import { listRuns, RunListItem } from "../../lib/runs";
import { useFrontendSession } from "../../components/session-provider";

const RUN_STATUS_OPTIONS = [
  "RUNNING",
  "WAITING_APPROVAL",
  "SUCCEEDED",
  "FAILED",
  "NEEDS_ATTENTION",
  "CANCEL_REQUESTED",
  "CANCELLED",
];

function formatCost(run: RunListItem): string {
  if (run.total_cost_amount === null) return "—";
  return `${run.total_cost_amount} ${run.cost_currency ?? ""}`.trim();
}

function formatStarted(run: RunListItem): string {
  return new Date(run.started_at).toLocaleString();
}

function shortId(id: string): string {
  return `${id.slice(0, 8)}…`;
}

export default function RunsPage() {
  const { workspaceId, accessToken, connected } = useFrontendSession();
  const [status, setStatus] = useState("");
  const [agentVersionId, setAgentVersionId] = useState("");
  const [runs, setRuns] = useState<RunListItem[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(false);
  const [initialLoaded, setInitialLoaded] = useState(false);

  const refresh = useCallback(
    async (cursor: string | null = null, overrides?: { status?: string; agentVersionId?: string }) => {
      setError(null);
      if (!connected) return;
      const effectiveStatus = overrides?.status ?? status;
      const effectiveVersion = overrides?.agentVersionId ?? agentVersionId;
      setLoading(true);
      try {
        const result = await listRuns({
          workspaceId,
          accessToken,
          status: effectiveStatus,
          agentVersionId: effectiveVersion,
          cursor,
          limit: 25,
        });
        setRuns((current) => (cursor ? [...current, ...result.items] : result.items));
        setNextCursor(result.next_cursor);
      } catch (caught) {
        setError(toApiError(caught, "Could not load runs."));
      } finally {
        setLoading(false);
      }
    },
    [connected, workspaceId, accessToken, status, agentVersionId],
  );

  // Deep links such as /runs?status=NEEDS_ATTENTION must preload the filters.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const urlStatus = params.get("status") ?? "";
    const urlVersion = params.get("agent_version_id") ?? "";
    if (urlStatus || urlVersion) {
      setStatus(urlStatus);
      setAgentVersionId(urlVersion);
    }
  }, []);

  useEffect(() => {
    if (connected && !initialLoaded) {
      setInitialLoaded(true);
      void refresh();
    }
  }, [connected, initialLoaded, refresh]);

  useEffect(() => {
    if (!connected) setInitialLoaded(false);
  }, [connected]);

  function applyFilters() {
    const params = new URLSearchParams();
    if (status.trim()) params.set("status", status.trim());
    if (agentVersionId.trim()) params.set("agent_version_id", agentVersionId.trim());
    const query = params.toString();
    window.history.replaceState(null, "", query ? `/runs?${query}` : "/runs");
    setInitialLoaded(true);
    void refresh();
  }

  function clearFilters() {
    setStatus("");
    setAgentVersionId("");
    window.history.replaceState(null, "", "/runs");
    setInitialLoaded(true);
    void refresh(null, { status: "", agentVersionId: "" });
  }

  if (!connected) {
    return (
      <div className="page">
        <header className="page-header">
          <p className="eyebrow">RUNS</p>
          <h1>Runs</h1>
          <p className="page-lede">
            Workspace-scoped runtime history with safe status, usage, cost and approval projections.
          </p>
        </header>
        <SessionRequired context="the run history" />
      </div>
    );
  }

  return (
    <div className="page">
      <header className="page-header">
        <p className="eyebrow">RUNS</p>
        <h1>Runs</h1>
        <p className="page-lede">
          Workspace-scoped runtime history with safe status, usage, cost and approval projections.
        </p>
      </header>

      <section className="panel" aria-label="Run filters">
        <div className="runs-toolbar">
          <label>
            Status
            <select value={status} onChange={(event) => setStatus(event.target.value)}>
              <option value="">All statuses</option>
              {RUN_STATUS_OPTIONS.map((option) => (
                <option value={option} key={option}>
                  {option}
                </option>
              ))}
            </select>
          </label>
          <label>
            Agent Version ID
            <input
              value={agentVersionId}
              onChange={(event) => setAgentVersionId(event.target.value)}
              placeholder="Leave empty for all versions"
              spellCheck={false}
            />
          </label>
          <div className="runs-toolbar-actions">
            <button type="button" className="button button-primary" onClick={applyFilters} disabled={loading}>
              Apply
            </button>
            <button type="button" className="button button-ghost" onClick={clearFilters} disabled={loading}>
              Clear
            </button>
          </div>
        </div>
      </section>

      {error && <ErrorState code={error.code} message={error.message} hint={errorHint(error)} onRetry={() => void refresh()} />}
      {loading && runs.length === 0 && !error && <LoadingState label="Loading runs…" />}
      {!loading && runs.length === 0 && !error && (
        <EmptyState
          title="No runs found."
          hint={status || agentVersionId ? "No runs match the current filters. Clear them to see everything." : "Runs appear here once agents execute in this workspace."}
        />
      )}

      {runs.length > 0 && (
        <section className="panel" aria-label="Run list">
          <div className="data-table">
            <table>
              <thead>
                <tr>
                  <th scope="col">Run</th>
                  <th scope="col">Status</th>
                  <th scope="col">Version</th>
                  <th scope="col">Started</th>
                  <th scope="col">Duration</th>
                  <th scope="col">Tokens</th>
                  <th scope="col">Cost</th>
                  <th scope="col">Tools</th>
                  <th scope="col">Failure</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((run) => (
                  <tr key={run.id}>
                    <td data-label="Run">
                      <Link href={`/runs/${encodeURIComponent(run.id)}`} title={run.id}>
                        <code>{shortId(run.id)}</code>
                      </Link>
                    </td>
                    <td data-label="Status"><StatusBadge status={run.status} /></td>
                    <td data-label="Version">v{run.agent_version_number}</td>
                    <td data-label="Started">{formatStarted(run)}</td>
                    <td data-label="Duration">{run.duration_ms === null ? "—" : `${run.duration_ms} ms`}</td>
                    <td data-label="Tokens">{run.total_tokens ?? "—"}</td>
                    <td data-label="Cost">{formatCost(run)}</td>
                    <td data-label="Tools">{run.tool_call_count}</td>
                    <td data-label="Failure">
                      {run.failure_code ? (
                        <span>
                          {run.failure_category ?? "UNKNOWN"}: <code>{run.failure_code}</code>
                        </span>
                      ) : (
                        "—"
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {nextCursor && (
            <div className="form-actions">
              <button type="button" className="button button-ghost" onClick={() => void refresh(nextCursor)} disabled={loading}>
                {loading ? "Loading…" : "Load more"}
              </button>
            </div>
          )}
        </section>
      )}
    </div>
  );
}
