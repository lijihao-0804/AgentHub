"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";

import StatusBadge from "@/components/ui/status-badge";
import Breadcrumbs from "@/components/layout/breadcrumbs";
import { ErrorState, LoadingState, Panel, SessionRequired } from "@/components/ui/states";
import { useFrontendSession } from "@/components/providers/session-provider";
import { useWorkspaceData } from "@/hooks/use-workspace-data";
import { errorHintKey, type ApiError, type AuthInput } from "@/lib/api/client";
import { getAgentRun, type AgentRun } from "@/lib/api/agent-runtime";
import {
  getRunDetail,
  getRunTimeline,
  type RunDetail,
  type RunTimelineEntry,
} from "@/lib/api/runs";
import { useI18n } from "@/i18n/provider";

/**
 * One run, read from both contracts it lives in: the runtime record
 * (`/agent-runs/**`) is authoritative for input and final output, the
 * observability projection (`/runs/**`) for usage, cost and timeline.
 */
type RunSide = {
  agent: AgentRun;
  detail: RunDetail;
  timeline: RunTimelineEntry[];
};

/** Kinds worth showing as trajectory steps; model steps stay a metric. */
const TRAJECTORY_KINDS = new Set([
  "RUN_STARTED",
  "RETRIEVAL",
  "TOOL",
  "APPROVAL_WAIT",
  "APPROVAL_DECISION",
  "ACTION_EXECUTION",
  "FINISH",
  "FAILURE",
]);

function shortId(value: string): string {
  return `${value.slice(0, 8)}…`;
}

function toNumber(value: number | string | null | undefined): number | null {
  if (value === null || value === undefined) return null;
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function toolIdentity(entry: RunTimelineEntry): string | null {
  const meta = entry.metadata;
  if (typeof meta.tool_identity === "string") return meta.tool_identity;
  if (Array.isArray(meta.tool_identities)) {
    const first = meta.tool_identities.find((value): value is string => typeof value === "string");
    return first ?? null;
  }
  return null;
}

/**
 * Side-by-side inspection of two runs.
 *
 * Both sides are shown as facts. Nothing here declares a winner: two runs
 * of the same version and input can legitimately differ, and deciding
 * which output is better is what Evaluation exists for.
 */
export default function RunCompareClient() {
  const { t, timelineKindLabel, formatNumber, formatDurationMs, formatCurrencyAmount } = useI18n();
  const { connected, workspaceId, sessionId } = useFrontendSession();

  const [leftId, setLeftId] = useState("");
  const [rightId, setRightId] = useState("");
  const [leftDraft, setLeftDraft] = useState("");
  const [rightDraft, setRightDraft] = useState("");

  // The URL query is the whole persistence story: no comparison record is
  // created anywhere.
  useEffect(() => {
    const params =
      typeof window === "undefined" ? null : new URLSearchParams(window.location.search);
    const left = params?.get("left") ?? "";
    const right = params?.get("right") ?? "";
    setLeftId(left);
    setRightId(right);
    setLeftDraft(left);
    setRightDraft(right);
  }, [sessionId]);

  const apply = useCallback(() => {
    const left = leftDraft.trim();
    const right = rightDraft.trim();
    setLeftId(left);
    setRightId(right);
    if (typeof window === "undefined") return;
    const url = new URL(window.location.href);
    if (left) url.searchParams.set("left", left);
    else url.searchParams.delete("left");
    if (right) url.searchParams.set("right", right);
    else url.searchParams.delete("right");
    window.history.replaceState(null, "", `${url.pathname}${url.search}`);
  }, [leftDraft, rightDraft]);

  const comparable = leftId !== "" && rightId !== "" && leftId !== rightId;

  const loadLeft = useCallback((auth: AuthInput) => loadSide(auth, leftId), [leftId]);
  const loadRight = useCallback((auth: AuthInput) => loadSide(auth, rightId), [rightId]);

  const left = useWorkspaceData<RunSide>(
    loadLeft,
    comparable ? `run-compare:${workspaceId}:${leftId}` : "",
    { enabled: comparable },
  );
  const right = useWorkspaceData<RunSide>(
    loadRight,
    comparable ? `run-compare:${workspaceId}:${rightId}` : "",
    { enabled: comparable },
  );

  const metrics = useMemo(() => {
    if (!left.data || !right.data) return [];
    return buildMetrics(left.data.detail, right.data.detail, {
      formatNumber,
      formatDurationMs,
      formatCurrencyAmount,
      mixedCurrency: t("runCompare.metrics.mixedCurrency"),
      labels: {
        durationMs: t("runCompare.metrics.durationMs"),
        totalTokens: t("runCompare.metrics.totalTokens"),
        inputTokens: t("runCompare.metrics.inputTokens"),
        outputTokens: t("runCompare.metrics.outputTokens"),
        cachedTokens: t("runCompare.metrics.cachedTokens"),
        cost: t("runCompare.metrics.cost"),
        modelSteps: t("runCompare.metrics.modelSteps"),
        toolCalls: t("runCompare.metrics.toolCalls"),
        approvals: t("runCompare.metrics.approvals"),
      },
    });
  }, [left.data, right.data, t, formatNumber, formatDurationMs, formatCurrencyAmount]);

  if (!connected) {
    return (
      <div className="page">
        <header className="page-header">
          <p className="eyebrow">{t("runCompare.eyebrow")}</p>
          <h1>{t("runCompare.title")}</h1>
        </header>
        <SessionRequired contextKey="session.context.run" />
      </div>
    );
  }

  function renderError(error: ApiError, retry: () => void, side: string) {
    const hint = errorHintKey(error);
    return (
      <Panel ariaLabel={side} title={side}>
        <ErrorState
          code={error.code}
          message={error.message || t("errors.loadRunDetail")}
          hint={hint ? t(hint) : undefined}
          onRetry={retry}
        />
      </Panel>
    );
  }

  function sideHeader(side: RunSide, label: string): ReactNode {
    return (
      <div className="approval-state-block">
        <span className="approval-state-label">{label}</span>
        <span>
          <code title={side.detail.id}>{shortId(side.detail.id)}</code>{" "}
          <StatusBadge status={side.detail.status} />
        </span>
        <span className="approval-state-meta">
          {t("run.agentVersionEyebrow", { version: side.detail.agent_version_number })}
        </span>
        <span className="approval-state-meta">
          {t("runCompare.failure")}: {side.detail.failure_code ?? "—"}
        </span>
        <Link className="button button-ghost" href={`/runs/${side.detail.id}`}>
          {t("runCompare.openRun")}
        </Link>
      </div>
    );
  }

  function trajectory(side: RunSide): RunTimelineEntry[] {
    return side.timeline.filter((entry) => TRAJECTORY_KINDS.has(entry.kind));
  }

  function trajectoryList(side: RunSide, label: string): ReactNode {
    const steps = trajectory(side);
    return (
      <div className="approval-state-block">
        <span className="approval-state-label">{label}</span>
        {steps.length === 0 ? (
          <span className="muted">{t("runCompare.trajectory.empty")}</span>
        ) : (
          <ol className="trajectory-list">
            {steps.map((entry) => {
              const tool = toolIdentity(entry);
              return (
                <li key={`${entry.sequence}-${entry.kind}`}>
                  <span>{timelineKindLabel(entry.kind)}</span>{" "}
                  <StatusBadge status={entry.status} />
                  {tool && <span className="approval-state-meta"> {tool}</span>}
                </li>
              );
            })}
          </ol>
        )}
      </div>
    );
  }

  const leftSide = left.data;
  const rightSide = right.data;
  const bothLoaded = Boolean(leftSide && rightSide);
  // Two withheld inputs are both null, which is not evidence that they match:
  // a reader who cannot see either prompt must not be told they are the same.
  const sameInput = Boolean(
    leftSide &&
      rightSide &&
      leftSide.agent.input_text !== null &&
      leftSide.agent.input_text === rightSide.agent.input_text,
  );

  return (
    <div className="page">
      <Breadcrumbs items={[{ label: t("nav.runs"), href: "/runs" }, { label: t("runCompare.title") }]} />
      <header className="page-header">
        <p className="eyebrow">{t("runCompare.eyebrow")}</p>
        <h1>{t("runCompare.title")}</h1>
        <p className="page-lede">{t("runCompare.lede")}</p>
      </header>

      <Panel title={t("runCompare.title")} ariaLabel={t("runCompare.title")}>
        <div className="form-grid compare-selectors">
          <label>
            {t("runCompare.left")} · {t("runCompare.runIdLabel")}
            <input
              value={leftDraft}
              onChange={(event) => setLeftDraft(event.target.value)}
              placeholder={t("runCompare.runIdPlaceholder")}
            />
          </label>
          <label>
            {t("runCompare.right")} · {t("runCompare.runIdLabel")}
            <input
              value={rightDraft}
              onChange={(event) => setRightDraft(event.target.value)}
              placeholder={t("runCompare.runIdPlaceholder")}
            />
          </label>
        </div>
        <div className="form-actions">
          <button className="button button-primary" type="button" onClick={apply}>
            {t("runCompare.compare")}
          </button>
        </div>
        {(leftId === "" || rightId === "") && (
          <p className="state-hint">{t("runCompare.selectBoth")}</p>
        )}
        {leftId !== "" && leftId === rightId && (
          <p className="state-hint">{t("runCompare.sameRun")}</p>
        )}
      </Panel>

      {comparable && left.error && renderError(left.error, left.reload, t("runCompare.left"))}
      {comparable && right.error && renderError(right.error, right.reload, t("runCompare.right"))}
      {comparable && !left.error && !right.error && (left.loading || right.loading) && !bothLoaded && (
        <Panel ariaLabel={t("runCompare.title")}>
          <LoadingState />
        </Panel>
      )}

      {leftSide && rightSide && (
        <>
          <Panel title={t("runCompare.statusTitle")} eyebrow={t("runCompare.eyebrow")}>
            <p className="state-hint">
              {leftSide.detail.agent_version_number === rightSide.detail.agent_version_number
                ? t("runCompare.versionsSame", { version: leftSide.detail.agent_version_number })
                : t("runCompare.versionsDiffer", {
                    left: leftSide.detail.agent_version_number,
                    right: rightSide.detail.agent_version_number,
                  })}
            </p>
            <div className="approval-split">
              {sideHeader(leftSide, t("runCompare.left"))}
              {sideHeader(rightSide, t("runCompare.right"))}
            </div>
          </Panel>

          <Panel title={t("runCompare.input.title")}>
            <p className="state-hint">
              {sameInput ? t("runCompare.input.same") : t("runCompare.input.different")}
            </p>
            {sameInput ? (
              <p className="playground-output">
                {leftSide.agent.input_text || t("runCompare.input.empty")}
              </p>
            ) : (
              <div className="approval-split">
                <div className="approval-state-block">
                  <span className="approval-state-label">{t("runCompare.left")}</span>
                  <p className="playground-output">
                    {leftSide.agent.input_text || t("runCompare.input.empty")}
                  </p>
                </div>
                <div className="approval-state-block">
                  <span className="approval-state-label">{t("runCompare.right")}</span>
                  <p className="playground-output">
                    {rightSide.agent.input_text || t("runCompare.input.empty")}
                  </p>
                </div>
              </div>
            )}
          </Panel>

          <Panel title={t("runCompare.output.title")}>
            <div className="approval-split">
              <div className="approval-state-block">
                <span className="approval-state-label">{t("runCompare.left")}</span>
                <p className="playground-output">
                  {leftSide.agent.final_output ?? t("runCompare.output.empty")}
                </p>
              </div>
              <div className="approval-state-block">
                <span className="approval-state-label">{t("runCompare.right")}</span>
                <p className="playground-output">
                  {rightSide.agent.final_output ?? t("runCompare.output.empty")}
                </p>
              </div>
            </div>
          </Panel>

          <Panel title={t("runCompare.metrics.title")}>
            <p className="state-hint">{t("runCompare.metrics.neutralHint")}</p>
            <div className="data-table">
              <table>
                <thead>
                  <tr>
                    <th scope="col">{t("runCompare.metrics.metric")}</th>
                    <th scope="col">{t("runCompare.left")}</th>
                    <th scope="col">{t("runCompare.right")}</th>
                    <th scope="col">{t("runCompare.metrics.delta")}</th>
                  </tr>
                </thead>
                <tbody>
                  {metrics.map((row) => (
                    <tr key={row.key}>
                      <td>{row.label}</td>
                      <td>{row.left}</td>
                      <td>{row.right}</td>
                      <td>{row.delta}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Panel>

          <Panel title={t("runCompare.trajectory.title")}>
            <p className="state-hint">
              {t("runCompare.trajectory.steps", {
                left: trajectory(leftSide).length,
                right: trajectory(rightSide).length,
              })}
            </p>
            <div className="approval-split">
              {trajectoryList(leftSide, t("runCompare.left"))}
              {trajectoryList(rightSide, t("runCompare.right"))}
            </div>
          </Panel>
        </>
      )}

    </div>
  );
}

async function loadSide(auth: AuthInput, runId: string): Promise<RunSide> {
  const [agent, detail, timeline] = await Promise.all([
    getAgentRun(auth, runId),
    getRunDetail(auth.workspaceId, runId, auth.accessToken),
    getRunTimeline(auth.workspaceId, runId, auth.accessToken),
  ]);
  return { agent, detail, timeline: timeline.items };
}

type MetricRow = { key: string; label: string; left: string; right: string; delta: string };

function buildMetrics(
  left: RunDetail,
  right: RunDetail,
  options: {
    formatNumber: (value: number) => string;
    formatDurationMs: (value: number | null | undefined) => string;
    formatCurrencyAmount: (
      value: number | string | null | undefined,
      currency: string | null | undefined,
    ) => string;
    mixedCurrency: string;
    labels: Record<string, string>;
  },
): MetricRow[] {
  const { formatNumber, formatDurationMs, formatCurrencyAmount, labels } = options;

  const numeric = (
    key: string,
    label: string,
    leftValue: number | null,
    rightValue: number | null,
  ): MetricRow => ({
    key,
    label,
    left: leftValue === null ? "—" : formatNumber(leftValue),
    right: rightValue === null ? "—" : formatNumber(rightValue),
    // A delta only exists when both sides reported the metric.
    delta:
      leftValue === null || rightValue === null
        ? "—"
        : `${rightValue - leftValue > 0 ? "+" : ""}${formatNumber(rightValue - leftValue)}`,
  });

  const leftCost = toNumber(left.total_cost_amount);
  const rightCost = toNumber(right.total_cost_amount);
  const sameCurrency = left.cost_currency === right.cost_currency;
  const costDelta =
    leftCost === null || rightCost === null
      ? "—"
      : sameCurrency
        ? `${rightCost - leftCost > 0 ? "+" : ""}${formatCurrencyAmount(rightCost - leftCost, left.cost_currency)}`
        : options.mixedCurrency;

  // Duration carries its own unit, so it never goes through `numeric`.
  const durationDelta =
    left.duration_ms === null || right.duration_ms === null
      ? "—"
      : `${right.duration_ms - left.duration_ms > 0 ? "+" : ""}${formatDurationMs(
          right.duration_ms - left.duration_ms,
        )}`;

  return [
    {
      key: "durationMs",
      label: labels.durationMs,
      left: left.duration_ms === null ? "—" : formatDurationMs(left.duration_ms),
      right: right.duration_ms === null ? "—" : formatDurationMs(right.duration_ms),
      delta: durationDelta,
    },
    numeric("totalTokens", labels.totalTokens, left.total_tokens, right.total_tokens),
    numeric("inputTokens", labels.inputTokens, left.total_input_tokens, right.total_input_tokens),
    numeric("outputTokens", labels.outputTokens, left.total_output_tokens, right.total_output_tokens),
    numeric("cachedTokens", labels.cachedTokens, left.total_cached_tokens, right.total_cached_tokens),
    {
      key: "cost",
      label: labels.cost,
      left: leftCost === null ? "—" : formatCurrencyAmount(left.total_cost_amount, left.cost_currency),
      right:
        rightCost === null ? "—" : formatCurrencyAmount(right.total_cost_amount, right.cost_currency),
      delta: costDelta,
    },
    numeric("modelSteps", labels.modelSteps, left.model_step_count, right.model_step_count),
    numeric("toolCalls", labels.toolCalls, left.tool_call_count, right.tool_call_count),
    numeric(
      "approvals",
      labels.approvals,
      left.approval_summary.total,
      right.approval_summary.total,
    ),
  ];
}
