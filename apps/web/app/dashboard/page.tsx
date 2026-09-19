"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import MetricCard from "../../components/metric-card";
import { EmptyState, ErrorState, LoadingState, Panel, SessionRequired } from "../../components/states";
import { ApiError, errorHint, toApiError } from "../../lib/api-client";
import {
  AgentVersionBreakdown,
  FailureAnalytics,
  getAgentVersionBreakdown,
  getObservabilityFailures,
  getObservabilitySummary,
  getObservabilityTimeseries,
  ObservabilitySummary,
  TimeseriesResponse,
} from "../../lib/observability";
import { useFrontendSession } from "../../components/session-provider";

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
  const { workspaceId, accessToken, connected } = useFrontendSession();
  const [days, setDays] = useState("7");
  const [summary, setSummary] = useState<ObservabilitySummary | null>(null);
  const [failures, setFailures] = useState<FailureAnalytics | null>(null);
  const [timeseries, setTimeseries] = useState<TimeseriesResponse | null>(null);
  const [versions, setVersions] = useState<AgentVersionBreakdown | null>(null);
  const [selectedCategory, setSelectedCategory] = useState<string | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(
    async (category?: string, dayOverride?: string) => {
      setError(null);
      if (!connected) return;
      const window = queryForDays(Number(dayOverride ?? days));
      setLoading(true);
      try {
        const input = { workspaceId, accessToken, ...window };
        const [nextSummary, nextTimeseries, nextFailures, nextVersions] = await Promise.all([
          getObservabilitySummary(input),
          getObservabilityTimeseries({ ...input, bucket: Number(dayOverride ?? days) <= 1 ? "hour" : "day" }),
          getObservabilityFailures({ ...input, category }),
          getAgentVersionBreakdown(input),
        ]);
        setSummary(nextSummary);
        setTimeseries(nextTimeseries);
        setFailures(nextFailures);
        setVersions(nextVersions);
        setSelectedCategory(category ?? null);
      } catch (caught) {
        setError(toApiError(caught, "Could not load observability data."));
      } finally {
        setLoading(false);
      }
    },
    [connected, workspaceId, accessToken, days],
  );

  useEffect(() => {
    if (connected) void load();
  }, [connected, load]);

  if (!connected) {
    return (
      <div className="page">
        <header className="page-header">
          <p className="eyebrow">OBSERVABILITY</p>
          <h1>Workspace dashboard</h1>
        </header>
        <SessionRequired context="the workspace dashboard" />
      </div>
    );
  }

  return (
    <div className="page">
      <header className="page-header">
        <p className="eyebrow">OBSERVABILITY</p>
        <h1>Workspace dashboard</h1>
        <p className="page-lede">
          Bounded success, latency, usage, cost and failure analytics. Percentages always show
          their sample denominator; raw prompts and tool payloads never enter this view.
        </p>
      </header>

      <section className="runs-toolbar" aria-label="Dashboard window">
          <label>
          Window
          <select value={days} onChange={(event) => setDays(event.target.value)}>
            <option value="1">24 hours</option>
            <option value="7">7 days</option>
            <option value="30">30 days</option>
            <option value="90">90 days</option>
          </select>
        </label>
        <div className="runs-toolbar-actions">
          <button type="button" className="button button-primary" onClick={() => void load()} disabled={loading}>
            {loading ? "Loading…" : "Refresh"}
          </button>
        </div>
      </section>

      {error && <ErrorState code={error.code} message={error.message} hint={errorHint(error)} onRetry={() => void load()} />}
      {loading && !summary && <LoadingState label="Loading dashboard…" />}

      {summary && (
        <>
          <section className="kpi-grid" aria-label="Summary metrics">
            <MetricCard label="Success rate" value={rate(summary.success_rate)} hint={`${summary.success_rate.numerator} / ${summary.success_rate.denominator} finished`} />
            <MetricCard label="p95 latency" value={metric(summary.latency.p95_ms, " ms")} hint={`p50 ${metric(summary.latency.p50_ms, " ms")} · ${summary.latency.sample_count} samples`} />
            <MetricCard label="Tokens / run" value={metric(summary.usage.avg_tokens_per_run)} hint={`${summary.usage.known_usage_count} known · ${summary.usage.unknown_usage_count} unknown`} />
            <MetricCard label="Cost / successful run" value={summary.cost.mixed_currency ? "Mixed" : cost(summary.cost.cost_per_successful_run, summary.cost.currency)} hint={`${summary.cost.successful_cost_denominator} successful cost samples`} />
          </section>

          <section className="op-grid" aria-label="Current operational state">
            <MetricCard label="Running" value={summary.current.running_count} href="/runs?status=RUNNING" />
            <MetricCard label="Waiting approval" value={summary.current.waiting_approval_count} href="/approvals" emphasis={summary.current.waiting_approval_count > 0} />
            <MetricCard label="Needs attention" value={summary.current.needs_attention_count} href="/runs?status=NEEDS_ATTENTION" emphasis={summary.current.needs_attention_count > 0} />
            <MetricCard label="Unknown outcome" value={summary.current.unknown_outcome_action_count} hint="Actions whose result could not be confirmed" href="/runs?status=NEEDS_ATTENTION" emphasis={summary.current.unknown_outcome_action_count > 0} />
          </section>

          <div className="dashboard-columns">
            <Panel title="Failure categories" eyebrow="FAILURE DISTRIBUTION">
              {!failures || failures.categories.length === 0 ? (
                <EmptyState title="No failures in this window." hint="Failed runs and their categories will appear here." />
              ) : (
                <div className="failure-bars">
                  {failures.categories.map((item) => (
                    <button
                      className="bar-row"
                      type="button"
                      key={item.failure_category}
                      aria-pressed={selectedCategory === item.failure_category}
                      onClick={() => void load(selectedCategory === item.failure_category ? undefined : item.failure_category)}
                    >
                      <span className="bar-row-name">{item.failure_category}</span>
                      <span className="bar-track">
                        <span
                          className="bar-fill"
                          style={{
                            width: `${item.percentage === null ? 0 : Math.max(item.percentage * 100, 1)}%`,
                            background: "var(--danger)",
                          }}
                        />
                      </span>
                      <span className="bar-row-value">
                        {item.count} · {item.percentage === null ? "—" : `${(item.percentage * 100).toFixed(1)}%`}
                      </span>
                    </button>
                  ))}
                </div>
              )}
            </Panel>

            <Panel title="Cost by currency" eyebrow="USAGE COST">
              {summary.cost.mixed_currency && <p className="state-hint">Mixed currencies detected — values are grouped per currency and never summed.</p>}
              {summary.cost.currencies.length === 0 ? (
                <EmptyState title="No cost data in this window." hint="Unknown cost is shown as Unknown, never as zero." />
              ) : (
                <div className="split-list">
                  {summary.cost.currencies.map((item) => (
                    <div className="split-row" key={item.currency}>
                      <span>{item.currency}</span>
                      <span>{cost(item.total_cost, null)}</span>
                      <span className="muted">{item.estimated_count} estimated · {item.exact_count} exact</span>
                      <span className="muted">/ success {cost(item.cost_per_successful_run, null)}</span>
                    </div>
                  ))}
                </div>
              )}
            </Panel>
          </div>

          <Panel title="Runs over time" eyebrow="TREND" actions={<span className="state-hint">UTC · {timeseries?.bucket ?? "day"}</span>}>
            {!timeseries || timeseries.items.length === 0 ? (
              <EmptyState title="No runs in this window." />
            ) : (
              <TimeseriesChart items={timeseries.items} />
            )}
          </Panel>

          <div className="dashboard-columns">
            <Panel title="AgentVersion breakdown" eyebrow="TRAFFIC BY VERSION" actions={<span className="state-hint">No winner ranking</span>}>
              {!versions || versions.items.length === 0 ? (
                <EmptyState title="No AgentVersion activity." />
              ) : (
                <div className="split-list">
                  {versions.items.map((item) => (
                    <Link className="split-row" href={`/runs?agent_version_id=${encodeURIComponent(item.agent_version_id)}`} key={item.agent_version_id}>
                      <span>v{item.version_number}</span>
                      <span>{item.run_count} runs</span>
                      <span className="muted">{item.success_count} ✓ · {item.failed_count} ✕ · {item.needs_attention_count} ⚠</span>
                      <span className="muted">p95 {metric(item.p95_latency_ms, " ms")}</span>
                      <span className="muted">
                        {item.cost_by_currency.length === 0
                          ? "cost unknown"
                          : item.cost_by_currency.map((c) => `${c.total_cost ?? "Unknown"} ${c.currency}`).join(" · ")}
                      </span>
                    </Link>
                  ))}
                </div>
              )}
            </Panel>

            <Panel title="Failure runs" eyebrow="RUN LIST" actions={<span className="state-hint">{selectedCategory ?? "All categories"}</span>}>
              {!failures || failures.items.length === 0 ? (
                <EmptyState title="No failure runs in this window." hint="Select a category above to filter." />
              ) : (
                <div className="split-list">
                  {failures.items.map((item) => (
                    <Link className="split-row" href={`/runs/${encodeURIComponent(item.run_id)}`} key={item.run_id}>
                      <span>{item.failure_category}</span>
                      <span><code>{item.failure_code}</code></span>
                      <span className="muted">v{item.agent_version_number} · {item.status}</span>
                      <span className="muted">
                        {item.action_failure_code ? `action ${item.action_failure_code}` : ""}
                      </span>
                    </Link>
                  ))}
                </div>
              )}
            </Panel>
          </div>
        </>
      )}
    </div>
  );
}

function TimeseriesChart({ items }: { items: TimeseriesResponse["items"] }) {
  const maxRuns = Math.max(...items.map((item) => item.runs), 1);
  const width = Math.max(items.length * 34, 120);
  const chartHeight = 130;
  const segmentColors: Array<[keyof Pick<TimeseriesResponse["items"][number], "succeeded" | "failed" | "needs_attention">, string]> = [
    ["succeeded", "var(--success)"],
    ["failed", "var(--danger)"],
    ["needs_attention", "var(--attention)"],
  ];

  return (
    <div>
      <svg
        className="trend-chart"
        viewBox={`0 0 ${width} ${chartHeight + 18}`}
        role="img"
        aria-label={`Runs per ${items.length > 0 ? "bucket" : "period"}: ${items
          .map((item) => `${new Date(item.bucket).toLocaleDateString()} ${item.succeeded} succeeded, ${item.failed} failed, ${item.needs_attention} needs attention`)
          .join("; ")}`}
      >
        {items.map((item, index) => {
          const x = index * 34 + 6;
          const barWidth = 22;
          const scale = chartHeight / maxRuns;
          let yOffset = chartHeight;
          return (
            <g key={item.bucket}>
              <title>
                {`${new Date(item.bucket).toLocaleString()} — ${item.runs} runs: ${item.succeeded} succeeded, ${item.failed} failed, ${item.needs_attention} needs attention, ${item.tokens ?? "unknown"} tokens`}
              </title>
              {segmentColors.map(([key, color]) => {
                const value = item[key];
                if (value <= 0) return null;
                const height = Math.max(value * scale, 1.5);
                yOffset -= height;
                return <rect key={key} x={x} y={yOffset} width={barWidth} height={height} fill={color} rx="2" />;
              })}
              {item.runs === 0 && (
                <rect x={x} y={chartHeight - 1} width={barWidth} height={1} fill="var(--border-strong)" />
              )}
              <text x={x + barWidth / 2} y={chartHeight + 12} textAnchor="middle" fontSize="8" fill="var(--text-muted)">
                {new Date(item.bucket).toLocaleDateString(undefined, { month: "short", day: "numeric" })}
              </text>
            </g>
          );
        })}
      </svg>
      <div className="trend-legend" aria-hidden="true">
        <span><span className="legend-swatch" style={{ background: "var(--success)" }} /> Succeeded</span>
        <span><span className="legend-swatch" style={{ background: "var(--danger)" }} /> Failed</span>
        <span><span className="legend-swatch" style={{ background: "var(--attention)" }} /> Needs attention</span>
      </div>
      <p className="trend-note">
        Peak {maxRuns} runs per bucket. Exact values:{" "}
        {items.map((item) => `${new Date(item.bucket).toLocaleDateString()} (${item.runs})`).join(", ")}.
      </p>
    </div>
  );
}
