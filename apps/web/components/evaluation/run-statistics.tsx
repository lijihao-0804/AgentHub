"use client";

import { useEffect, useState } from "react";

import StatusBadge from "@/components/ui/status-badge";
import MetricCard from "@/components/ui/metric-card";
import {
  EmptyState,
  ErrorState,
  InlineError,
  LoadingState,
  Panel,
  SessionRequired,
} from "@/components/ui/states";
import { useFrontendSession } from "@/components/providers/session-provider";
import { useI18n } from "@/i18n/provider";
import type { Locale } from "@/i18n/types";
import { ApiError, toApiError } from "@/lib/api/client";
import {
  EvaluationExperimentRun,
  EvaluationExperimentRunProgress,
  EvaluationExperimentRunStatistics,
  EvaluationFailureTaxonomy,
  getExperimentRun,
  getExperimentRunFailureTaxonomy,
  getExperimentRunProgress,
  getExperimentRunStatistics,
} from "@/lib/api/evaluation";
import {
  evidenceLevelGuidance,
  kOneLimitation,
  literalConsistencyLimitation,
  orderedFailureCounts,
  orderedStatisticsVariants,
  orderedTaxonomyVariants,
  passHatKUnavailableReason,
  runExecutionSucceededWithoutTaskSuccess,
  taskOutcomeCounts,
  taskOutcomeUnavailableReason,
  usableInterval,
} from "@/lib/evaluation-results";

type Copy = {
  title: string;
  eyebrow: string;
  refresh: string;
  refreshing: string;
  noData: string;
  noDataHint: string;
  execution: string;
  experimentRun: string;
  executionSplit: string;
  caseProgress: string;
  progress: string;
  total: string;
  pending: string;
  running: string;
  completed: string;
  failed: string;
  cancelled: string;
  progressUnavailable: string;
  backendEvidence: string;
  runEvidence: string;
  statisticsEvidence: string;
  taxonomyEvidence: string;
  evidenceMismatch: string;
  notRecorded: string;
  variants: string;
  samples: string;
  cases: string;
  trials: string;
  repetitions: string;
  completeCases: string;
  incompleteCases: string;
  taskOutcomes: string;
  knownTrials: string;
  taskSuccesses: string;
  taskFailures: string;
  unknownTrials: string;
  countsUnavailable: string;
  successRate: string;
  passHatK: string;
  completeCaseRate: string;
  successCoverage: string;
  successRateHint: string;
  passHatKHint: string;
  completeRateHint: string;
  coverageHint: string;
  intervals: string;
  knownTrialInterval: string;
  caseBootstrapInterval: string;
  passHatKInterval: string;
  noIntervalRecorded: string;
  literalConsistency: string;
  literalConsistencyValue: string;
  incompleteNotice: string;
  failureTaxonomy: string;
  taxonomyVersion: string;
  labelledCases: string;
  noRuleLabels: string;
  taxonomyNotCompliance: string;
  nullRate: string;
  notAvailable: string;
  executionAndTaskNotice: string;
};

function copyFor(locale: Locale): Copy {
  if (locale === "zh-CN") {
    return {
      title: "运行摘要与统计",
      eyebrow: "结果摘要",
      refresh: "刷新摘要",
      refreshing: "正在刷新…",
      noData: "暂无可展示的摘要",
      noDataHint: "运行尚未产生可由当前授权接口读取的结果。",
      execution: "执行状态",
      experimentRun: "评测运行",
      executionSplit: "Case 执行与任务评分分开显示；执行成功不等于 task_success=1。",
      caseProgress: "Case 执行进度",
      progress: "进度",
      total: "总数",
      pending: "等待",
      running: "执行中",
      completed: "已完成",
      failed: "失败",
      cancelled: "已取消",
      progressUnavailable: "该接口未返回执行进度。",
      backendEvidence: "后端证据等级",
      runEvidence: "Run 响应",
      statisticsEvidence: "统计响应",
      taxonomyEvidence: "失败分类响应",
      evidenceMismatch: "不同后端响应记录了不同证据等级；页面原样并列显示，不自行选取更高等级。",
      notRecorded: "旧版本未记录",
      variants: "按 Variant 统计",
      samples: "样本量",
      cases: "case",
      trials: "trial",
      repetitions: "重复数 K",
      completeCases: "完整 case",
      incompleteCases: "不完整 case",
      taskOutcomes: "任务结果",
      knownTrials: "已获得 task_success 结果的 trial",
      taskSuccesses: "任务成功 trial",
      taskFailures: "任务未成功 trial",
      unknownTrials: "未知结果 trial",
      countsUnavailable: "后端计数不一致，因此不展示推测的分子或分母。",
      successRate: "success_rate",
      passHatK: "pass_hat_k",
      completeCaseRate: "complete_case_rate",
      successCoverage: "success_coverage_rate",
      successRateHint: "已获得 task_success 结果的 trial 中，成功 trial 的比例",
      passHatKHint: "仅在完整 case 中计算",
      completeRateHint: "完整 case / 全部 case",
      coverageHint: "已知 trial / 全部 trial",
      intervals: "分母与区间",
      knownTrialInterval: "success_rate · 已知结果 trial 的 Jeffreys 区间",
      caseBootstrapInterval: "success_rate · 按 case 重采样的 bootstrap 区间",
      passHatKInterval: "pass_hat_k · complete-case 区间",
      noIntervalRecorded: "不可用或旧版本未返回该区间",
      literalConsistency: "字面一致性",
      literalConsistencyValue: "literal_consistency_at_k",
      incompleteNotice: "存在缺少 K 次已知 trial 的 case；这些 case 未进入 pass_hat_k 分母。",
      failureTaxonomy: "规则式失败定位",
      taxonomyVersion: "Taxonomy",
      labelledCases: "带标签 case",
      noRuleLabels: "没有规则标签。",
      taxonomyNotCompliance: "缺失规则标签只表示当前规则未定位到问题，不能据此推断“没有违规”。",
      nullRate: "该速率返回 null 或缺失；页面未将其按 0 处理，也不展示推导分母。",
      notAvailable: "不可用",
      executionAndTaskNotice: "运行执行成功，但已知任务结果中没有成功样本；这不能显示为任务成功。",
    };
  }
  return {
    title: "Run summary and statistics",
    eyebrow: "Results summary",
    refresh: "Refresh summary",
    refreshing: "Refreshing…",
    noData: "No summary is available yet",
    noDataHint: "The run has not produced results readable through the currently authorized endpoints.",
    execution: "Execution",
    experimentRun: "Evaluation run",
    executionSplit: "Case execution and task success are separate; successful execution does not imply task_success=1.",
    caseProgress: "Case execution progress",
    progress: "Progress",
    total: "Total",
    pending: "Pending",
    running: "Running",
    completed: "Completed",
    failed: "Failed",
    cancelled: "Cancelled",
    progressUnavailable: "This endpoint did not return execution progress.",
    backendEvidence: "Backend evidence level",
    runEvidence: "Run response",
    statisticsEvidence: "Statistics response",
    taxonomyEvidence: "Failure-taxonomy response",
    evidenceMismatch: "Backend responses recorded different evidence levels. Both are shown verbatim; the UI does not choose the stronger one.",
    notRecorded: "Not recorded by this version",
    variants: "Statistics by variant",
    samples: "Sample volume",
    cases: "cases",
    trials: "trials",
    repetitions: "Repetitions K",
    completeCases: "Complete cases",
    incompleteCases: "Incomplete cases",
    taskOutcomes: "Task outcomes",
    knownTrials: "Trials with a known task_success result",
    taskSuccesses: "Successful task trials",
    taskFailures: "Unsuccessful task trials",
    unknownTrials: "Unknown-outcome trials",
    countsUnavailable: "Backend counts are inconsistent, so no inferred numerator or denominator is shown.",
    successRate: "success_rate",
    passHatK: "pass_hat_k",
    completeCaseRate: "complete_case_rate",
    successCoverage: "success_coverage_rate",
    successRateHint: "Successful trials among trials with a known task_success result",
    passHatKHint: "Complete cases only",
    completeRateHint: "Complete cases / all cases",
    coverageHint: "Known trials / all trials",
    intervals: "Denominators and intervals",
    knownTrialInterval: "success_rate · Jeffreys interval over trials with known results",
    caseBootstrapInterval: "success_rate · bootstrap interval resampled by case",
    passHatKInterval: "pass_hat_k · complete-case interval",
    noIntervalRecorded: "Unavailable or not returned by this backend version",
    literalConsistency: "Literal consistency",
    literalConsistencyValue: "literal_consistency_at_k",
    incompleteNotice:
      "Some cases do not have K trials with known outcomes; they are excluded from the pass_hat_k denominator.",
    failureTaxonomy: "Rule-based failure localization",
    taxonomyVersion: "Taxonomy",
    labelledCases: "Labelled cases",
    noRuleLabels: "No rule labels.",
    taxonomyNotCompliance: "Missing rule labels only means the current rules did not localize a problem; it does not establish that no violation occurred.",
    nullRate:
      "The rate is null or absent; the UI neither treats it as zero nor shows an inferred denominator.",
    notAvailable: "Not available",
    executionAndTaskNotice: "The run executed successfully, but no known task outcome succeeded; this must not be read as task success.",
  };
}

type SummarySnapshot = {
  sessionId: number;
  workspaceId: string;
  runId: string;
  loading: boolean;
  run: EvaluationExperimentRun | null;
  progress: EvaluationExperimentRunProgress | null;
  statistics: EvaluationExperimentRunStatistics | null;
  taxonomy: EvaluationFailureTaxonomy | null;
  errors: {
    run: ApiError | null;
    progress: ApiError | null;
    statistics: ApiError | null;
    taxonomy: ApiError | null;
  };
};

function settledValue<T>(result: PromiseSettledResult<T>): T | null {
  return result.status === "fulfilled" ? result.value : null;
}

function settledError<T>(result: PromiseSettledResult<T>): ApiError | null {
  return result.status === "rejected" ? toApiError(result.reason, "") : null;
}

function rateValue(
  value: unknown,
  formatPercent: (rate: number) => string,
  unavailable: string,
): string {
  return typeof value !== "number" ||
    !Number.isFinite(value) ||
    value < 0 ||
    value > 1
    ? unavailable
    : formatPercent(value);
}

function intervalValue(
  interval: ReturnType<typeof usableInterval>,
  formatPercent: (rate: number) => string,
  unavailable: string,
): string {
  if (!interval) return unavailable;
  return `${formatPercent(interval.lower)}–${formatPercent(interval.upper)}`;
}

export function RunStatistics({
  runId,
  refreshKey,
}: {
  runId: string;
  refreshKey?: string | number;
}) {
  const { locale, formatCount, formatPercent } = useI18n();
  const { workspaceId, accessToken, connected, sessionId } = useFrontendSession();
  const text = copyFor(locale);
  const [refreshNonce, setRefreshNonce] = useState(0);
  const [snapshot, setSnapshot] = useState<SummarySnapshot | null>(null);

  useEffect(() => {
    if (!connected) {
      setSnapshot(null);
      return;
    }
    let active = true;
    const requestSessionId = sessionId;
    const requestWorkspaceId = workspaceId;
    setSnapshot((current) =>
      current &&
      current.sessionId === requestSessionId &&
      current.workspaceId === requestWorkspaceId &&
      current.runId === runId
        ? { ...current, loading: true }
        : null,
    );
    const auth = { workspaceId, accessToken };
    void Promise.allSettled([
      getExperimentRun(auth, runId),
      getExperimentRunProgress(auth, runId),
      getExperimentRunStatistics(auth, runId),
      getExperimentRunFailureTaxonomy(auth, runId),
    ]).then(([runResult, progressResult, statisticsResult, taxonomyResult]) => {
      // Effect cleanup invalidates the complete response set on session,
      // workspace, credential, run or refresh-generation changes.
      if (!active) return;
      setSnapshot({
        sessionId: requestSessionId,
        workspaceId: requestWorkspaceId,
        runId,
        loading: false,
        run: settledValue(runResult),
        progress: settledValue(progressResult),
        statistics: settledValue(statisticsResult),
        taxonomy: settledValue(taxonomyResult),
        errors: {
          run: settledError(runResult),
          progress: settledError(progressResult),
          statistics: settledError(statisticsResult),
          taxonomy: settledError(taxonomyResult),
        },
      });
    });
    return () => {
      active = false;
    };
  }, [
    connected,
    workspaceId,
    accessToken,
    sessionId,
    runId,
    refreshKey,
    refreshNonce,
  ]);

  const current =
    snapshot &&
    snapshot.sessionId === sessionId &&
    snapshot.workspaceId === workspaceId &&
    snapshot.runId === runId
      ? snapshot
      : null;
  const refresh = () => setRefreshNonce((value) => value + 1);
  const variants = orderedStatisticsVariants(current?.statistics);
  const taxonomyVariants = orderedTaxonomyVariants(current?.taxonomy);
  const evidenceLevels = Array.from(
    new Set(
      [
        current?.run?.evidence_level,
        current?.statistics?.evidence_level,
        current?.taxonomy?.evidence_level,
      ].filter((value): value is string => typeof value === "string" && value.length > 0),
    ),
  );
  const evidenceGuidance = evidenceLevels.map((level) => evidenceLevelGuidance(level, locale));
  if (
    evidenceLevels.length === 0 &&
    (current?.run || current?.statistics || current?.taxonomy)
  ) {
    evidenceGuidance.push(evidenceLevelGuidance(null, locale));
  }

  return (
    <Panel
      title={text.title}
      eyebrow={text.eyebrow}
      actions={
        <button
          type="button"
          className="button button-ghost"
          onClick={refresh}
          disabled={!connected || current?.loading}
        >
          {current?.loading ? text.refreshing : text.refresh}
        </button>
      }
    >
      {!connected ? (
        <SessionRequired contextKey="session.context.evaluations" />
      ) : !current ? (
        <LoadingState />
      ) : (
        <>
          {current.errors.run && (
            <ErrorState
              code={current.errors.run.code}
              message={current.errors.run.message}
              onRetry={refresh}
            />
          )}
          {current.errors.statistics && (
            <InlineError error={current.errors.statistics} fallback={text.notAvailable} />
          )}
          {current.errors.taxonomy && (
            <InlineError error={current.errors.taxonomy} fallback={text.notAvailable} />
          )}

          {(current.run || current.progress || current.statistics || current.taxonomy) && (
            <>
              <section className="state-block state-inline" aria-label={text.execution}>
                <div className="run-facts">
                  <span>
                    {text.experimentRun}
                    <strong>
                      {current.run ? (
                        <StatusBadge status={current.run.status} />
                      ) : (
                        text.notAvailable
                      )}
                    </strong>
                  </span>
                </div>
                <p className="state-hint">{text.executionSplit}</p>
                {current.progress ? (
                  <>
                    <p className="state-title">{text.caseProgress}</p>
                    <div
                      className="progress-track"
                      role="progressbar"
                      aria-label={`${text.progress}: ${rateValue(
                        current.progress.progress,
                        formatPercent,
                        text.notAvailable,
                      )}`}
                      aria-valuemin={0}
                      aria-valuemax={100}
                      aria-valuenow={Math.round(current.progress.progress * 100)}
                    >
                      <div
                        className="progress-fill"
                        style={{ width: `${Math.min(100, Math.max(0, current.progress.progress * 100))}%` }}
                      />
                    </div>
                    <div className="run-facts">
                      <span>
                        {text.progress}
                        <strong>{formatPercent(current.progress.progress)}</strong>
                      </span>
                      <span>
                        {text.total}
                        <strong>{formatCount(current.progress.total)}</strong>
                      </span>
                      <span>
                        {text.pending}
                        <strong>{formatCount(current.progress.pending)}</strong>
                      </span>
                      <span>
                        {text.running}
                        <strong>{formatCount(current.progress.running)}</strong>
                      </span>
                      <span>
                        {text.completed}
                        <strong>{formatCount(current.progress.completed)}</strong>
                      </span>
                      <span>
                        {text.failed}
                        <strong>{formatCount(current.progress.failed)}</strong>
                      </span>
                      <span>
                        {text.cancelled}
                        <strong>{formatCount(current.progress.cancelled)}</strong>
                      </span>
                    </div>
                  </>
                ) : (
                  <p className="state-hint">{text.progressUnavailable}</p>
                )}
                {current.errors.progress && (
                  <InlineError error={current.errors.progress} fallback={text.notAvailable} />
                )}
              </section>

              <section className="state-block state-inline" aria-label={text.backendEvidence}>
                <div className="run-facts">
                  {current.run && (
                    <span>
                      {text.runEvidence}
                      <strong>
                        <code>{current.run.evidence_level ?? text.notRecorded}</code>
                      </strong>
                    </span>
                  )}
                  {current.statistics && (
                    <span>
                      {text.statisticsEvidence}
                      <strong>
                        <code>{current.statistics.evidence_level ?? text.notRecorded}</code>
                      </strong>
                    </span>
                  )}
                  {current.taxonomy && (
                    <span>
                      {text.taxonomyEvidence}
                      <strong>
                        <code>{current.taxonomy.evidence_level ?? text.notRecorded}</code>
                      </strong>
                    </span>
                  )}
                </div>
                {evidenceLevels.length > 1 && (
                  <p className="inline-notice" role="status">
                    {text.evidenceMismatch}
                  </p>
                )}
                {evidenceGuidance.map((item) => (
                  <p className="state-hint" key={item.level ?? "not-recorded"}>
                    <code>{item.level ?? text.notRecorded}</code>: {item.guidance}
                  </p>
                ))}
              </section>
            </>
          )}

          {current.statistics && variants.length > 0 && (
            <section aria-label={text.variants}>
              <p className="eyebrow">{text.variants}</p>
              {variants.map(({ id, variant }) => {
                const outcomes = taskOutcomeCounts(variant);
                const successInterval = usableInterval(variant.success_rate_interval);
                const bootstrapInterval = usableInterval(
                  variant.success_rate_interval_bootstrap,
                );
                const passInterval = usableInterval(variant.pass_hat_k_interval);
                const successReason = taskOutcomeUnavailableReason(variant, locale);
                const passReason = passHatKUnavailableReason(variant, locale);
                return (
                  <article className="state-block" key={id}>
                    <p className="state-title">{variant.variant_label}</p>
                    <p className="state-hint">
                      {text.samples}: {formatCount(variant.case_count)} {text.cases} ·{" "}
                      {formatCount(variant.trial_count)} {text.trials} · {text.repetitions}{" "}
                      {formatCount(variant.k)} · {text.completeCases}{" "}
                      {formatCount(variant.complete_case_count)} · {text.incompleteCases}{" "}
                      {formatCount(variant.incomplete_case_count)}
                    </p>

                    <div className="kpi-grid">
                      <MetricCard
                        label={text.successRate}
                        value={rateValue(variant.success_rate, formatPercent, text.notAvailable)}
                        hint={
                          successReason ??
                          `${formatCount(variant.success_count)} / ${formatCount(
                            variant.known_trial_count,
                          )}. ${text.successRateHint}`
                        }
                      />
                      <MetricCard
                        label={text.passHatK}
                        value={rateValue(variant.pass_hat_k, formatPercent, text.notAvailable)}
                        hint={
                          passReason ??
                          `${formatCount(variant.complete_case_count)} ${text.completeCases}. ${text.passHatKHint}`
                        }
                      />
                      <MetricCard
                        label={text.completeCaseRate}
                        value={rateValue(
                          variant.complete_case_rate,
                          formatPercent,
                          text.notAvailable,
                        )}
                        hint={
                          variant.complete_case_rate == null
                            ? text.nullRate
                            : `${formatCount(variant.complete_case_count)} / ${formatCount(
                                variant.case_count,
                              )}. ${text.completeRateHint}`
                        }
                      />
                      <MetricCard
                        label={text.successCoverage}
                        value={rateValue(
                          variant.success_coverage_rate,
                          formatPercent,
                          text.notAvailable,
                        )}
                        hint={
                          variant.success_coverage_rate == null
                            ? text.nullRate
                            : `${formatCount(variant.known_trial_count)} / ${formatCount(
                                variant.trial_count,
                              )}. ${text.coverageHint}`
                        }
                      />
                    </div>

                    <p className="eyebrow">{text.taskOutcomes}</p>
                    <div className="run-facts">
                      <span>
                        {text.knownTrials}
                        <strong>{formatCount(variant.known_trial_count)}</strong>
                      </span>
                      <span>
                        {text.taskSuccesses}
                        <strong>{formatCount(variant.success_count)}</strong>
                      </span>
                      <span>
                        {text.taskFailures}
                        <strong>
                          {outcomes ? formatCount(outcomes.failureCount) : text.notAvailable}
                        </strong>
                      </span>
                      <span>
                        {text.unknownTrials}
                        <strong>{formatCount(variant.unknown_trial_count)}</strong>
                      </span>
                    </div>
                    {!outcomes && (
                      <p className="inline-notice" role="status">
                        {text.countsUnavailable}
                      </p>
                    )}
                    {runExecutionSucceededWithoutTaskSuccess(current.run?.status, variant) && (
                      <p className="inline-notice" role="status">
                        {text.executionAndTaskNotice}
                      </p>
                    )}
                    {variant.k === 1 && (
                      <p className="state-hint">{kOneLimitation(locale)}</p>
                    )}
                    {variant.incomplete_case_count > 0 && (
                      <p className="state-hint">{text.incompleteNotice}</p>
                    )}

                    <p className="eyebrow">{text.intervals}</p>
                    <div className="split-list">
                      <div className="stat-row">
                        <span>{text.knownTrialInterval}</span>
                        <strong className="stat-row-value">
                          {intervalValue(
                            successInterval,
                            formatPercent,
                            text.noIntervalRecorded,
                          )}
                        </strong>
                      </div>
                      <div className="stat-row">
                        <span>{text.caseBootstrapInterval}</span>
                        <strong className="stat-row-value">
                          {intervalValue(
                            bootstrapInterval,
                            formatPercent,
                            text.noIntervalRecorded,
                          )}
                        </strong>
                      </div>
                      <div className="stat-row">
                        <span>{text.passHatKInterval}</span>
                        <strong className="stat-row-value">
                          {intervalValue(passInterval, formatPercent, text.noIntervalRecorded)}
                        </strong>
                      </div>
                    </div>

                    <div className="run-facts">
                      <span>
                        {text.literalConsistencyValue}
                        <strong>
                          {rateValue(
                            variant.literal_consistency_at_k,
                            formatPercent,
                            text.notAvailable,
                          )}
                        </strong>
                      </span>
                    </div>
                    <p className="state-hint">{literalConsistencyLimitation(locale)}</p>
                  </article>
                );
              })}
            </section>
          )}

          {current.taxonomy && taxonomyVariants.length > 0 && (
            <section className="state-block state-inline" aria-label={text.failureTaxonomy}>
              <p className="eyebrow">
                {text.failureTaxonomy} · {text.taxonomyVersion}{" "}
                <code>{current.taxonomy.taxonomy_version}</code>
              </p>
              {taxonomyVariants.map(({ id, variant }) => {
                const counts = orderedFailureCounts(variant.counts);
                return (
                  <div key={id}>
                    <p className="state-title">{variant.variant_label}</p>
                    <p className="state-hint">
                      {text.labelledCases}: {formatCount(variant.labeled_case_count)} /{" "}
                      {formatCount(variant.case_count)}
                    </p>
                    {counts.length === 0 ? (
                      <p className="state-hint">{text.noRuleLabels}</p>
                    ) : (
                      <div className="split-list">
                        {counts.map(([code, count]) => (
                          <div className="stat-row" key={code}>
                            <code>{code}</code>
                            <strong className="stat-row-value">{formatCount(count)}</strong>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                );
              })}
              <p className="state-hint">{text.taxonomyNotCompliance}</p>
            </section>
          )}

          {!current.loading &&
            !current.run &&
            !current.progress &&
            !current.statistics &&
            !current.taxonomy && (
              <EmptyState title={text.noData} hint={text.noDataHint} />
            )}
        </>
      )}
    </Panel>
  );
}

export default RunStatistics;
