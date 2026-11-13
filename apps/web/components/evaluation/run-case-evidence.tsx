"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { useFrontendSession } from "@/components/providers/session-provider";
import {
  EmptyState,
  ErrorState,
  InlineError,
  LoadingState,
  Panel,
  SessionRequired,
} from "@/components/ui/states";
import StatusBadge from "@/components/ui/status-badge";
import { useI18n } from "@/i18n/provider";
import type { Locale } from "@/i18n/types";
import { ApiError, toApiError } from "@/lib/api/client";
import {
  getExperimentRunCaseEvidence,
  listExperimentRunCaseResults,
} from "@/lib/api/evaluation";
import CaseEvidenceDetails from "@/components/evaluation/case-evidence-details";
import {
  COST_RESERVATION_UNAVAILABLE,
  caseDisplayIdentity,
  describeCost,
  describeTaskSuccess,
  normalizeCaseEvidence,
  normalizeCaseEvidenceList,
  type CaseEvidenceListItem,
  type ExperimentRunCaseEvidence,
  type TaskSuccessKind,
} from "@/lib/case-evidence-view";

const PAGE_SIZE = 50;
const CASE_STATUSES = ["PENDING", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"] as const;

type AuthInput = { workspaceId: string; accessToken: string };

const RUN_CASE_COPY = {
  "en-US": {
    eyebrow: "Evaluation evidence",
    title: "Case results and evidence",
    refresh: "Refresh",
    filterLabel: "Execution status",
    filterAll: "All execution statuses",
    case: "Case",
    category: "Category",
    variant: "Variant",
    repetition: "Repetition",
    executionStatus: "Case execution",
    observedAgent: "AgentRun status",
    taskSuccess: "Task success",
    failure: "Failure / attention code",
    cost: "Pricing estimate",
    openDetails: "Open evidence",
    selected: "Selected",
    total: "Case results: {count}",
    empty: "No case results were returned",
    emptyHint:
      "This can be an old, empty, or still-running experiment. An empty response does not prove there were no violations.",
    loadMore: "Load more",
    loadingMore: "Loading more…",
    loading: "Loading case results…",
    detailLoading: "Loading safe case evidence…",
    loadError: "The case results could not be loaded.",
    detailError: "The safe evidence for this case could not be loaded.",
    mismatch: "The evidence response did not identify the selected case.",
    notRecorded: "Not recorded",
    notLoaded: "Open details",
    repetitionNumber: "Repetition {number}",
    taskLabels: {
      success: "Success (value 1)",
      failure: "Failure (value 0)",
      unknown: "Unknown",
      not_available: "Not available",
      not_applicable: "Not applicable",
      other: "Other status",
    },
    pending: "Not started",
    running: "Running",
    succeeded: "Completed",
    failed: "Failed",
    cancelled: "Cancelled",
    costUnknown: "UNKNOWN",
    costUnknownHint: "Not zero; not free",
    costRecordedHint: "Recorded pricing estimate (not a bill)",
    costSourceMissing: "Pricing source not recorded",
    currencyMissing: "currency not recorded",
    costReservation: "The amount hard limit is not supported; no billable call was dispatched.",
    caseFailureCode: "Case failure code",
    agentFailureCode: "AgentRun failure code",
  },
  "zh-CN": {
    eyebrow: "评测证据",
    title: "逐题结果与证据",
    refresh: "刷新",
    filterLabel: "执行状态",
    filterAll: "全部执行状态",
    case: "题目",
    category: "类别",
    variant: "变体",
    repetition: "重复",
    executionStatus: "Case 执行状态",
    observedAgent: "AgentRun 状态",
    taskSuccess: "任务成功",
    failure: "失败/待关注码",
    cost: "计价估算",
    openDetails: "查看证据",
    selected: "已选择",
    total: "Case 结果：{count}",
    empty: "未返回 Case 结果",
    emptyHint: "这可能来自旧实验、空实验或尚未完成的运行；空响应不证明没有违规。",
    loadMore: "加载更多",
    loadingMore: "正在加载更多…",
    loading: "正在加载逐题结果…",
    detailLoading: "正在加载安全题目证据…",
    loadError: "无法加载逐题结果。",
    detailError: "无法加载该题的安全证据。",
    mismatch: "证据响应与所选 case 身份不一致。",
    notRecorded: "未记录",
    notLoaded: "打开详情",
    repetitionNumber: "第 {number} 次重复",
    taskLabels: {
      success: "成功（值 1）",
      failure: "失败（值 0）",
      unknown: "未知",
      not_available: "不可用",
      not_applicable: "不适用",
      other: "其他状态",
    },
    pending: "尚未开始",
    running: "运行中",
    succeeded: "已完成",
    failed: "失败",
    cancelled: "已取消",
    costUnknown: "UNKNOWN",
    costUnknownHint: "不等于零，也不表示免费",
    costRecordedHint: "记录的计价估算（非账单）",
    costSourceMissing: "费用来源未记录",
    currencyMissing: "未记录币种",
    costReservation: "金额硬上限暂不支持，收费调用尚未派发。",
    caseFailureCode: "Case 失败码",
    agentFailureCode: "AgentRun 失败码",
  },
} as const;

type ListState =
  | { kind: "loading" }
  | { kind: "ready" }
  | { kind: "error"; error: ApiError };

type DetailState =
  | { kind: "idle" }
  | { kind: "loading"; caseId: string }
  | { kind: "ready"; caseId: string; evidence: ExperimentRunCaseEvidence }
  | { kind: "error"; caseId: string; error: ApiError };

function caseStatusLabel(
  status: string,
  text: (typeof RUN_CASE_COPY)[Locale],
  fallback: (status: string) => string,
): string {
  switch (status.toUpperCase()) {
    case "PENDING":
      return text.pending;
    case "RUNNING":
      return text.running;
    case "SUCCEEDED":
      return text.succeeded;
    case "FAILED":
      return text.failed;
    case "CANCELLED":
      return text.cancelled;
    default:
      return fallback(status);
  }
}

function taskSuccessTone(kind: TaskSuccessKind): "neutral" | "success" | "danger" | "attention" {
  if (kind === "success") return "success";
  if (kind === "failure") return "danger";
  if (kind === "unknown") return "attention";
  return "neutral";
}

function uniqueItems(items: CaseEvidenceListItem[]): CaseEvidenceListItem[] {
  const seen = new Set<string>();
  return items.filter((item) => {
    if (seen.has(item.id)) return false;
    seen.add(item.id);
    return true;
  });
}

async function loadEvidence(
  input: AuthInput,
  runId: string,
  caseId: string,
): Promise<ExperimentRunCaseEvidence> {
  const payload: unknown = await getExperimentRunCaseEvidence(input, runId, caseId);
  return normalizeCaseEvidence(payload);
}

function FailureDetails({
  item,
  text,
}: {
  item: CaseEvidenceListItem;
  text: (typeof RUN_CASE_COPY)[Locale];
}) {
  const codes: Array<{ source: string; code: string }> = [];
  if (item.failure_code) {
    codes.push({ source: text.caseFailureCode, code: item.failure_code });
  }
  if (item.observed_agent_failure_code) {
    codes.push({ source: text.agentFailureCode, code: item.observed_agent_failure_code });
  }
  if (codes.length === 0 && !item.safe_failure_message) {
    return <span>{text.notRecorded}</span>;
  }
  return (
    <div className="case-evidence-failures">
      {codes.map((entry) => (
        <div key={`${entry.source}:${entry.code}`}>
          <span className="state-hint">{entry.source}</span>
          <code>{entry.code}</code>
          {entry.code.toUpperCase() === COST_RESERVATION_UNAVAILABLE && (
            <span className="state-hint">{text.costReservation}</span>
          )}
        </div>
      ))}
      {item.safe_failure_message && (
        <span className="state-hint" title={item.safe_failure_message}>
          {item.safe_failure_message}
        </span>
      )}
    </div>
  );
}

export function RunCaseEvidence({
  runId,
  variants,
  refreshKey,
}: {
  runId: string;
  variants: Array<{ id: string; label: string }>;
  refreshKey?: string | number;
}) {
  const { workspaceId, accessToken, connected, sessionId } = useFrontendSession();
  if (!connected || !workspaceId || !accessToken) {
    return <SessionRequired contextKey="session.context.evaluations" />;
  }
  return (
    <RunCaseEvidenceSession
      key={`${sessionId}:${workspaceId}:${runId}:${String(refreshKey ?? "")}`}
      sessionId={sessionId}
      workspaceId={workspaceId}
      accessToken={accessToken}
      runId={runId}
      variants={variants}
    />
  );
}

function RunCaseEvidenceSession({
  sessionId,
  workspaceId,
  accessToken,
  runId,
  variants,
}: {
  sessionId: number;
  workspaceId: string;
  accessToken: string;
  runId: string;
  variants: Array<{ id: string; label: string }>;
}) {
  const { locale, statusLabel, formatCount } = useI18n();
  const text = RUN_CASE_COPY[locale];
  const [statusFilter, setStatusFilter] = useState("");
  const [items, setItems] = useState<CaseEvidenceListItem[]>([]);
  const [total, setTotal] = useState(0);
  const [listState, setListState] = useState<ListState>({ kind: "loading" });
  const [loadMoreError, setLoadMoreError] = useState<ApiError | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [selectedCaseId, setSelectedCaseId] = useState<string | null>(null);
  const [detailState, setDetailState] = useState<DetailState>({ kind: "idle" });
  const [evidenceCache, setEvidenceCache] = useState<Record<string, ExperimentRunCaseEvidence>>({});
  const activeRef = useRef({ sessionId, workspaceId, accessToken, runId });
  activeRef.current = { sessionId, workspaceId, accessToken, runId };
  const statusFilterRef = useRef("");
  const selectedIdRef = useRef<string | null>(null);
  const listGenerationRef = useRef(0);
  const detailGenerationRef = useRef(0);
  const loadMoreInFlightRef = useRef(false);

  const isCurrent = useCallback(
    (
      snapshot: { sessionId: number; workspaceId: string; accessToken: string; runId: string },
      generation: number,
      expectedStatus?: string,
    ) =>
      activeRef.current.sessionId === snapshot.sessionId &&
      activeRef.current.workspaceId === snapshot.workspaceId &&
      activeRef.current.accessToken === snapshot.accessToken &&
      activeRef.current.runId === snapshot.runId &&
      listGenerationRef.current === generation &&
      (expectedStatus === undefined || statusFilterRef.current === expectedStatus),
    [],
  );

  const loadFirstPage = useCallback(
    async () => {
      const status = statusFilterRef.current;
      const generation = (listGenerationRef.current += 1);
      detailGenerationRef.current += 1;
      const snapshot = activeRef.current;
      loadMoreInFlightRef.current = false;
      setListState({ kind: "loading" });
      setItems([]);
      setTotal(0);
      setLoadingMore(false);
      setLoadMoreError(null);
      setSelectedCaseId(null);
      selectedIdRef.current = null;
      setDetailState({ kind: "idle" });
      setEvidenceCache({});
      try {
        const payload: unknown = await listExperimentRunCaseResults(
          {
            workspaceId: snapshot.workspaceId,
            accessToken: snapshot.accessToken,
          },
          snapshot.runId,
          {
            status: status || undefined,
            limit: PAGE_SIZE,
            offset: 0,
          },
        );
        if (!isCurrent(snapshot, generation, status)) return;
        const result = normalizeCaseEvidenceList(payload);
        setItems(uniqueItems(result.items));
        setTotal(result.total);
        setListState({ kind: "ready" });
      } catch (caught) {
        if (!isCurrent(snapshot, generation, status)) return;
        setListState({
          kind: "error",
          error: toApiError(caught, ""),
        });
      }
    },
    [isCurrent],
  );

  useEffect(() => {
    void loadFirstPage();
    return () => {
      listGenerationRef.current += 1;
      detailGenerationRef.current += 1;
      loadMoreInFlightRef.current = false;
    };
  }, [accessToken, loadFirstPage, runId, sessionId, workspaceId]);

  const loadMore = useCallback(async () => {
    if (loadMoreInFlightRef.current) return;
    const status = statusFilterRef.current;
    const generation = listGenerationRef.current;
    const snapshot = activeRef.current;
    loadMoreInFlightRef.current = true;
    setLoadingMore(true);
    setLoadMoreError(null);
    try {
      const payload: unknown = await listExperimentRunCaseResults(
        {
          workspaceId: snapshot.workspaceId,
          accessToken: snapshot.accessToken,
        },
        snapshot.runId,
        {
          status: status || undefined,
          limit: PAGE_SIZE,
          offset: items.length,
        },
      );
      if (!isCurrent(snapshot, generation, status)) return;
      const result = normalizeCaseEvidenceList(payload);
      setItems((current) => uniqueItems([...current, ...result.items]));
      setTotal(result.total);
    } catch (caught) {
      if (!isCurrent(snapshot, generation, status)) return;
      setLoadMoreError(toApiError(caught, ""));
    } finally {
      if (isCurrent(snapshot, generation, status)) {
        loadMoreInFlightRef.current = false;
        setLoadingMore(false);
      }
    }
  }, [isCurrent, items.length]);

  function changeStatus(status: string) {
    statusFilterRef.current = status;
    setStatusFilter(status);
    void loadFirstPage();
  }

  async function openCase(item: CaseEvidenceListItem) {
    setSelectedCaseId(item.id);
    selectedIdRef.current = item.id;
    setLoadMoreError(null);
    const cached = evidenceCache[item.id];
    if (cached) {
      setDetailState({ kind: "ready", caseId: item.id, evidence: cached });
      return;
    }

    const generation = (detailGenerationRef.current += 1);
    const snapshot = activeRef.current;
    setDetailState({ kind: "loading", caseId: item.id });
    try {
      const evidence = await loadEvidence(
        { workspaceId: snapshot.workspaceId, accessToken: snapshot.accessToken },
        snapshot.runId,
        item.id,
      );
      if (
        !isCurrent(snapshot, listGenerationRef.current) ||
        detailGenerationRef.current !== generation ||
        selectedIdRef.current !== item.id
      ) {
        return;
      }
      if (evidence.case_id && evidence.case_id !== item.id) {
        throw new ApiError("CASE_EVIDENCE_ID_MISMATCH", text.mismatch, 0);
      }
      setEvidenceCache((current) => ({ ...current, [item.id]: evidence }));
      setDetailState({ kind: "ready", caseId: item.id, evidence });
    } catch (caught) {
      if (
        !isCurrent(snapshot, listGenerationRef.current) ||
        detailGenerationRef.current !== generation ||
        selectedIdRef.current !== item.id
      ) {
        return;
      }
      setDetailState({
        kind: "error",
        caseId: item.id,
        error: toApiError(caught, text.detailError),
      });
    }
  }

  const selectedItem = items.find((item) => item.id === selectedCaseId) ?? null;

  return (
    <div className="case-evidence">
      <Panel
        title={text.title}
        eyebrow={text.eyebrow}
        actions={
          <>
            <button
              type="button"
              className="button button-ghost"
              onClick={() => void loadFirstPage()}
            >
              {text.refresh}
            </button>
            <label className="state-hint case-evidence-filter">
              <span>{text.filterLabel}</span>
              <select
                aria-label={text.filterLabel}
                value={statusFilter}
                onChange={(event) => changeStatus(event.currentTarget.value)}
              >
                <option value="">{text.filterAll}</option>
                {CASE_STATUSES.map((status) => (
                  <option key={status} value={status}>
                    {caseStatusLabel(status, text, statusLabel)}
                  </option>
                ))}
              </select>
            </label>
          </>
        }
      >
        {listState.kind === "loading" ? (
          <LoadingState label={text.loading} />
        ) : listState.kind === "error" ? (
          <ErrorState
            code={listState.error.code}
            message={listState.error.message || text.loadError}
            onRetry={
              listState.error.status === 403 || listState.error.status === 404
                ? undefined
                : () => void loadFirstPage()
            }
          />
        ) : items.length === 0 ? (
          <EmptyState title={text.empty} hint={text.emptyHint} />
        ) : (
          <>
            <p className="state-hint">{text.total.replace("{count}", formatCount(total))}</p>
            <div className="data-table case-evidence-list">
              <table>
                <thead>
                  <tr>
                    <th scope="col">{text.case}</th>
                    <th scope="col">{text.category}</th>
                    <th scope="col">{text.variant}</th>
                    <th scope="col">{text.repetition}</th>
                    <th scope="col">{text.executionStatus}</th>
                    <th scope="col">{text.observedAgent}</th>
                    <th scope="col">{text.taskSuccess}</th>
                    <th scope="col">{text.failure}</th>
                    <th scope="col">{text.cost}</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((item) => {
                    const evidence = evidenceCache[item.id];
                    const identity = caseDisplayIdentity(item, evidence);
                    const task = describeTaskSuccess(identity.taskSuccess);
                    const cost = describeCost(item.cost_amount, item.cost_currency);
                    const variantLabel =
                      identity.variantLabel ??
                      variants.find((variant) => variant.id === item.experiment_variant_id)?.label ??
                      item.experiment_variant_id;
                    const selected = selectedCaseId === item.id;
                    return (
                      <tr className={selected ? "case-evidence-row-selected" : undefined} key={item.id}>
                        <td className="tight-cell" data-label={text.case}>
                          <button
                            type="button"
                            className="case-evidence-row-select"
                            aria-pressed={selected}
                            title={identity.caseKey ?? item.id}
                            onClick={() => void openCase(item)}
                          >
                            <code>{identity.caseKey ?? item.id}</code>
                            <span>{selected ? text.selected : text.openDetails}</span>
                          </button>
                        </td>
                        <td data-label={text.category}>{identity.category ?? text.notRecorded}</td>
                        <td className="tight-cell" data-label={text.variant}>
                          {variantLabel || text.notRecorded}
                        </td>
                        <td className="numeric-cell" data-label={text.repetition}>
                          {text.repetitionNumber.replace("{number}", String(item.repetition_index + 1))}
                        </td>
                        <td data-label={text.executionStatus}>
                          <StatusBadge
                            status={item.status || "NOT_AVAILABLE"}
                            label={
                              item.status
                                ? caseStatusLabel(item.status, text, statusLabel)
                                : text.notRecorded
                            }
                          />
                        </td>
                        <td data-label={text.observedAgent}>
                          {item.observed_agent_status ? (
                            <StatusBadge status={item.observed_agent_status} />
                          ) : (
                            text.notRecorded
                          )}
                        </td>
                        <td data-label={text.taskSuccess}>
                          <StatusBadge
                            status={task.status}
                            label={
                              task.status === "NOT_LOADED"
                                ? text.notLoaded
                                : text.taskLabels[task.kind]
                            }
                            tone={taskSuccessTone(task.kind)}
                          />
                          {task.value !== null && (
                            <span className="state-hint">value: {String(task.value)}</span>
                          )}
                        </td>
                        <td data-label={text.failure}>
                          <FailureDetails item={item} text={text} />
                        </td>
                        <td className="numeric-cell" data-label={text.cost}>
                          {cost.kind === "unknown" ? (
                            <span className="case-evidence-cost">
                              <strong>{text.costUnknown}</strong>
                              <span className="state-hint">{text.costUnknownHint}</span>
                            </span>
                          ) : (
                            <span className="case-evidence-cost">
                              <strong>{cost.text} {cost.currency ?? ""}</strong>
                              <span className="state-hint">{text.costRecordedHint}</span>
                              {!cost.currency && <span className="state-hint">{text.currencyMissing}</span>}
                              <span className="state-hint">{text.costSourceMissing}</span>
                            </span>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            {items.length < total && (
              <div className="form-actions">
                <button
                  type="button"
                  className="button button-ghost"
                  disabled={loadingMore}
                  onClick={() => void loadMore()}
                >
                  {loadingMore ? text.loadingMore : text.loadMore}
                </button>
              </div>
            )}
            <InlineError error={loadMoreError} fallback={text.loadError} />
          </>
        )}
      </Panel>

      {detailState.kind === "loading" && (
        <Panel
          title={
            items.find((item) => item.id === detailState.caseId)?.case_execution_key ??
            detailState.caseId
          }
          eyebrow={text.eyebrow}
        >
          <LoadingState label={text.detailLoading} />
        </Panel>
      )}
      {detailState.kind === "error" && (
        <Panel
          title={
            selectedItem?.case_execution_key ??
            detailState.caseId
          }
          eyebrow={text.eyebrow}
        >
          <ErrorState
            code={detailState.error.code}
            message={detailState.error.message || text.detailError}
            onRetry={
              detailState.error.status === 403 || detailState.error.status === 404 || !selectedItem
                ? undefined
                : () => void openCase(selectedItem)
            }
          />
        </Panel>
      )}
      {detailState.kind === "ready" && (
        <CaseEvidenceDetails
          evidence={detailState.evidence}
          caseResult={items.find((item) => item.id === detailState.caseId) ?? null}
        />
      )}
    </div>
  );
}

export default RunCaseEvidence;
