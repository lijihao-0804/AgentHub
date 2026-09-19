"use client";

import Link from "next/link";
import { useState } from "react";

import {
  AgentVersionBreakdown,
  FailureAnalytics,
  getAgentVersionBreakdown,
  getObservabilityFailures,
  getObservabilitySummary,
  getObservabilityTimeseries,
  ObservabilityApiError,
  ObservabilitySummary,
  TimeseriesResponse,
} from "../../lib/observability";

type QueryState = { from?: string; to?: string };

function metric(value: number | null, suffix = ""): string {
  return value === null || !Number.isFinite(value) ? "—" : `${value}${suffix}`;
}

function rate(value: { rate: number | null }): string {
  return value.rate === null ? "—" : `${(value.rate * 100).toFixed(1)}%`;
}

function cost(value: number | string | null, currency?: string | null): string {
  if (value === null || value === undefined) return "Unknown";
  return `${value} ${currency ?? ""}`.trim();
}

function queryForDays(days: number): QueryState {
  const to = new Date();
  const from = new Date(to.getTime() - days * 24 * 60 * 60 * 1000);
  return { from: from.toISOString(), to: to.toISOString() };
}

export default function DashboardPage() {
  const [workspaceId, setWorkspaceId] = useState("");
  const [accessToken, setAccessToken] = useState("");
  const [days, setDays] = useState("7");
  const [summary, setSummary] = useState<ObservabilitySummary | null>(null);
  const [failures, setFailures] = useState<FailureAnalytics | null>(null);
  const [timeseries, setTimeseries] = useState<TimeseriesResponse | null>(null);
  const [versions, setVersions] = useState<AgentVersionBreakdown | null>(null);
  const [selectedCategory, setSelectedCategory] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function load(category?: string) {
    setMessage(null);
    if (!workspaceId.trim() || !accessToken.trim()) {
      setMessage("Workspace ID and access token are required.");
      return;
    }
    const window = queryForDays(Number(days));
    setLoading(true);
    try {
      const input = { workspaceId, accessToken, ...window };
      const [nextSummary, nextTimeseries, nextFailures, nextVersions] = await Promise.all([
        getObservabilitySummary(input),
        getObservabilityTimeseries({ ...input, bucket: Number(days) <= 1 ? "hour" : "day" }),
        getObservabilityFailures({ ...input, category }),
        getAgentVersionBreakdown(input),
      ]);
      setSummary(nextSummary);
      setTimeseries(nextTimeseries);
      setFailures(nextFailures);
      setVersions(nextVersions);
      setSelectedCategory(category ?? null);
    } catch (error) {
      setMessage(
        error instanceof ObservabilityApiError ? error.message : "Could not load observability data.",
      );
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="dashboard-shell">
      <header className="observability-header">
        <div>
          <p className="eyebrow">M6-B · METRICS / FAILURE ANALYTICS</p>
          <h1>Workspace dashboard</h1>
          <p className="observability-lede">
            Read-only operational metrics from AgentHub PostgreSQL. Percentages always show their
            sample denominator; raw prompts, tool payloads and checkpoint data never enter this view.
          </p>
        </div>
        <Link className="back-link" href="/">
          Back to AgentHub
        </Link>
      </header>

      <section className="observability-panel" aria-labelledby="dashboard-query-title">
        <div className="panel-heading">
          <div>
            <p className="eyebrow">WORKSPACE-SCOPED QUERY</p>
            <h2 id="dashboard-query-title">Load metrics</h2>
          </div>
          <span className="badge">NO TOKEN STORAGE</span>
        </div>
        <p className="observability-note">
          The access token remains in React state only and disappears on refresh. The default window is seven days.
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
            Window
            <select value={days} onChange={(event) => setDays(event.target.value)}>
              <option value="1">24 hours</option>
              <option value="7">7 days</option>
              <option value="30">30 days</option>
              <option value="90">90 days</option>
            </select>
          </label>
          <button type="button" onClick={() => load()} disabled={loading}>
            {loading ? "Loading…" : "Load dashboard"}
          </button>
        </div>
        {message && <p className="state-message">{message}</p>}
      </section>

      {summary && (
        <>
          <section className="dashboard-grid" aria-label="Summary metrics">
            <article className="dashboard-card"><span>Success rate</span><strong>{rate(summary.success_rate)}</strong><small>{summary.success_rate.numerator} / {summary.success_rate.denominator} finished</small></article>
            <article className="dashboard-card"><span>Failure rate</span><strong>{rate(summary.failure_rate)}</strong><small>{summary.failure_rate.numerator} / {summary.failure_rate.denominator} finished</small></article>
            <article className="dashboard-card"><span>p50 / p95 latency</span><strong>{metric(summary.latency.p50_ms, " ms")}</strong><small>p95 {metric(summary.latency.p95_ms, " ms")} · {summary.latency.sample_count} samples</small></article>
            <article className="dashboard-card"><span>Tokens / run</span><strong>{metric(summary.usage.avg_tokens_per_run)}</strong><small>{summary.usage.known_usage_count} known · {summary.usage.unknown_usage_count} unknown</small></article>
            <article className="dashboard-card"><span>Cost / successful run</span><strong>{summary.cost.mixed_currency ? "Mixed" : cost(summary.cost.cost_per_successful_run, summary.cost.currency)}</strong><small>{summary.cost.successful_cost_denominator} successful cost samples</small></article>
            <article className="dashboard-card"><span>Needs attention</span><strong>{summary.current.needs_attention_count}</strong><small>{summary.current.unknown_outcome_action_count} unknown action outcomes</small></article>
          </section>

          <section className="dashboard-panel" aria-labelledby="operational-title">
            <div className="panel-heading"><h2 id="operational-title">Current operational state</h2><Link href="/">Open approval inbox →</Link></div>
            <div className="dashboard-stat-row">
              <span>Running <strong>{summary.current.running_count}</strong></span>
              <span>Waiting approval <strong>{summary.current.waiting_approval_count}</strong></span>
              <span>Cancel requested <strong>{summary.current.cancel_requested_count}</strong></span>
              <span>Needs attention <strong>{summary.current.needs_attention_count}</strong></span>
              <span>Unknown outcome <strong>{summary.current.unknown_outcome_action_count}</strong></span>
            </div>
          </section>

          <section className="dashboard-two-column">
            <div className="dashboard-panel">
              <div className="panel-heading"><h2>Failure categories</h2><span>{failures?.total_failure_runs ?? 0} runs</span></div>
              {!failures || failures.categories.length === 0 ? <p className="muted">No failures in this window.</p> : (
                <div className="dashboard-table">
                  {failures.categories.map((item) => (
                    <button className={`dashboard-table-row ${selectedCategory === item.failure_category ? "selected" : ""}`} key={item.failure_category} type="button" onClick={() => load(item.failure_category)}>
                      <span>{item.failure_category}</span><strong>{item.count}</strong><small>{item.percentage === null ? "—" : `${(item.percentage * 100).toFixed(1)}%`}</small>
                    </button>
                  ))}
                </div>
              )}
            </div>
            <div className="dashboard-panel">
              <div className="panel-heading"><h2>Cost by currency</h2><span>{summary.cost.mixed_currency ? "Grouped" : summary.cost.currency ?? "Unknown"}</span></div>
              {summary.cost.currencies.length === 0 ? <p className="muted">No known cost in this window.</p> : (
                <div className="dashboard-table">
                  {summary.cost.currencies.map((item) => <div className="dashboard-table-row" key={item.currency}><span>{item.currency}</span><strong>{cost(item.total_cost, item.currency)}</strong><small>{item.estimated_count} estimated · {item.exact_count} exact</small></div>)}
                </div>
              )}
            </div>
          </section>

          <section className="dashboard-panel">
            <div className="panel-heading"><h2>Runs over time</h2><span>UTC · {timeseries?.bucket ?? "day"}</span></div>
            {!timeseries || timeseries.items.length === 0 ? <p className="muted">No runs in this window.</p> : (
              <div className="dashboard-table">
                {timeseries.items.map((item) => <div className="dashboard-table-row" key={item.bucket}><span>{new Date(item.bucket).toLocaleString()}</span><strong>{item.runs} runs</strong><small>{item.succeeded} succeeded · {item.failed} failed · {item.needs_attention} attention · {item.tokens ?? "—"} tokens</small></div>)}
              </div>
            )}
          </section>

          <section className="dashboard-two-column">
            <div className="dashboard-panel">
              <div className="panel-heading"><h2>AgentVersion breakdown</h2><span>No winner ranking</span></div>
              {!versions || versions.items.length === 0 ? <p className="muted">No AgentVersion activity.</p> : <div className="dashboard-table">{versions.items.map((item) => <Link className="dashboard-table-row" href={`/runs?agent_version_id=${encodeURIComponent(item.agent_version_id)}`} key={item.agent_version_id}><span>v{item.version_number}</span><strong>{item.run_count} runs</strong><small>{item.success_count} success · {item.failed_count} failed · p95 {metric(item.p95_latency_ms, " ms")}</small></Link>)}</div>}
            </div>
            <div className="dashboard-panel">
              <div className="panel-heading"><h2>Failure runs</h2><span>{selectedCategory ?? "All categories"}</span></div>
              {!failures || failures.items.length === 0 ? <p className="muted">No failure runs in this window.</p> : <div className="dashboard-table">{failures.items.map((item) => <Link className="dashboard-table-row" href={`/runs/${encodeURIComponent(item.run_id)}`} key={item.run_id}><span>{item.failure_category}</span><strong>{item.failure_code}</strong><small>v{item.agent_version_number} · {item.status}{item.action_failure_code ? ` · action ${item.action_failure_code}` : ""}</small></Link>)}</div>}
            </div>
          </section>
        </>
      )}
    </main>
  );
}
