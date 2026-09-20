"use client";

import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";

import StatusBadge from "../../../../components/status-badge";
import { EmptyState, ErrorState, Panel } from "../../../../components/states";
import TechnicalDetails from "../../../../components/technical-details";
import { ApiError, toApiError } from "../../../../lib/api-client";
import {
  EvaluationComparison,
  EvaluationExperimentRun,
  EvaluationExperimentVariant,
  EvaluationAblation,
  MetricSnapshotPayload,
  MetricValueView,
  PairedMetricView,
  ReleaseGateDecision,
  ReleaseGatePolicy,
  comparableMetricEntries,
  createExperimentAblation,
  createExperimentComparison,
  createReleaseGateDecision,
  getExperimentAblation,
  getExperimentRunMetrics,
  listExperimentComparisons,
  listReleaseGateDecisions,
  listReleaseGatePolicies,
  materializeExperimentRunMetrics,
  narrowMetricGroup,
  uniqueById,
  upsertById,
} from "../../../../lib/evaluation";
import { useI18n } from "../../../../i18n/provider";

type AuthInput = { workspaceId: string; accessToken: string; sessionId: number };

type MetricsState =
  | { kind: "loading" }
  | { kind: "notMaterialized" }
  | { kind: "ready"; payload: MetricSnapshotPayload }
  | { kind: "error"; error: ApiError };

function metricDisplayValue(metric: MetricValueView): string {
  if (metric.status !== "AVAILABLE") return "—";
  return metric.value === null || metric.value === undefined ? "—" : String(metric.value);
}

function isDeprecated(metric: MetricValueView): boolean {
  return (metric.details as { deprecated?: unknown } | null)?.deprecated === true;
}

function StepHeader({ step, title, eyebrow }: { step: number; title: string; eyebrow: string }) {
  const { t } = useI18n();
  return (
    <div className="workflow-step-header">
      <span className="workflow-step-number" aria-hidden="true">{step}</span>
      <div>
        <p className="eyebrow">{t("evaluation.run.workflow.step", { number: step })} · {eyebrow}</p>
        <h3>{title}</h3>
      </div>
    </div>
  );
}

/** Sequential evaluation workflow: Metrics → Comparison → Ablation → Release gate. */
export default function RunWorkflow({
  input,
  run,
  variants,
  terminal,
}: {
  input: AuthInput;
  run: EvaluationExperimentRun;
  variants: EvaluationExperimentVariant[];
  terminal: boolean;
}) {
  const { t, formatDateTime } = useI18n();
  const [metrics, setMetrics] = useState<MetricsState>({ kind: "loading" });
  const [materializing, setMaterializing] = useState(false);
  const [metricsError, setMetricsError] = useState<string | null>(null);

  const [comparisons, setComparisons] = useState<EvaluationComparison[] | null>(null);
  const [comparisonsError, setComparisonsError] = useState<ApiError | null>(null);
  const [selectedComparisonId, setSelectedComparisonId] = useState<string | null>(null);
  const [baselineId, setBaselineId] = useState("");
  const [candidateId, setCandidateId] = useState("");
  const [creatingComparison, setCreatingComparison] = useState(false);
  const [comparisonError, setComparisonError] = useState<string | null>(null);

  const [ablation, setAblation] = useState<EvaluationAblation | null>(null);
  const [ablationState, setAblationState] = useState<"loading" | "absent" | "ready" | "error">("loading");
  const [ablationError, setAblationError] = useState<string | null>(null);
  const [creatingAblation, setCreatingAblation] = useState(false);

  const [policies, setPolicies] = useState<ReleaseGatePolicy[] | null>(null);
  const [policiesError, setPoliciesError] = useState<ApiError | null>(null);
  const [policyId, setPolicyId] = useState("");
  const [decisions, setDecisions] = useState<ReleaseGateDecision[]>([]);
  const [decisionsLoaded, setDecisionsLoaded] = useState(false);
  const [decisionsError, setDecisionsError] = useState<ApiError | null>(null);
  const [runningGate, setRunningGate] = useState(false);
  const [gateError, setGateError] = useState<string | null>(null);
  const activeSessionRef = useRef(input.sessionId);
  activeSessionRef.current = input.sessionId;

  useEffect(() => {
    setMetrics({ kind: "loading" });
    setMetricsError(null);
    setMaterializing(false);
    setComparisons(null);
    setComparisonsError(null);
    setSelectedComparisonId(null);
    setBaselineId("");
    setCandidateId("");
    setCreatingComparison(false);
    setComparisonError(null);
    setAblation(null);
    setAblationState("loading");
    setAblationError(null);
    setCreatingAblation(false);
    setPolicies(null);
    setPoliciesError(null);
    setPolicyId("");
    setDecisions([]);
    setDecisionsLoaded(false);
    setDecisionsError(null);
    setRunningGate(false);
    setGateError(null);
  }, [input.sessionId, run.id]);

  const gateEligible = run.purpose === "RELEASE_GATE" && run.split === "HOLDOUT";

  const loadMetrics = useCallback(async () => {
    const requestSessionId = input.sessionId;
    setMetrics({ kind: "loading" });
    setMetricsError(null);
    try {
      const payload = await getExperimentRunMetrics(input, run.id);
      if (activeSessionRef.current !== requestSessionId) return;
      setMetrics({ kind: "ready", payload });
    } catch (caught) {
      if (activeSessionRef.current !== requestSessionId) return;
      const apiError = toApiError(caught, "");
      if (apiError.code === "EVALUATION_METRICS_NOT_MATERIALIZED") {
        setMetrics({ kind: "notMaterialized" });
      } else {
        setMetrics({ kind: "error", error: apiError });
      }
    }
  }, [run.id, input.sessionId, input.workspaceId, input.accessToken]);

  useEffect(() => {
    if (terminal) void loadMetrics();
  }, [terminal, loadMetrics]);

  async function materialize() {
    const requestSessionId = input.sessionId;
    setMaterializing(true);
    setMetricsError(null);
    try {
      const payload = await materializeExperimentRunMetrics(input, run.id);
      if (activeSessionRef.current !== requestSessionId) return;
      setMetrics({ kind: "ready", payload });
    } catch (caught) {
      const apiError = toApiError(caught, "");
      if (activeSessionRef.current === requestSessionId) setMetricsError(apiError.message || apiError.code);
    } finally {
      if (activeSessionRef.current === requestSessionId) setMaterializing(false);
    }
  }

  const metricsReady = metrics.kind === "ready";

  const loadComparisons = useCallback(async () => {
    const requestSessionId = input.sessionId;
    setComparisonsError(null);
    try {
      const list = await listExperimentComparisons(input, run.id);
      if (activeSessionRef.current !== requestSessionId) return;
      const uniqueComparisons = uniqueById(list);
      setComparisons(uniqueComparisons);
      setSelectedComparisonId((current) =>
        current && uniqueComparisons.some((comparison) => comparison.id === current)
          ? current
          : uniqueComparisons[uniqueComparisons.length - 1]?.id ?? null,
      );
    } catch (caught) {
      if (activeSessionRef.current === requestSessionId) setComparisonsError(toApiError(caught, ""));
    }
  }, [run.id, input.sessionId, input.workspaceId, input.accessToken]);

  useEffect(() => {
    if (metricsReady) void loadComparisons();
  }, [metricsReady, loadComparisons]);

  async function createComparison(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setComparisonError(null);
    if (!baselineId || !candidateId || baselineId === candidateId) return;
    const requestSessionId = input.sessionId;
    setCreatingComparison(true);
    try {
      const comparison = await createExperimentComparison(input, run.id, {
        baseline_variant_id: baselineId,
        candidate_variant_id: candidateId,
      });
      if (activeSessionRef.current !== requestSessionId) return;
      setComparisons((current) => upsertById(current, comparison));
      setSelectedComparisonId(comparison.id);
      setBaselineId("");
      setCandidateId("");
    } catch (caught) {
      const apiError = toApiError(caught, "");
      if (activeSessionRef.current === requestSessionId) setComparisonError(apiError.message || apiError.code);
    } finally {
      if (activeSessionRef.current === requestSessionId) setCreatingComparison(false);
    }
  }

  const selectedComparison = comparisons?.find((c) => c.id === selectedComparisonId) ?? null;

  const loadAblation = useCallback(async () => {
    if (!selectedComparisonId) return;
    const requestSessionId = input.sessionId;
    setAblationState("loading");
    setAblationError(null);
    try {
      const nextAblation = await getExperimentAblation(input, run.id, selectedComparisonId);
      if (activeSessionRef.current !== requestSessionId) return;
      setAblation(nextAblation);
      setAblationState("ready");
    } catch (caught) {
      if (activeSessionRef.current !== requestSessionId) return;
      const apiError = toApiError(caught, "");
      if (apiError.code === "ABLATION_NOT_FOUND" || apiError.status === 404) {
        setAblationState("absent");
      } else {
        setAblationError(apiError.message || apiError.code);
        setAblationState("error");
      }
    }
  }, [selectedComparisonId, run.id, input.sessionId, input.workspaceId, input.accessToken]);

  useEffect(() => {
    setAblation(null);
    setAblationError(null);
    if (selectedComparisonId) void loadAblation();
  }, [selectedComparisonId, loadAblation]);

  async function createAblation() {
    if (!selectedComparisonId) return;
    const requestSessionId = input.sessionId;
    setCreatingAblation(true);
    setAblationError(null);
    try {
      const nextAblation = await createExperimentAblation(input, run.id, selectedComparisonId);
      if (activeSessionRef.current !== requestSessionId) return;
      setAblation(nextAblation);
      setAblationState("ready");
    } catch (caught) {
      const apiError = toApiError(caught, "");
      if (activeSessionRef.current === requestSessionId) setAblationError(apiError.message || apiError.code);
    } finally {
      if (activeSessionRef.current === requestSessionId) setCreatingAblation(false);
    }
  }

  const loadPolicies = useCallback(async () => {
    const requestSessionId = input.sessionId;
    setPoliciesError(null);
    try {
      const nextPolicies = await listReleaseGatePolicies(input);
      if (activeSessionRef.current !== requestSessionId) return;
      setPolicies(uniqueById(nextPolicies));
    } catch (caught) {
      if (activeSessionRef.current === requestSessionId) setPoliciesError(toApiError(caught, ""));
    }
  }, [input.sessionId, input.workspaceId, input.accessToken]);

  const loadDecisions = useCallback(async () => {
    if (!selectedComparisonId) return;
    const requestSessionId = input.sessionId;
    setDecisionsError(null);
    try {
      const nextDecisions = await listReleaseGateDecisions(input, run.id, selectedComparisonId);
      if (activeSessionRef.current !== requestSessionId) return;
      setDecisions(uniqueById(nextDecisions));
      setDecisionsLoaded(true);
    } catch (caught) {
      if (activeSessionRef.current === requestSessionId) setDecisionsError(toApiError(caught, ""));
    }
  }, [selectedComparisonId, run.id, input.sessionId, input.workspaceId, input.accessToken]);

  useEffect(() => {
    if (gateEligible) void loadPolicies();
  }, [gateEligible, loadPolicies]);

  useEffect(() => {
    setDecisions([]);
    setDecisionsLoaded(false);
    setDecisionsError(null);
    if (selectedComparisonId && gateEligible) void loadDecisions();
  }, [selectedComparisonId, gateEligible, loadDecisions]);

  async function runGate() {
    if (!selectedComparisonId || !policyId) return;
    const requestSessionId = input.sessionId;
    setRunningGate(true);
    setGateError(null);
    try {
      const decision = await createReleaseGateDecision(input, run.id, selectedComparisonId, { policy_id: policyId });
      if (activeSessionRef.current !== requestSessionId) return;
      setDecisions((current) => upsertById(current, decision));
      setPolicyId("");
    } catch (caught) {
      const apiError = toApiError(caught, "");
      if (activeSessionRef.current === requestSessionId) setGateError(apiError.message || apiError.code);
    } finally {
      if (activeSessionRef.current === requestSessionId) setRunningGate(false);
    }
  }

  const variantLabel = (id: string) => variants.find((v) => v.id === id)?.label ?? id.slice(0, 8) + "…";

  return (
    <>
      {/* ---------- 1 Metrics ---------- */}
      <Panel ariaLabel={t("evaluation.metrics.stepTitle")}>
        <StepHeader step={1} title={t("evaluation.metrics.stepTitle")} eyebrow={t("evaluation.metrics.stepTitle")} />
        {!terminal && <p className="state-hint">{t("evaluation.run.workflow.needsTerminal")}</p>}
        {terminal && metrics.kind === "notMaterialized" && (
          <div className="state-block state-inline">
            <p className="state-title">{t("evaluation.metrics.notMaterialized")}</p>
            <p className="state-hint">{t("evaluation.metrics.materializeHint")}</p>
            <button type="button" className="button button-primary" onClick={() => void materialize()} disabled={materializing}>
              {materializing ? t("evaluation.metrics.materializing") : t("evaluation.metrics.materialize")}
            </button>
            {metricsError && <p className="session-error" role="alert">{metricsError}</p>}
          </div>
        )}
        {terminal && metrics.kind === "error" && (
          <ErrorState code={metrics.error.code} message={metrics.error.message || t("evaluation.metrics.loadError")} onRetry={() => void loadMetrics()} />
        )}
        {terminal && metrics.kind === "ready" && (
          <MetricsView payload={metrics.payload} />
        )}
      </Panel>

      {/* ---------- 2 Comparison ---------- */}
      <Panel ariaLabel={t("evaluation.comparison.stepTitle")}>
        <StepHeader step={2} title={t("evaluation.comparison.stepTitle")} eyebrow={t("evaluation.comparison.stepTitle")} />
        {!metricsReady && <p className="state-hint">{t("evaluation.comparison.needsMetrics")}</p>}
        {metricsReady && (
          <>
            {comparisonsError && (
              <ErrorState code={comparisonsError.code} message={comparisonsError.message || t("evaluation.comparison.loadError")} onRetry={() => void loadComparisons()} />
            )}
            {variants.length >= 2 && (
              <form className="eval-form" onSubmit={createComparison} noValidate>
                <div className="eval-form-title">{t("evaluation.comparison.createTitle")}</div>
                <div className="form-grid">
                  <label>
                    {t("evaluation.comparison.baseline")}
                    <select value={baselineId} onChange={(e) => setBaselineId(e.target.value)}>
                      <option value="">—</option>
                      {variants.map((variant) => (
                        <option value={variant.id} key={variant.id}>
                          {variant.label}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    {t("evaluation.comparison.candidate")}
                    <select value={candidateId} onChange={(e) => setCandidateId(e.target.value)}>
                      <option value="">—</option>
                      {variants.map((variant) => (
                        <option value={variant.id} key={variant.id}>
                          {variant.label}
                        </option>
                      ))}
                    </select>
                  </label>
                </div>
                {baselineId && candidateId && baselineId === candidateId && (
                  <p className="session-error" role="alert">{t("evaluation.comparison.sameVariantError")}</p>
                )}
                {comparisonError && <p className="session-error" role="alert">{comparisonError}</p>}
                <div className="form-actions">
                  <button
                    type="submit"
                    className="button button-primary"
                    disabled={creatingComparison || !baselineId || !candidateId || baselineId === candidateId}
                  >
                    {creatingComparison ? t("evaluation.comparison.creating") : t("evaluation.comparison.create")}
                  </button>
                </div>
              </form>
            )}

            {comparisons && comparisons.length > 0 && (
              <div className="comparison-picker">
                {comparisons.map((comparison) => (
                  <button
                    type="button"
                    key={comparison.id}
                    className={`comparison-option${comparison.id === selectedComparisonId ? " selected" : ""}`}
                    aria-pressed={comparison.id === selectedComparisonId}
                    onClick={() => setSelectedComparisonId(comparison.id)}
                  >
                    {variantLabel(comparison.baseline_variant_id)} → {variantLabel(comparison.candidate_variant_id)}
                    <StatusBadge status={comparison.status} />
                  </button>
                ))}
              </div>
            )}
            {comparisons && comparisons.length === 0 && <EmptyState title={t("evaluation.comparison.empty")} />}

            {selectedComparison && <ComparisonView comparison={selectedComparison} variantLabel={variantLabel} />}
          </>
        )}
      </Panel>

      {/* ---------- 3 Ablation ---------- */}
      <Panel ariaLabel={t("evaluation.ablation.stepTitle")}>
        <StepHeader step={3} title={t("evaluation.ablation.stepTitle")} eyebrow={t("evaluation.ablation.stepTitle")} />
        {!selectedComparison && <p className="state-hint">{t("evaluation.ablation.needsComparison")}</p>}
        {selectedComparison && ablationState === "loading" && <p className="state-hint">{t("common.loading")}</p>}
        {selectedComparison && ablationState === "error" && ablationError && (
          <ErrorState
            code="ABLATION_LOAD_FAILED"
            message={ablationError}
            onRetry={() => void loadAblation()}
          />
        )}
        {selectedComparison && ablationState === "absent" && (
          <div className="state-block state-inline">
            <p className="state-title">{t("evaluation.ablation.empty")}</p>
            <button type="button" className="button button-primary" onClick={() => void createAblation()} disabled={creatingAblation}>
              {creatingAblation ? t("evaluation.ablation.creating") : t("evaluation.ablation.create")}
            </button>
            {ablationError && <p className="session-error" role="alert">{ablationError}</p>}
          </div>
        )}
        {selectedComparison && ablation && <AblationView ablation={ablation} variantLabel={variantLabel} />}
      </Panel>

      {/* ---------- 4 Release gate ---------- */}
      <Panel ariaLabel={t("evaluation.gate.stepTitle")}>
        <StepHeader step={4} title={t("evaluation.gate.stepTitle")} eyebrow={t("evaluation.gate.stepTitle")} />
        <p className="state-hint">{t("evaluation.gate.disclaimer")}</p>
        {!gateEligible && <p className="state-hint">{t("evaluation.gate.eligibilityNotice")}</p>}
        {gateEligible && !selectedComparison && <p className="state-hint">{t("evaluation.gate.needsComparison")}</p>}
        {gateEligible && selectedComparison && (
          <>
            {policiesError && (
              <ErrorState
                code={policiesError.code}
                message={policiesError.message || t("errors.loadEvaluation")}
                onRetry={() => void loadPolicies()}
              />
            )}
            {!policiesError && policies === null && <p className="state-hint">{t("common.loading")}</p>}
            {!policiesError && policies !== null && policies.length === 0 && <p className="state-hint">{t("evaluation.gate.noPolicies")}</p>}
            {!policiesError && policies && policies.length > 0 && (
              <form
                className="eval-form"
                onSubmit={(event) => {
                  event.preventDefault();
                  void runGate();
                }}
                noValidate
              >
                <label>
                  {t("evaluation.gate.policySelect")}
                  <select value={policyId} onChange={(e) => setPolicyId(e.target.value)}>
                    <option value="">—</option>
                    {policies.map((policy) => (
                      <option value={policy.id} key={policy.id}>
                        {policy.name} · {policy.policy_json.rules.length} rules
                      </option>
                    ))}
                  </select>
                </label>
                {gateError && <p className="session-error" role="alert">{gateError}</p>}
                <div className="form-actions">
                  <button type="submit" className="button button-primary" disabled={runningGate || !policyId}>
                    {runningGate ? t("evaluation.gate.running") : t("evaluation.gate.run")}
                  </button>
                </div>
              </form>
            )}
            {decisionsError && (
              <ErrorState
                code={decisionsError.code}
                message={decisionsError.message || t("errors.loadEvaluation")}
                onRetry={() => void loadDecisions()}
              />
            )}
            {!decisionsError && decisionsLoaded && decisions.length === 0 && <EmptyState title={t("evaluation.gate.empty")} />}
            {!decisionsError && decisions.map((decision) => (
              <GateDecisionView key={decision.id} decision={decision} policy={policies?.find((p) => p.id === decision.policy_id) ?? null} />
            ))}
          </>
        )}
      </Panel>
    </>
  );
}

function MetricsView({ payload }: { payload: MetricSnapshotPayload }) {
  const { t, formatNumber } = useI18n();
  const variantEntries = Object.entries(payload.variants ?? {});

  function MetricRows({ metrics }: { metrics: MetricValueView[] }) {
    if (metrics.length === 0) return null;
    return (
      <div className="data-table">
        <table>
          <thead>
            <tr>
              <th scope="col">{t("evaluation.metrics.columns.metric")}</th>
              <th scope="col">{t("evaluation.metrics.columns.status")}</th>
              <th scope="col">{t("evaluation.metrics.columns.value")}</th>
              <th scope="col">{t("evaluation.metrics.columns.samples")}</th>
            </tr>
          </thead>
          <tbody>
            {metrics
              .slice()
              .sort((a, b) => a.name.localeCompare(b.name))
              .map((metric) => (
                <tr key={metric.name} className={isDeprecated(metric) ? "deprecated-row" : undefined}>
                  <td data-label={t("evaluation.metrics.columns.metric")}>
                    <code>{metric.name}</code>
                    {isDeprecated(metric) && <span className="deprecated-tag">{t("evaluation.metrics.deprecated")}</span>}
                  </td>
                  <td data-label={t("evaluation.metrics.columns.status")}>
                    <StatusBadge status={metric.status} />
                  </td>
                  <td data-label={t("evaluation.metrics.columns.value")}>
                    {metricDisplayValue(metric)}
                    {metric.status === "AVAILABLE" && metric.numerator !== null && metric.denominator !== null && (
                      <span className="muted"> ({metric.numerator}/{metric.denominator})</span>
                    )}
                  </td>
                  <td data-label={t("evaluation.metrics.columns.samples")}>{t("evaluation.metrics.samples", { count: formatNumber(metric.sample_count) })}</td>
                </tr>
              ))}
          </tbody>
        </table>
      </div>
    );
  }

  function ReasonRows({ metrics }: { metrics: MetricValueView[] }) {
    const problematic = metrics.filter((m) => m.status !== "AVAILABLE" && m.reason);
    if (problematic.length === 0) return null;
    return (
      <div className="split-list">
        {problematic.map((metric) => (
          <div className="split-row" key={metric.name}>
            <span><code>{metric.name}</code></span>
            <span className="muted">{t("evaluation.metrics.reasonLabel")}: {metric.reason}</span>
          </div>
        ))}
      </div>
    );
  }

  const overall = narrowMetricGroup(payload.overall);

  return (
    <>
      <h3>{t("evaluation.metrics.overall")}</h3>
      <MetricRows metrics={overall.metrics} />
      <ReasonRows metrics={overall.metrics} />
      {Object.keys(overall.categories).length > 0 && (
        <>
          <h3>{t("evaluation.metrics.categories")}</h3>
          {Object.entries(overall.categories).map(([category, metrics]) => (
            <div key={category}>
              <p className="eyebrow">{category}</p>
              <MetricRows metrics={metrics} />
            </div>
          ))}
        </>
      )}
      <h3>{t("evaluation.metrics.byVariant")}</h3>
      {variantEntries.length === 0 && <EmptyState title={t("evaluation.comparison.empty")} />}
      {variantEntries.map(([variantId, node]) => {
        const group = narrowMetricGroup(node);
        return (
          <div key={variantId}>
            <p className="eyebrow">
              <code>{variantId.slice(0, 8)}…</code>
            </p>
            <MetricRows metrics={group.metrics} />
            {Object.entries(group.categories).map(([category, metrics]) => (
              <div key={category}>
                <p className="state-hint">{category}</p>
                <MetricRows metrics={metrics} />
              </div>
            ))}
          </div>
        );
      })}
      <TechnicalDetails
        summary={t("evaluation.metrics.snapshotHashes")}
        value={{
          snapshot_id: payload.snapshot_id,
          evaluator_manifest_hash: payload.evaluator_manifest_hash,
          case_result_set_hash: payload.case_result_set_hash,
          metrics_hash: payload.metrics_hash,
        }}
      />
    </>
  );
}

function ComparisonView({
  comparison,
  variantLabel,
}: {
  comparison: EvaluationComparison;
  variantLabel: (id: string) => string;
}) {
  const { t, statusLabel } = useI18n();
  return (
    <div className="comparison-detail">
      <p className="comparison-variant-line">
        <strong>{variantLabel(comparison.baseline_variant_id)}</strong>
        {" → "}
        <strong>{variantLabel(comparison.candidate_variant_id)}</strong>
        <StatusBadge status={comparison.status} />
      </p>
      <div className="data-table">
        <table>
          <thead>
            <tr>
              <th scope="col">{t("evaluation.comparison.metricColumns.metric")}</th>
              <th scope="col">{t("evaluation.comparison.metricColumns.baseline")}</th>
              <th scope="col">{t("evaluation.comparison.metricColumns.candidate")}</th>
              <th scope="col">{t("evaluation.comparison.metricColumns.direction")}</th>
              <th scope="col">{t("evaluation.comparison.metricColumns.delta")}</th>
              <th scope="col">{t("evaluation.comparison.metricColumns.relativeDelta")}</th>
              <th scope="col">{t("evaluation.comparison.metricColumns.pairs")}</th>
              <th scope="col">{t("evaluation.comparison.metricColumns.status")}</th>
            </tr>
          </thead>
          <tbody>
            {comparableMetricEntries(comparison).map(([name, paired]) => (
              <PairedMetricRow key={name} name={name} paired={paired} />
            ))}
          </tbody>
        </table>
      </div>
      <TechnicalDetails
        summary={t("evaluation.comparison.hashes")}
        value={{
          metric_snapshot_hash: comparison.metric_snapshot_hash,
          baseline_variant_hash: comparison.baseline_variant_hash,
          candidate_variant_hash: comparison.candidate_variant_hash,
          evaluator_manifest_hash: comparison.evaluator_manifest_hash,
          comparison_hash: comparison.comparison_hash,
          missing_pairs: comparison.missing_pairs,
          paired_pairs: comparison.paired_pairs,
        }}
      />
    </div>
  );
}

function PairedMetricRow({ name, paired }: { name: string; paired: PairedMetricView }) {
  const { t } = useI18n();
  const directionKey =
    paired.direction === "HIGHER_IS_BETTER" || paired.direction === "LOWER_IS_BETTER"
      ? paired.direction
      : "UNKNOWN";
  const delta =
    paired.absolute_delta === null || paired.absolute_delta === undefined
      ? "—"
      : String(paired.absolute_delta);
  const relative =
    paired.relative_delta === null || paired.relative_delta === undefined
      ? t("evaluation.comparison.notAvailable")
      : `${(paired.relative_delta * 100).toFixed(1)}%`;
  return (
    <tr>
      <td data-label={t("evaluation.comparison.metricColumns.metric")}><code>{name}</code></td>
      <td data-label={t("evaluation.comparison.metricColumns.baseline")}>{metricDisplayValue(paired.baseline)}</td>
      <td data-label={t("evaluation.comparison.metricColumns.candidate")}>{metricDisplayValue(paired.candidate)}</td>
      <td data-label={t("evaluation.comparison.metricColumns.direction")}>{t(`evaluation.metrics.direction.${directionKey}` as never)}</td>
      <td data-label={t("evaluation.comparison.metricColumns.delta")}>{delta}</td>
      <td data-label={t("evaluation.comparison.metricColumns.relativeDelta")}>{relative}</td>
      <td data-label={t("evaluation.comparison.metricColumns.pairs")}>
        {t("evaluation.comparison.winsTiesLosses", {
          wins: paired.paired_win,
          ties: paired.paired_tie,
          losses: paired.paired_loss,
        })}
      </td>
      <td data-label={t("evaluation.comparison.metricColumns.status")}>
        <StatusBadge status={paired.status} />
      </td>
    </tr>
  );
}

function AblationView({
  ablation,
  variantLabel,
}: {
  ablation: EvaluationAblation;
  variantLabel: (id: string) => string;
}) {
  const { t } = useI18n();
  const snapshotChanged = ablation.changed_paths.includes("effective_knowledge_snapshots");
  return (
    <div className="ablation-view">
      <p className="comparison-variant-line">
        <strong>{variantLabel(ablation.baseline_variant_id)}</strong>
        {" → "}
        <strong>{variantLabel(ablation.candidate_variant_id)}</strong>
      </p>
      <p className="state-title">
        {t("evaluation.ablation.factorTitle")}: <StatusBadge status={ablation.factor} />
      </p>
      <p className="state-hint">{t(`evaluation.ablation.factorExplanations.${ablation.factor}` as never)}</p>
      {snapshotChanged && <p className="inline-notice">{t("evaluation.ablation.effectiveSnapshotNotice")}</p>}
      {ablation.changed_paths.length > 0 && (
        <div className="split-list">
          <p className="eyebrow">{t("evaluation.ablation.changedPaths")}</p>
          {ablation.changed_paths.map((path) => (
            <div className="split-row" key={path}>
              <code>{path}</code>
            </div>
          ))}
        </div>
      )}
      <TechnicalDetails
        summary={t("evaluation.ablation.hashes")}
        value={{
          baseline_factor_hash: ablation.baseline_factor_hash,
          candidate_factor_hash: ablation.candidate_factor_hash,
          analysis_hash: ablation.analysis_hash,
        }}
      />
    </div>
  );
}

function GateDecisionView({ decision, policy }: { decision: ReleaseGateDecision; policy: ReleaseGatePolicy | null }) {
  const { t, statusLabel, formatDateTime } = useI18n();
  return (
    <article className="gate-decision">
      <div className="gate-decision-header">
        <span className={`gate-status gate-${decision.status.toLowerCase()}`}>
          {statusLabel(decision.status)}
        </span>
        <div>
          <strong>{policy?.name ?? decision.policy_id.slice(0, 8) + "…"}</strong>
          <span className="state-hint"> · {formatDateTime(decision.created_at)}</span>
        </div>
      </div>
      <div className="data-table">
        <table>
          <thead>
            <tr>
              <th scope="col">{t("evaluation.gate.ruleColumns.metric")}</th>
              <th scope="col">{t("evaluation.gate.ruleColumns.rule")}</th>
              <th scope="col">{t("evaluation.gate.ruleColumns.baseline")}</th>
              <th scope="col">{t("evaluation.gate.ruleColumns.candidate")}</th>
              <th scope="col">{t("evaluation.gate.ruleColumns.result")}</th>
              <th scope="col">{t("evaluation.gate.ruleColumns.reason")}</th>
            </tr>
          </thead>
          <tbody>
            {decision.rule_results.map((result) => (
              <tr key={result.metric + result.rule}>
                <td data-label={t("evaluation.gate.ruleColumns.metric")}><code>{result.metric}</code></td>
                <td data-label={t("evaluation.gate.ruleColumns.rule")}>{result.rule}</td>
                <td data-label={t("evaluation.gate.ruleColumns.baseline")}>
                  {result.baseline === null || result.baseline === undefined ? "—" : String(result.baseline)}
                </td>
                <td data-label={t("evaluation.gate.ruleColumns.candidate")}>
                  {result.candidate === null || result.candidate === undefined ? "—" : String(result.candidate)}
                </td>
                <td data-label={t("evaluation.gate.ruleColumns.result")}>
                  <StatusBadge status={result.status} />
                </td>
                <td data-label={t("evaluation.gate.ruleColumns.reason")} className="muted">
                  {result.reason ?? "—"}
                  {result.status === "PASS" && result.reason === "compensated_tradeoff" && (
                    <span className="inline-notice">{t("evaluation.gate.compensation.passedNotice")}</span>
                  )}
                  {result.compensation && (
                    <span className="state-hint">
                      {t("evaluation.gate.compensation.title")}: <code>{result.compensation.metric}</code> ·{" "}
                      {t("evaluation.gate.compensation.gain")}: {result.compensation.gain ?? "—"}
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {decision.reasons.length > 0 && (
        <p className="state-hint">
          {t("evaluation.gate.reasons")}: {decision.reasons.join(", ")}
        </p>
      )}
      <TechnicalDetails
        summary={t("evaluation.gate.rawReasons")}
        value={{ reasons: decision.reasons, comparison_hash: decision.comparison_hash, policy_hash: decision.policy_hash, decision_hash: decision.decision_hash }}
      />
    </article>
  );
}
