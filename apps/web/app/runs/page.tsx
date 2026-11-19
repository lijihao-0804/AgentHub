"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useCallback, useDeferredValue, useEffect, useRef, useState } from "react";

import StatusBadge from "@/components/ui/status-badge";
import { EmptyState, ErrorState, LoadingState, SessionRequired } from "@/components/ui/states";
import {
  CatalogEmptyNotice,
  CatalogPageHeader,
  CatalogStatStrip,
  CatalogToolbar,
} from "@/components/catalog/catalog-layout";
import { ApiError, errorHintKey, toApiError } from "@/lib/api/client";
import { Agent, AgentVersion, listAgents, listAgentVersions } from "@/lib/api/agents";
import { listRuns, RunListItem } from "@/lib/api/runs";
import { useFrontendSession } from "@/components/providers/session-provider";
import Select, { type SelectOption } from "@/components/ui/select";
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

/** Statuses whose rows go stale fast enough to justify auto-refresh. */
const ACTIVE_RUN_STATUSES = new Set(["RUNNING", "WAITING_APPROVAL", "CANCEL_REQUESTED"]);
const RUNS_POLL_MS = 5000;

function shortId(id: string): string {
  return `${id.slice(0, 8)}…`;
}

export default function RunsPage() {
  return <Suspense fallback={<LoadingState />}><RunHistory /></Suspense>;
}

function RunHistory() {
  const { t, locale, statusLabel, failureCategoryLabel, formatDateTime, formatRelativeTime, formatCount, formatDurationMs, formatCurrencyAmount } =
    useI18n();
  const { workspaceId, accessToken, connected } = useFrontendSession();
  const query = useSearchParams();
  const status = query.get("status")?.trim() ?? "";
  const agentVersionId = query.get("agent_version_id")?.trim() ?? "";
  const [agents, setAgents] = useState<Agent[]>([]);
  const [agentVersions, setAgentVersions] = useState<AgentVersion[]>([]);
  const [selectedAgentId, setSelectedAgentId] = useState("");
  const [runs, setRuns] = useState<RunListItem[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(false);
  const loadedFilterKey = useRef<string | null>(null);
  const [search, setSearch] = useState("");
  const deferredSearch = useDeferredValue(search);
  /** Generation of the latest list request; stale responses never land. */
  const generationRef = useRef(0);
  /** While the user has paged deeper, a poll must not collapse to page one. */
  const pagedRef = useRef(false);

  const refresh = useCallback(
    async (cursor: string | null = null) => {
      if (!connected) return;
      const generation = (generationRef.current += 1);
      pagedRef.current = cursor !== null;
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
        if (generationRef.current !== generation) return;
        setRuns((current) => (cursor ? [...current, ...result.items] : result.items));
        setNextCursor(result.next_cursor);
        setError(null);
      } catch (caught) {
        if (generationRef.current !== generation) return;
        setError(toApiError(caught, ""));
      } finally {
        if (generationRef.current === generation) setLoading(false);
      }
    },
    [connected, workspaceId, accessToken, status, agentVersionId],
  );

  // A renewed access token must preserve older pages the user already loaded.
  const filterKey = connected ? JSON.stringify([workspaceId, status, agentVersionId]) : null;
  useEffect(() => {
    if (loadedFilterKey.current === filterKey) return;
    loadedFilterKey.current = filterKey;
    if (filterKey !== null) {
      setRuns([]);
      setNextCursor(null);
      setError(null);
      void refresh();
    }
  }, [filterKey, refresh]);

  // The version filter is a pick list, not a hand-typed UUID: load the agent
  // roster once per workspace. Failure degrades silently — the raw id stays
  // in the deep link and the selects simply stay empty.
  useEffect(() => {
    if (!connected || !accessToken) {
      setAgents([]);
      return;
    }
    let cancelled = false;
    listAgents({ workspaceId, accessToken })
      .then((items) => {
        if (!cancelled) setAgents(items);
      })
      .catch(() => {
        if (!cancelled) setAgents([]);
      });
    return () => {
      cancelled = true;
    };
  }, [connected, workspaceId, accessToken]);

  useEffect(() => {
    if (!selectedAgentId || !connected || !accessToken) {
      setAgentVersions([]);
      return;
    }
    let cancelled = false;
    listAgentVersions({ workspaceId, accessToken }, selectedAgentId)
      .then((items) => {
        if (!cancelled) setAgentVersions(items);
      })
      .catch(() => {
        if (!cancelled) setAgentVersions([]);
      });
    return () => {
      cancelled = true;
    };
  }, [connected, workspaceId, accessToken, selectedAgentId]);

  // Runs in flight go stale fast: poll page one while any visible row is
  // active and the tab is visible. Paging deeper pauses the poll so a refresh
  // cannot collapse the list back to its first page.
  const hasActiveRows = runs.some((run) => ACTIVE_RUN_STATUSES.has(run.status));
  const copy = locale === "zh-CN"
    ? {
        searchLabel: "搜索当前列表中的运行",
        searchPlaceholder: "按运行 ID、状态、版本或失败代码搜索",
        clearSearch: "清空搜索",
        result: (visible: number, total: number) => `显示 ${visible} / ${total}`,
        noMatch: "没有匹配的运行",
        noMatchHint: "可清空搜索或调整上方筛选。",
        noMatchMore: "可以加载更多记录，在更早的运行中继续搜索。",
        loadedOnPage: "当前列表",
        active: "活动运行",
        needsAttention: "需要处理",
        loading: "加载中",
        unavailable: "不可用",
      }
    : {
        searchLabel: "Search runs in this view",
        searchPlaceholder: "Search run ID, status, version, or failure code",
        clearSearch: "Clear search",
        result: (visible: number, total: number) => `${visible} of ${total} shown`,
        noMatch: "No matching runs",
        noMatchHint: "Clear the search or change the filters above.",
        noMatchMore: "Load more to search older runs.",
        loadedOnPage: "In this view",
        active: "Active",
        needsAttention: "Needs attention",
        loading: "Loading",
        unavailable: "Unavailable",
      };

  useEffect(() => {
    setSearch("");
  }, [workspaceId]);
  useEffect(() => {
    if (!connected || !hasActiveRows || nextCursor !== null) return;
    const interval = window.setInterval(() => {
      if (!document.hidden && !loading && !pagedRef.current) {
        void refresh(null);
      }
    }, RUNS_POLL_MS);
    return () => window.clearInterval(interval);
  }, [connected, hasActiveRows, nextCursor, loading, refresh]);

  function applyFilters(nextStatus: string, nextVersion: string) {
    const params = new URLSearchParams();
    if (nextStatus) params.set("status", nextStatus);
    if (nextVersion) params.set("agent_version_id", nextVersion);
    const query = params.toString();
    window.history.replaceState(null, "", query ? `/runs?${query}` : "/runs");
  }

  function clearFilters() {
    setSelectedAgentId("");
    applyFilters("", "");
  }

  if (!connected) {
    return (
      <div className="page">
        <header className="page-header">
          <h1>{t("runs.title")}</h1>
        </header>
        <SessionRequired contextKey="session.context.runs" />
      </div>
    );
  }

  const hint = error ? errorHintKey(error) : null;
  const hasFilters = Boolean(status || agentVersionId);
  const normalizedSearch = deferredSearch.trim().toLocaleLowerCase(locale);
  const filteredRuns = normalizedSearch
    ? runs.filter((run) =>
        [
          run.id,
          run.trace_id,
          run.status,
          statusLabel(run.status),
          `v${run.agent_version_number}`,
          run.failure_code ?? "",
          run.failure_category ? failureCategoryLabel(run.failure_category) : "",
        ].some((value) => value.toLocaleLowerCase(locale).includes(normalizedSearch)),
      )
    : runs;
  const activeCount = runs.filter((run) => ACTIVE_RUN_STATUSES.has(run.status)).length;
  const attentionCount = runs.filter((run) => run.status === "NEEDS_ATTENTION").length;

  return (
    <div className="page">
      <CatalogPageHeader
        title={t("runs.title")}
        actions={
          <>
            <Link className="button button-ghost" href="/runs/compare">{t("nav.runCompare")}</Link>
            <button type="button" className="button button-primary" onClick={() => void refresh()} disabled={loading}>
              {t("common.refresh")}
            </button>
          </>
        }
      />

      <CatalogStatStrip
        items={[
          { label: copy.loadedOnPage, value: loading && runs.length === 0 ? copy.loading : formatCount(runs.length) },
          { label: copy.active, value: loading && runs.length === 0 ? copy.loading : formatCount(activeCount), tone: activeCount > 0 ? "success" : undefined },
          { label: copy.needsAttention, value: loading && runs.length === 0 ? copy.loading : formatCount(attentionCount), href: "/runs?status=NEEDS_ATTENTION", tone: attentionCount > 0 ? "attention" : undefined },
        ]}
      />

      <section className="panel runs-filter-panel" aria-label={t("runs.filters.status")}>
        <div className="runs-toolbar">
          <label>
            {t("runs.filters.status")}
            <Select
              value={status}
              onChange={(next) => applyFilters(next, agentVersionId)}
              options={[
                { value: "", label: t("runs.filters.allStatuses") },
                ...RUN_STATUS_OPTIONS.map(
                  (option): SelectOption => ({ value: option, label: statusLabel(option) }),
                ),
              ]}
            />
          </label>
          <label>
            {t("runs.filters.agent")}
            <Select
              value={selectedAgentId}
              onChange={(next) => {
                setSelectedAgentId(next);
                // Switching agents invalidates a version filter picked under
                // the previous one; without a filter there is nothing to re-run.
                if (agentVersionId) applyFilters(status, "");
              }}
              options={[
                { value: "", label: t("runs.filters.allAgents") },
                ...agents.map(
                  (agent): SelectOption => ({ value: agent.id, label: agent.name }),
                ),
              ]}
            />
          </label>
          <label>
            {t("runs.filters.version")}
            <Select
              value={agentVersionId}
              onChange={(next) => applyFilters(status, next)}
              disabled={!selectedAgentId && agentVersions.length === 0 && !agentVersionId}
              options={[
                { value: "", label: t("runs.filters.allVersions") },
                ...(agentVersionId && !agentVersions.some((v) => v.id === agentVersionId)
                  ? [{ value: agentVersionId, label: t("runs.filters.linkedVersion") }]
                  : []),
                ...agentVersions.map(
                  (version): SelectOption => ({
                    value: version.id,
                    label: `v${version.version_number} · ${version.resolved_spec_hash.slice(0, 8)}`,
                  }),
                ),
              ]}
            />
          </label>
          <div className="runs-toolbar-actions">
            {hasFilters && (
              <button type="button" className="button button-ghost" onClick={clearFilters} disabled={loading}>
                {t("runs.filters.clear")}
              </button>
            )}
          </div>
        </div>
      </section>

      {runs.length > 0 && (
        <CatalogToolbar
          searchLabel={copy.searchLabel}
          searchPlaceholder={copy.searchPlaceholder}
          searchValue={search}
          onSearchChange={setSearch}
          resultLabel={copy.result(filteredRuns.length, runs.length)}
          clearLabel={copy.clearSearch}
        />
      )}

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
          actions={hasFilters ? (
            <button type="button" className="button button-ghost" onClick={clearFilters}>
              {t("runs.filters.clear")}
            </button>
          ) : undefined}
        />
      )}

      {runs.length > 0 && (
        <section className="panel" aria-label={t("runs.title")}>
          {filteredRuns.length === 0 ? (
            <CatalogEmptyNotice title={copy.noMatch} hint={nextCursor ? copy.noMatchMore : copy.noMatchHint} />
          ) : <div className="data-table">
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
                {filteredRuns.map((run) => (
                  <tr key={run.id}>
                    <td className="tight-cell" data-label={t("runs.columns.run")}>
                      <Link href={`/runs/${encodeURIComponent(run.id)}`} title={run.id}>
                        <code>{shortId(run.id)}</code>
                      </Link>
                    </td>
                    <td data-label={t("runs.columns.status")}><StatusBadge status={run.status} /></td>
                    <td className="tight-cell" data-label={t("runs.columns.version")}>v{run.agent_version_number}</td>
                    <td className="tight-cell" data-label={t("runs.columns.started")} title={formatDateTime(run.started_at)}>{formatRelativeTime(run.started_at)}</td>
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
                        <span
                          className="failure-cell"
                          title={`${failureCategoryLabel(run.failure_category ?? "")}: ${run.failure_code}`}
                        >
                          {failureCategoryLabel(run.failure_category ?? "")}
                        </span>
                      ) : (
                        "—"
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>}
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
