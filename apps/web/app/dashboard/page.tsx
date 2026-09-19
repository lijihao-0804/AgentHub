"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import MetricCard from "../../components/metric-card";
import { EmptyState, ErrorState, LoadingState, Panel, SessionRequired } from "../../components/states";
import { ApiError, errorHintKey, toApiError } from "../../lib/api-client";
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
import { useI18n } from "../../i18n/provider";

type QueryState = { from?: string; to?: string };

function queryForDays(days: number): QueryState {
  const to = new Date();
  const from = new Date(to.getTime() - days * 24 * 60 * 60 * 1000);
  return { from: from.toISOString(), to: to.toISOString() };
}

const WINDOW_OPTIONS = [
  { value: "1", labelKey: "dashboard.windows.d1" },
  { value: "7", labelKey: "dashboard.windows.d7" },
  { value: "30", labelKey: "dashboard.windows.d30" },
  { value: "90", labelKey: "dashboard.windows.d90" },
] as const;

export default function DashboardPage() {
  const { t, statusLabel, failureCategoryLabel, formatNumber, formatPercent, formatUTCBucketDate, formatCurrencyAmount } = useI18n();
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
        setError(toApiError(caught, ""));
      } finally {
        setLoading(false);
      }
    },
    [connected, workspaceId, accessToken, days],
  );

  useEffect(() => {
    if (connected) void load();
  }, [connected, load]);

  const hint = error ? errorHintKey(error) : null;

  if (!connected) {
    return (
      <div className="page">
        <header className="page-header">
          <p className="eyebrow">{t("dashboard.eyebrow")}</p>
          <h1>{t("dashboard.title")}</h1>
        </header>
        <SessionRequired contextKey="session.context.dashboard" />
      </div>
    );
  }

  return (
    <div className="page">
      <header className="page-header">
        <p className="eyebrow">{t("dashboard.eyebrow")}</p>
        <h1>{t("dashboard.title")}</h1>
        <p className="page-lede">{t("dashboard.lede")}</p>
      </header>

      <section className="runs-toolbar" aria-label={t("dashboard.window")}>
        <label>
          {t("dashboard.window")}
          <select value={days} onChange={(event) => setDays(event.target.value)}>
            {WINDOW_OPTIONS.map((option) => (
              <option value={option.value} key={option.value}>
                {t(option.labelKey)}
              </option>
            ))}
          </select>
        </label>
        <div className="runs-toolbar-actions">
          <button type="button" className="button button-primary" onClick={() => void load()} disabled={loading}>
            {loading ? t("common.loading") : t("common.refresh")}
          </button>
        </div>
      </section>

      {error && (
        <ErrorState
          code={error.code}
          message={error.message || t("errors.loadDashboard")}
          hint={hint ? t(hint) : undefined}
          onRetry={() => void load()}
        />
      )}
      {loading && !summary && <LoadingState />}

      {summary && (
        <>
          <section className="kpi-grid" aria-label={t("dashboard.title")}>
            <MetricCard
              label={t("dashboard.kpi.successRate")}
              value={summary.success_rate.rate === null ? "—" : formatPercent(summary.success_rate.rate)}
              hint={t("dashboard.kpi.successHint", {
                numerator: summary.success_rate.numerator,
                denominator: summary.success_rate.denominator,
              })}
            />
            <MetricCard
              label={t("dashboard.kpi.p95Latency")}
              value={summary.latency.p95_ms === null ? "—" : `${formatNumber(summary.latency.p95_ms)} ms`}
              hint={t("dashboard.kpi.p95Hint", {
                p50: summary.latency.p50_ms === null ? "—" : `${formatNumber(summary.latency.p50_ms)} ms`,
                count: summary.latency.sample_count,
              })}
            />
            <MetricCard
              label={t("dashboard.kpi.tokensPerRun")}
              value={summary.usage.avg_tokens_per_run === null ? "—" : summary.usage.avg_tokens_per_run}
              hint={t("dashboard.kpi.tokensHint", {
                known: summary.usage.known_usage_count,
                unknown: summary.usage.unknown_usage_count,
              })}
            />
            <MetricCard
              label={t("dashboard.kpi.costPerSuccess")}
              value={
                summary.cost.mixed_currency
                  ? t("dashboard.cost.mixed")
                  : summary.cost.cost_per_successful_run === null
                    ? t("common.unknown")
                    : formatCurrencyAmount(summary.cost.cost_per_successful_run, summary.cost.currency)
              }
              hint={t("dashboard.kpi.costHint", { count: summary.cost.successful_cost_denominator })}
            />
          </section>

          <section className="op-grid" aria-label={t("dashboard.title")}>
            <MetricCard label={t("dashboard.ops.running")} value={summary.current.running_count} href="/runs?status=RUNNING" />
            <MetricCard
              label={t("dashboard.ops.waitingApproval")}
              value={summary.current.waiting_approval_count}
              href="/approvals"
              emphasis={summary.current.waiting_approval_count > 0}
            />
            <MetricCard
              label={t("dashboard.ops.needsAttention")}
              value={summary.current.needs_attention_count}
              href="/runs?status=NEEDS_ATTENTION"
              emphasis={summary.current.needs_attention_count > 0}
            />
            <MetricCard
              label={t("dashboard.ops.unknownOutcome")}
              value={summary.current.unknown_outcome_action_count}
              hint={t("dashboard.ops.unknownOutcomeHint")}
              href="/runs?status=NEEDS_ATTENTION"
              emphasis={summary.current.unknown_outcome_action_count > 0}
            />
          </section>

          <div className="dashboard-columns">
            <Panel title={t("dashboard.failures.title")} eyebrow={t("dashboard.failures.eyebrow")}>
              {!failures || failures.categories.length === 0 ? (
                <EmptyState title={t("dashboard.failures.empty")} hint={t("dashboard.failures.emptyHint")} />
              ) : (
                <div className="failure-bars">
                  {failures.categories.map((item) => (
                    <button
                      className="bar-row"
                      type="button"
                      key={item.failure_category}
                      aria-pressed={selectedCategory === item.failure_category}
                      onClick={() =>
                        void load(selectedCategory === item.failure_category ? undefined : item.failure_category)
                      }
                    >
                      <span className="bar-row-name">{failureCategoryLabel(item.failure_category)}</span>
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
                        {formatNumber(item.count)} · {item.percentage === null ? "—" : formatPercent(item.percentage)}
                      </span>
                    </button>
                  ))}
                </div>
              )}
            </Panel>

            <Panel title={t("dashboard.cost.title")} eyebrow={t("dashboard.cost.eyebrow")}>
              {summary.cost.mixed_currency && <p className="state-hint">{t("dashboard.cost.mixedNote")}</p>}
              {summary.cost.currencies.length === 0 ? (
                <EmptyState title={t("dashboard.cost.empty")} hint={t("dashboard.cost.emptyHint")} />
              ) : (
                <div className="split-list">
                  {summary.cost.currencies.map((item) => (
                    <div className="split-row" key={item.currency}>
                      <span>{item.currency}</span>
                      <span>{item.total_cost === null ? t("common.unknown") : formatCurrencyAmount(item.total_cost, null)}</span>
                      <span className="muted">
                        {t("dashboard.cost.samples", { estimated: item.estimated_count, exact: item.exact_count })}
                      </span>
                      <span className="muted">
                        {t("dashboard.cost.perSuccess", {
                          value: item.cost_per_successful_run === null ? t("common.unknown") : item.cost_per_successful_run,
                        })}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </Panel>
          </div>

          <Panel
            title={t("dashboard.trend.title")}
            eyebrow={t("dashboard.trend.eyebrow")}
            actions={<span className="state-hint">{t("common.utcBucket", { bucket: timeseries?.bucket ?? "day" })}</span>}
          >
            {!timeseries || timeseries.items.length === 0 ? (
              <EmptyState title={t("dashboard.failures.empty")} />
            ) : (
              <TimeseriesChart items={timeseries.items} />
            )}
          </Panel>

          <div className="dashboard-columns">
            <Panel
              title={t("dashboard.versions.title")}
              eyebrow={t("dashboard.versions.eyebrow")}
              actions={<span className="state-hint">{t("dashboard.versions.noRanking")}</span>}
            >
              {!versions || versions.items.length === 0 ? (
                <EmptyState title={t("dashboard.versions.empty")} />
              ) : (
                <div className="split-list">
                  {versions.items.map((item) => (
                    <Link
                      className="split-row"
                      href={`/runs?agent_version_id=${encodeURIComponent(item.agent_version_id)}`}
                      key={item.agent_version_id}
                    >
                      <span>v{item.version_number}</span>
                      <span>
                        {t(item.run_count === 1 ? "dashboard.versions.runCountOne" : "dashboard.versions.runCountOther", {
                          count: item.run_count,
                        })}
                      </span>
                      <span className="muted">
                        {item.success_count} ✓ · {item.failed_count} ✕ · {item.needs_attention_count} ⚠
                      </span>
                      <span className="muted">
                        {item.p95_latency_ms === null ? "p95 —" : `p95 ${formatNumber(item.p95_latency_ms)} ms`}
                      </span>
                      <span className="muted">
                        {item.cost_by_currency.length === 0
                          ? t("dashboard.versions.costUnknown")
                          : item.cost_by_currency
                              .map((c) => (c.total_cost === null ? t("common.unknown") : `${c.total_cost} ${c.currency}`))
                              .join(" · ")}
                      </span>
                    </Link>
                  ))}
                </div>
              )}
            </Panel>

            <Panel
              title={t("dashboard.failureRuns.title")}
              eyebrow={t("dashboard.failureRuns.eyebrow")}
              actions={
                <span className="state-hint">
                  {selectedCategory
                    ? failureCategoryLabel(selectedCategory)
                    : t("dashboard.failureRuns.allCategories")}
                </span>
              }
            >
              {!failures || failures.items.length === 0 ? (
                <EmptyState title={t("dashboard.failureRuns.empty")} hint={t("dashboard.failureRuns.emptyHint")} />
              ) : (
                <div className="split-list">
                  {failures.items.map((item) => (
                    <Link className="split-row" href={`/runs/${encodeURIComponent(item.run_id)}`} key={item.run_id}>
                      <span>{failureCategoryLabel(item.failure_category)}</span>
                      <span>
                        <code>{item.failure_code}</code>
                      </span>
                      <span className="muted">
                        v{item.agent_version_number} · {statusLabel(item.status)}
                      </span>
                      <span className="muted">
                        {item.action_failure_code
                          ? t("dashboard.failureRuns.actionPrefix", { code: item.action_failure_code })
                          : ""}
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
  const { t, formatNumber, formatUTCBucketDate } = useI18n();
  const maxRuns = Math.max(...items.map((item) => item.runs), 1);
  const width = Math.max(items.length * 34, 120);
  const chartHeight = 130;
  const segmentColors: Array<[keyof Pick<TimeseriesResponse["items"][number], "succeeded" | "failed" | "needs_attention">, string]> = [
    ["succeeded", "var(--success)"],
    ["failed", "var(--danger)"],
    ["needs_attention", "var(--attention)"],
  ];
  const ariaEntries = items
    .map((item) =>
      t("dashboard.trend.ariaEntry", {
        date: formatUTCBucketDate(item.bucket),
        succeeded: item.succeeded,
        failed: item.failed,
        needsAttention: item.needs_attention,
      }),
    )
    .join("; ");

  return (
    <div>
      <svg
        className="trend-chart"
        viewBox={`0 0 ${width} ${chartHeight + 18}`}
        role="img"
        aria-label={`${t("dashboard.trend.ariaPrefix")} ${ariaEntries}`}
      >
        {items.map((item, index) => {
          const x = index * 34 + 6;
          const barWidth = 22;
          const scale = chartHeight / maxRuns;
          let yOffset = chartHeight;
          return (
            <g key={item.bucket}>
              <title>
                {t("dashboard.trend.barTitle", {
                  date: formatUTCBucketDate(item.bucket),
                  runs: item.runs,
                  succeeded: item.succeeded,
                  failed: item.failed,
                  needsAttention: item.needs_attention,
                  tokens: item.tokens ?? t("common.unknown"),
                })}
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
                {formatUTCBucketDate(item.bucket)}
              </text>
            </g>
          );
        })}
      </svg>
      <div className="trend-legend" aria-hidden="true">
        <span><span className="legend-swatch" style={{ background: "var(--success)" }} /> {t("dashboard.trend.legendSucceeded")}</span>
        <span><span className="legend-swatch" style={{ background: "var(--danger)" }} /> {t("dashboard.trend.legendFailed")}</span>
        <span><span className="legend-swatch" style={{ background: "var(--attention)" }} /> {t("dashboard.trend.legendNeedsAttention")}</span>
      </div>
      <p className="trend-note">
        {t("dashboard.trend.note", {
          peak: formatNumber(maxRuns),
          values: items.map((item) => `${formatUTCBucketDate(item.bucket)} (${formatNumber(item.runs)})`).join(", "),
        })}
      </p>
    </div>
  );
}
