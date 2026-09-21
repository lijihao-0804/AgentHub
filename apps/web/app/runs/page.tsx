"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import StatusBadge from "@/components/ui/status-badge";
import { EmptyState, ErrorState, LoadingState, SessionRequired } from "@/components/ui/states";
import { ApiError, errorHintKey, toApiError } from "@/lib/api/client";
import { listRuns, RunListItem } from "@/lib/api/runs";
import { useFrontendSession } from "@/components/providers/session-provider";
import { useI18n } from "@/i18n/provider";

const RUN_STATUS_OPTIONS = [
  "RUNNING",
  "WAITING_APPROVAL",
  "SUCCEEDED",
  "FAILED",
  "NEEDS_ATTENTION",
  "CANCEL_REQUESTED",
  "CANCELLED",
];

function shortId(id: string): string {
  return `${id.slice(0, 8)}…`;
}

export default function RunsPage() {
  const { t, statusLabel, failureCategoryLabel, formatDateTime, formatCount, formatDurationMs, formatCurrencyAmount } =
    useI18n();
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
        setError(toApiError(caught, ""));
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
          <p className="eyebrow">{t("runs.eyebrow")}</p>
          <h1>{t("runs.title")}</h1>
          <p className="page-lede">{t("runs.lede")}</p>
        </header>
        <SessionRequired contextKey="session.context.runs" />
      </div>
    );
  }

  const hint = error ? errorHintKey(error) : null;
  const hasFilters = Boolean(status || agentVersionId);

  return (
    <div className="page">
      <header className="page-header">
        <p className="eyebrow">{t("runs.eyebrow")}</p>
        <h1>{t("runs.title")}</h1>
        <p className="page-lede">{t("runs.lede")}</p>
      </header>

      <section className="panel" aria-label={t("runs.filters.status")}>
        <div className="runs-toolbar">
          <label>
            {t("runs.filters.status")}
            <select value={status} onChange={(event) => setStatus(event.target.value)}>
              <option value="">{t("runs.filters.allStatuses")}</option>
              {RUN_STATUS_OPTIONS.map((option) => (
                <option value={option} key={option}>
                  {statusLabel(option)}
                </option>
              ))}
            </select>
          </label>
          <label>
            {t("runs.filters.agentVersionId")}
            <input
              value={agentVersionId}
              onChange={(event) => setAgentVersionId(event.target.value)}
              placeholder={t("runs.filters.agentVersionPlaceholder")}
              spellCheck={false}
            />
          </label>
          <div className="runs-toolbar-actions">
            <button type="button" className="button button-primary" onClick={applyFilters} disabled={loading}>
              {t("runs.filters.apply")}
            </button>
            <button type="button" className="button button-ghost" onClick={clearFilters} disabled={loading}>
              {t("runs.filters.clear")}
            </button>
          </div>
        </div>
      </section>

      {error && (
        <ErrorState
          code={error.code}
          message={error.message || t("errors.loadRuns")}
          hint={hint ? t(hint) : undefined}
          onRetry={() => void refresh()}
        />
      )}
      {loading && runs.length === 0 && !error && <LoadingState />}
      {!loading && runs.length === 0 && !error && (
        <EmptyState
          title={t("runs.empty")}
          hint={hasFilters ? t("runs.emptyFilteredHint") : t("runs.emptyHint")}
        />
      )}

      {runs.length > 0 && (
        <section className="panel" aria-label={t("runs.title")}>
          <div className="data-table">
            <table>
              <thead>
                <tr>
                  <th scope="col">{t("runs.columns.run")}</th>
                  <th scope="col">{t("runs.columns.status")}</th>
                  <th scope="col">{t("runs.columns.version")}</th>
                  <th scope="col">{t("runs.columns.started")}</th>
                  <th className="numeric-cell" scope="col">{t("runs.columns.duration")}</th>
                  <th className="numeric-cell" scope="col">{t("runs.columns.tokens")}</th>
                  <th className="numeric-cell" scope="col">{t("runs.columns.cost")}</th>
                  <th className="numeric-cell" scope="col">{t("runs.columns.tools")}</th>
                  <th scope="col">{t("runs.columns.failure")}</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((run) => (
                  <tr key={run.id}>
                    <td className="tight-cell" data-label={t("runs.columns.run")}>
                      <Link href={`/runs/${encodeURIComponent(run.id)}`} title={run.id}>
                        <code>{shortId(run.id)}</code>
                      </Link>
                    </td>
                    <td data-label={t("runs.columns.status")}><StatusBadge status={run.status} /></td>
                    <td className="tight-cell" data-label={t("runs.columns.version")}>v{run.agent_version_number}</td>
                    <td className="tight-cell" data-label={t("runs.columns.started")}>{formatDateTime(run.started_at)}</td>
                    <td className="numeric-cell" data-label={t("runs.columns.duration")} title={
                      run.duration_ms === null ? undefined : `${run.duration_ms} ms`
                    }>
                      {run.duration_ms === null ? "—" : formatDurationMs(run.duration_ms)}
                    </td>
                    <td className="numeric-cell" data-label={t("runs.columns.tokens")}>
                      {run.total_tokens === null ? "—" : formatCount(run.total_tokens)}
                    </td>
                    <td className="numeric-cell" data-label={t("runs.columns.cost")} title={
                      run.total_cost_amount === null
                        ? undefined
                        : `${run.total_cost_amount} ${run.cost_currency ?? ""}`.trim()
                    }>
                      {run.total_cost_amount === null ? "—" : formatCurrencyAmount(run.total_cost_amount, run.cost_currency)}
                    </td>
                    <td className="numeric-cell" data-label={t("runs.columns.tools")}>{run.tool_call_count}</td>
                    <td data-label={t("runs.columns.failure")}>
                      {run.failure_code ? (
                        <span>
                          {failureCategoryLabel(run.failure_category ?? "")}: <code>{run.failure_code}</code>
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
                {loading ? t("common.loading") : t("runs.loadMore")}
              </button>
            </div>
          )}
        </section>
      )}
    </div>
  );
}
