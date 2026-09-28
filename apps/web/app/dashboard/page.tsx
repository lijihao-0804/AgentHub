"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import MetricCard, { windowDelta } from "@/components/ui/metric-card";
import StatusBadge from "@/components/ui/status-badge";
import { EmptyState, ErrorState, LoadingState, Panel, SessionRequired } from "@/components/ui/states";
import { ApiError, errorHintKey, toApiError } from "@/lib/api/client";
import {
  AgentVersionBreakdown,
  FailureAnalytics,
  getAgentVersionBreakdown,
  getObservabilityFailures,
  getObservabilitySummary,
  getObservabilityTimeseries,
  ObservabilitySummary,
  TimeseriesResponse,
} from "@/lib/api/observability";
import { useFrontendSession } from "@/components/providers/session-provider";
import { useI18n } from "@/i18n/provider";

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

/** Poll cadence for the live ops cards while runs are in flight. */
const DASHBOARD_POLL_MS = 5000;

function windowFromUrl(): string {
  if (typeof window === "undefined") return "7";
  const param = new URLSearchParams(window.location.search).get("days") ?? "7";
  return WINDOW_OPTIONS.some((option) => option.value === param) ? param : "7";
}

export default function DashboardPage() {
  const {
    t,
    statusLabel,
    failureCategoryLabel,
    formatNumber,
    formatCount,
    formatDurationMs,
    formatPercent,
    formatCurrencyAmount,
  } = useI18n();
  const { workspaceId, accessToken, connected } = useFrontendSession();
  // null until the URL has been read, so a deep link such as /dashboard?days=30
  // configures the window before the first load fires.
  const [days, setDays] = useState<string | null>(null);
  const [summary, setSummary] = useState<ObservabilitySummary | null>(null);
  const [failures, setFailures] = useState<FailureAnalytics | null>(null);
  const [timeseries, setTimeseries] = useState<TimeseriesResponse | null>(null);
  const [versions, setVersions] = useState<AgentVersionBreakdown | null>(null);
  const [selectedCategory, setSelectedCategory] = useState<string | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(false);
  /** Generation shared by the full load and failures-only reloads, so a poll
   * can never land on top of a newer drill-down (or vice versa). */
  const generationRef = useRef(0);
  /** Live drill-down selection; a full load must re-apply, not clear it. */
  const selectedCategoryRef = useRef<string | null>(null);
  /** True while a full load is on the wire, so polls cannot stack up. */
  const loadingRef = useRef(false);

  const load = useCallback(
    async (dayOverride?: string) => {
      if (!connected) return;
      const effectiveDays = dayOverride ?? days;
      if (!effectiveDays) return;
      const generation = (generationRef.current += 1);
      // The user's failure drill-down survives every refresh and poll: the
      // failures endpoint is re-pulled with the selected category instead of
      // being reset to "all categories".
      const category = selectedCategoryRef.current;
      setError(null);
      const window = queryForDays(Number(effectiveDays));
      loadingRef.current = true;
      setLoading(true);
      try {
        const input = { workspaceId, accessToken, ...window };
        const [nextSummary, nextTimeseries, nextFailures, nextVersions] = await Promise.all([
          getObservabilitySummary(input),
          getObservabilityTimeseries({ ...input, bucket: Number(effectiveDays) <= 1 ? "hour" : "day" }),
          getObservabilityFailures({ ...input, ...(category ? { category } : {}) }),
          getAgentVersionBreakdown(input),
        ]);
        if (generationRef.current !== generation) return;
        setSummary(nextSummary);
        setTimeseries(nextTimeseries);
        setFailures(nextFailures);
        setVersions(nextVersions);
        setSelectedCategory(category);
      } catch (caught) {
        if (generationRef.current !== generation) return;
        setError(toApiError(caught, ""));
      } finally {
        if (generationRef.current === generation) {
          loadingRef.current = false;
          setLoading(false);
        }
      }
    },
    [connected, workspaceId, accessToken, days],
  );

  /** Category drill-down re-pulls only the failures endpoint. */
  const loadFailures = useCallback(
    async (category: string | null) => {
      if (!connected) return;
      const generation = (generationRef.current += 1);
      selectedCategoryRef.current = category;
      const window = queryForDays(Number(days ?? "7"));
      try {
        const nextFailures = await getObservabilityFailures({
          workspaceId,
          accessToken,
          ...window,
          ...(category ? { category } : {}),
        });
        if (generationRef.current !== generation) return;
        setFailures(nextFailures);
        setSelectedCategory(category);
      } catch (caught) {
        if (generationRef.current !== generation) return;
        setError(toApiError(caught, ""));
      }
    },
    [connected, workspaceId, accessToken, days],
  );

  // Read the shared time window from the URL before the first load, and keep
  // the URL in sync afterwards so a view can be linked or reloaded.
  useEffect(() => {
    setDays(windowFromUrl());
  }, []);

  const changeDays = useCallback((nextDays: string) => {
    setDays(nextDays);
    const params = new URLSearchParams(window.location.search);
    params.set("days", nextDays);
    window.history.replaceState(null, "", `/dashboard?${params.toString()}`);
  }, []);

  useEffect(() => {
    if (connected && days !== null) void load();
  }, [connected, days, load]);

  const activeOps =
    summary !== null &&
    (summary.current.running_count > 0 ||
      summary.current.waiting_approval_count > 0 ||
      summary.current.needs_attention_count > 0);

  // Live ops must not need a manual refresh: poll while anything is in
  // flight, stop when the workspace is quiet, and skip hidden tabs.
  useEffect(() => {
    if (!connected || !activeOps || days === null) return;
    const interval = window.setInterval(() => {
      if (!document.hidden && !loadingRef.current) void load();
    }, DASHBOARD_POLL_MS);
    return () => window.clearInterval(interval);
  }, [connected, activeOps, days, load]);

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
          <select value={days ?? "7"} onChange={(event) => changeDays(event.target.value)}>
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
          {summary.success_rate.denominator === 0 && (
            <Panel title={t("dashboard.onboarding.title")} eyebrow={t("dashboard.onboarding.eyebrow")}>
              <p className="state-hint">{t("dashboard.onboarding.lede")}</p>
              <div className="split-list">
                <Link className="stat-row" href="/settings/models">
                  <span className="stat-row-lead">{t("dashboard.onboarding.step1")}</span>
                  <span className="stat-row-meta stat-row-meta-end">{t("dashboard.onboarding.step1Hint")}</span>
                </Link>
                <Link className="stat-row" href="/agents">
                  <span className="stat-row-lead">{t("dashboard.onboarding.step2")}</span>
                  <span className="stat-row-meta stat-row-meta-end">{t("dashboard.onboarding.step2Hint")}</span>
                </Link>
                <Link className="stat-row" href="/knowledge">
                  <span className="stat-row-lead">{t("dashboard.onboarding.step3")}</span>
                  <span className="stat-row-meta stat-row-meta-end">{t("dashboard.onboarding.step3Hint")}</span>
                </Link>
              </div>
            </Panel>
          )}
          <section className="kpi-grid" aria-label={t("dashboard.title")}>
            <MetricCard
              label={t("dashboard.kpi.totalRuns")}
              value={formatCount(summary.finished_runs.denominator)}
              href="/runs"
              delta={windowDelta((timeseries?.items ?? []).map((item) => item.runs))}
              deltaLabel={t("dashboard.kpi.deltaLabel")}
              sparkline={(timeseries?.items ?? []).map((item) => item.runs)}
            />
            <MetricCard
              label={t("dashboard.kpi.failedRuns")}
              value={formatCount(summary.finished_runs.failed)}
              href="/runs?status=FAILED"
              emphasis={summary.finished_runs.failed > 0}
              delta={windowDelta((timeseries?.items ?? []).map((item) => item.failed))}
              deltaLabel={t("dashboard.kpi.deltaLabel")}
              deltaInverse
              sparkline={(timeseries?.items ?? []).map((item) => item.failed)}
            />
            <MetricCard
              label={t("dashboard.kpi.successRate")}
              value={summary.success_rate.rate === null ? "—" : formatPercent(summary.success_rate.rate)}
              hint={t("dashboard.kpi.successHint", {
                numerator: formatCount(summary.success_rate.numerator),
                denominator: formatCount(summary.success_rate.denominator),
              })}
            />
            <MetricCard
              label={t("dashboard.kpi.p95Latency")}
              value={summary.latency.p95_ms === null ? "—" : formatDurationMs(summary.latency.p95_ms)}
              hint={t("dashboard.kpi.p95Hint", {
                p50: summary.latency.p50_ms === null ? "—" : formatDurationMs(summary.latency.p50_ms),
                avg: summary.latency.avg_ms === null ? "—" : formatDurationMs(summary.latency.avg_ms),
                count: formatCount(summary.latency.sample_count),
              })}
            />
            <MetricCard
              label={t("dashboard.kpi.tokensPerRun")}
              value={summary.usage.avg_tokens_per_run === null ? "—" : formatCount(summary.usage.avg_tokens_per_run)}
              hint={
                <>
                  {t("dashboard.kpi.tokensHint", {
                    known: formatCount(summary.usage.known_usage_count),
                    unknown: formatCount(summary.usage.unknown_usage_count),
                  })}
                  <br />
                  {t("dashboard.kpi.tokensBreakdown", {
                    input: summary.usage.avg_input_tokens === null ? t("common.unknown") : formatCount(summary.usage.avg_input_tokens),
                    output: summary.usage.avg_output_tokens === null ? t("common.unknown") : formatCount(summary.usage.avg_output_tokens),
                    cached: summary.usage.avg_cached_tokens === null ? t("common.unknown") : formatCount(summary.usage.avg_cached_tokens),
                  })}
                </>
              }
            />
            <MetricCard
              label={t("dashboard.kpi.costPerSuccess")}
              value={
                summary.cost.mixed_currency
                  ? t("dashboard.cost.mixed")
                  : summary.cost.cost_per_successful_run === null
                    ? t("common.unknown")
                    : formatCurrencyAmount(summary.cost.cost_per_successful_run, null)
              }
              unit={
                summary.cost.mixed_currency || summary.cost.cost_per_successful_run === null
                  ? null
                  : summary.cost.currency
              }
              hint={
                summary.cost.total_cost === null || summary.cost.mixed_currency
                  ? t("dashboard.kpi.costHint", { count: formatCount(summary.cost.successful_cost_denominator) })
                  : t("dashboard.kpi.costHintTotal", {
                      total: formatCurrencyAmount(summary.cost.total_cost, null),
                      average: summary.cost.avg_cost_per_run === null
                        ? t("common.unknown")
                        : formatCurrencyAmount(summary.cost.avg_cost_per_run, null),
                      count: formatCount(summary.cost.successful_cost_denominator),
                    })
              }
            />
          </section>

          <section className="op-grid" aria-label={t("dashboard.title")}>
            <MetricCard label={t("dashboard.ops.running")} value={formatCount(summary.current.running_count)} href="/runs?status=RUNNING" />
            <MetricCard
              label={t("dashboard.ops.waitingApproval")}
              value={formatCount(summary.current.waiting_approval_count)}
              href="/approvals"
              emphasis={summary.current.waiting_approval_count > 0}
            />
            <MetricCard
              label={t("dashboard.ops.cancelRequested")}
              value={formatCount(summary.current.cancel_requested_count)}
              href="/runs?status=CANCEL_REQUESTED"
              emphasis={summary.current.cancel_requested_count > 0}
            />
            <MetricCard
              label={t("dashboard.ops.needsAttention")}
              value={formatCount(summary.current.needs_attention_count)}
              href="/runs?status=NEEDS_ATTENTION"
              emphasis={summary.current.needs_attention_count > 0}
            />
            <MetricCard
              label={t("dashboard.ops.unknownOutcome")}
              value={formatCount(summary.current.unknown_outcome_action_count)}
              hint={t("dashboard.ops.unknownOutcomeHint")}
              href="/runs?status=NEEDS_ATTENTION"
              emphasis={summary.current.unknown_outcome_action_count > 0}
            />
          </section>

          <Panel title={t("dashboard.approvals.title")} eyebrow={t("dashboard.approvals.eyebrow")}>
            <div className="run-facts">
              <span>{t("dashboard.approvals.decisionTotal")}<strong>{formatCount(summary.approvals.approval_total)}</strong></span>
              <span>{t("dashboard.approvals.waitP50")}<strong>{summary.approvals.wait_latency.p50_ms === null ? "—" : formatDurationMs(summary.approvals.wait_latency.p50_ms)}</strong></span>
              <span>{t("dashboard.approvals.waitP95")}<strong>{summary.approvals.wait_latency.p95_ms === null ? "—" : formatDurationMs(summary.approvals.wait_latency.p95_ms)}</strong></span>
            </div>
            <div className="approval-chips">
              <StatusBadge status="PENDING" label={t("run.chips.pending", { count: formatCount(summary.approvals.pending) })} />
              <StatusBadge status="APPROVED" label={t("run.chips.approved", { count: formatCount(summary.approvals.approved) })} />
              <StatusBadge status="DENIED" label={t("run.chips.denied", { count: formatCount(summary.approvals.denied) })} />
              <StatusBadge status="EXPIRED" label={t("dashboard.approvals.expired", { count: formatCount(summary.approvals.expired) })} />
              <StatusBadge status="CANCELLED" label={t("dashboard.approvals.cancelled", { count: formatCount(summary.approvals.cancelled) })} />
            </div>
            <p className="state-hint">{t("dashboard.approvals.executionDistribution")}</p>
            <div className="approval-chips">
              <StatusBadge status="NOT_STARTED" label={t("dashboard.approvals.executionNotStarted", { count: formatCount(summary.approvals.execution_not_started) })} />
              <StatusBadge status="CLAIMED" label={t("dashboard.approvals.executionClaimed", { count: formatCount(summary.approvals.claimed) })} />
              <StatusBadge status="SUCCEEDED" label={t("dashboard.approvals.executionSucceeded", { count: formatCount(summary.approvals.succeeded) })} />
              <StatusBadge status="FAILED" label={t("dashboard.approvals.executionFailed", { count: formatCount(summary.approvals.failed) })} />
              <StatusBadge status="UNKNOWN_OUTCOME" label={t("dashboard.approvals.executionUnknown", { count: formatCount(summary.approvals.unknown_outcome) })} />
            </div>
          </Panel>

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
                        void loadFailures(selectedCategory === item.failure_category ? null : item.failure_category)
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
                    <div className="stat-row" key={item.currency}>
                      <span className="stat-row-lead">{item.currency}</span>
                      <span className="stat-row-value" title={item.total_cost === null ? undefined : String(item.total_cost)}>
                        {item.total_cost === null ? t("common.unknown") : formatCurrencyAmount(item.total_cost, null)}
                      </span>
                      <span className="stat-row-meta">
                        {t("dashboard.cost.samples", {
                          estimated: formatCount(item.estimated_count),
                          exact: formatCount(item.exact_count),
                        })}
                      </span>
                      <span className="stat-row-meta stat-row-meta-end">
                        {t("dashboard.cost.perSuccess", {
                          value:
                            item.cost_per_successful_run === null
                              ? t("common.unknown")
                              : formatCurrencyAmount(item.cost_per_successful_run, null),
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
              <EmptyState title={t("dashboard.trend.empty")} />
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
                      className="stat-row"
                      href={`/runs?agent_version_id=${encodeURIComponent(item.agent_version_id)}`}
                      key={item.agent_version_id}
                    >
                      <span className="stat-row-lead">v{item.version_number}</span>
                      <span className="stat-row-value">
                        {t(item.run_count === 1 ? "dashboard.versions.runCountOne" : "dashboard.versions.runCountOther", {
                          count: formatCount(item.run_count),
                        })}
                      </span>
                      <span className="stat-row-meta">
                        <StatusBadge
                          status="SUCCEEDED"
                          label={`${statusLabel("SUCCEEDED")} ${formatCount(item.success_count)}`}
                        />{" "}
                        <StatusBadge
                          status="FAILED"
                          label={`${statusLabel("FAILED")} ${formatCount(item.failed_count)}`}
                        />{" "}
                        <StatusBadge
                          status="NEEDS_ATTENTION"
                          label={`${statusLabel("NEEDS_ATTENTION")} ${formatCount(item.needs_attention_count)}`}
                        />
                      </span>
                      <span className="stat-row-meta stat-row-meta-end">
                        {item.p95_latency_ms === null ? "p95 —" : `p95 ${formatDurationMs(item.p95_latency_ms)}`}
                        {" · "}
                        {item.cost_by_currency.length === 0
                          ? t("dashboard.versions.costUnknown")
                          : item.cost_by_currency
                              .map((c) =>
                                c.total_cost === null
                                  ? t("common.unknown")
                                  : formatCurrencyAmount(c.total_cost, c.currency),
                              )
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
                    <Link className="stat-row" href={`/runs/${encodeURIComponent(item.run_id)}`} key={item.run_id}>
                      <span className="stat-row-lead">{failureCategoryLabel(item.failure_category)}</span>
                      <span className="stat-row-value">
                        <code>{item.failure_code}</code>
                      </span>
                      <span className="stat-row-meta">
                        v{item.agent_version_number} · {statusLabel(item.status)}
                      </span>
                      <span className="stat-row-meta stat-row-meta-end">
                        {item.action_failure_code
                          ? t("dashboard.failureRuns.actionPrefix", { code: item.action_failure_code })
                          : ""}
                      </span>
                    </Link>
                  ))}
                  <div className="stat-row">
                    <Link className="button button-ghost" href="/runs?status=FAILED">
                      {t("dashboard.failureRuns.viewAll")}
                    </Link>
                  </div>
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
  const { t, formatNumber, formatCount, formatUTCBucketDate, formatCurrencyAmount } = useI18n();
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
        preserveAspectRatio="xMinYMax meet"
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
                  runs: formatCount(item.runs),
                  succeeded: formatCount(item.succeeded),
                  failed: formatCount(item.failed),
                  needsAttention: formatCount(item.needs_attention),
                  tokens: item.tokens === null ? t("common.unknown") : formatCount(item.tokens),
                  cost:
                    item.cost_by_currency.length === 0
                      ? t("common.unknown")
                      : item.cost_by_currency
                          .map((entry) =>
                            entry.total_cost === null
                              ? t("common.unknown")
                              : formatCurrencyAmount(entry.total_cost, entry.currency),
                          )
                          .join(", "),
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
