import { apiRequest } from "@/lib/api/client";

/**
 * Typed client for the M7 Evaluation Platform API. Shapes mirror
 * apps/api/schemas/evaluation.py and the evaluation services exactly;
 * the frontend never recomputes backend semantics (metrics, comparisons,
 * gate decisions, ablation factors) — it only presents them.
 */

const BASE = "/api/v1/workspaces";

function evalBase(workspaceId: string): string {
  return `${BASE}/${encodeURIComponent(workspaceId)}/evaluation`;
}

type AuthInput = { workspaceId: string; accessToken: string };

function get<T>(input: AuthInput, path: string): Promise<T> {
  return apiRequest<T>(`${evalBase(input.workspaceId)}${path}`, input.accessToken);
}

function post<T>(input: AuthInput, path: string, body?: unknown): Promise<T> {
  return apiRequest<T>(`${evalBase(input.workspaceId)}${path}`, input.accessToken, {
    method: "POST",
    body: body ?? {},
  });
}

// ---------- Datasets ----------

export type EvaluationDataset = {
  id: string;
  workspace_id: string;
  name: string;
  description: string | null;
  created_by: string;
  created_at: string;
};

export type EvaluationDatasetItemInput = {
  case_key: string;
  split: string;
  category: string;
  input: Record<string, unknown>;
  expected: Record<string, unknown>;
  tags: string[];
  source_provenance: Record<string, unknown>;
  ordinal: number;
};

export type EvaluationDatasetItem = EvaluationDatasetItemInput & {
  id: string;
  workspace_id: string;
  dataset_version_id: string;
};

export type EvaluationDatasetVersion = {
  id: string;
  workspace_id: string;
  dataset_id: string;
  version_number: number;
  schema_version: number;
  content_hash: string;
  status: string;
  created_by: string;
  created_at: string;
  published_at: string | null;
  item_count: number | null;
};

export type EvaluationDatasetVersionDetail = EvaluationDatasetVersion & {
  items: EvaluationDatasetItem[];
};

export function listDatasets(input: AuthInput): Promise<EvaluationDataset[]> {
  return get(input, "/datasets");
}

export function createDataset(
  input: AuthInput,
  payload: { name: string; description: string | null },
): Promise<EvaluationDataset> {
  return post(input, "/datasets", payload);
}

export function listDatasetVersions(input: AuthInput, datasetId: string): Promise<EvaluationDatasetVersion[]> {
  return get(input, `/datasets/${encodeURIComponent(datasetId)}/versions`);
}

export function getDatasetVersion(
  input: AuthInput,
  datasetId: string,
  versionId: string,
): Promise<EvaluationDatasetVersionDetail> {
  return get(input, `/datasets/${encodeURIComponent(datasetId)}/versions/${encodeURIComponent(versionId)}`);
}

export function createDatasetVersion(
  input: AuthInput,
  datasetId: string,
  payload: { schema_version: number; items: EvaluationDatasetItemInput[] },
): Promise<EvaluationDatasetVersion> {
  return post(input, `/datasets/${encodeURIComponent(datasetId)}/versions`, payload);
}

export function publishDatasetVersion(
  input: AuthInput,
  datasetId: string,
  versionId: string,
): Promise<EvaluationDatasetVersion> {
  return post(input, `/datasets/${encodeURIComponent(datasetId)}/versions/${encodeURIComponent(versionId)}/publish`);
}

// ---------- Pricing snapshots ----------

export type PricingSnapshot = {
  id: string;
  workspace_id: string;
  name: string;
  provider: string;
  model: string;
  currency: string;
  input_price_per_1m: number | string;
  output_price_per_1m: number | string;
  cached_input_price_per_1m: number | string | null;
  effective_at: string;
  source_note: string;
  content_hash: string;
  created_at: string;
  created_by: string;
};

export function listPricingSnapshots(input: AuthInput): Promise<PricingSnapshot[]> {
  return get(input, "/pricing-snapshots");
}

export function createPricingSnapshot(
  input: AuthInput,
  payload: {
    name: string;
    provider: string;
    model: string;
    currency: string;
    input_price_per_1m: string;
    output_price_per_1m: string;
    cached_input_price_per_1m: string | null;
    effective_at: string;
    source_note: string;
  },
): Promise<PricingSnapshot> {
  return post(input, "/pricing-snapshots", payload);
}

// ---------- Experiments & variants ----------

export type EvaluationExperimentVariant = {
  id: string;
  workspace_id: string;
  experiment_id: string;
  label: string;
  agent_version_id: string;
  resolved_spec_hash: string;
  pricing_snapshot_id: string;
  pricing_snapshot_hash: string;
  effective_knowledge_snapshots: Array<Record<string, string>>;
  variant_metadata: Record<string, unknown>;
  variant_hash: string;
  ordinal: number;
  created_at: string;
};

export type EvaluationExperiment = {
  id: string;
  workspace_id: string;
  name: string;
  description: string | null;
  dataset_version_id: string;
  dataset_content_hash: string;
  dataset_schema_version: number;
  split: string;
  purpose: string;
  repetitions: number;
  status: string;
  build_sha: string;
  evaluator_manifest: Record<string, unknown>;
  spec_json: Record<string, unknown> | null;
  spec_hash: string | null;
  holdout_exposure_index: number | null;
  holdout_exposure_count: number | null;
  created_by: string;
  created_at: string;
};

export type EvaluationExperimentDetail = EvaluationExperiment & {
  variants: EvaluationExperimentVariant[];
};

export function listExperiments(input: AuthInput): Promise<EvaluationExperiment[]> {
  return get(input, "/experiments");
}

export function getExperiment(input: AuthInput, experimentId: string): Promise<EvaluationExperimentDetail> {
  return get(input, `/experiments/${encodeURIComponent(experimentId)}`);
}

export function createExperiment(
  input: AuthInput,
  payload: {
    name: string;
    description: string | null;
    dataset_version_id: string;
    split: string;
    purpose: string;
    repetitions: number;
  },
): Promise<EvaluationExperiment> {
  return post(input, "/experiments", payload);
}

export function addExperimentVariant(
  input: AuthInput,
  experimentId: string,
  payload: {
    label: string;
    agent_version_id: string;
    pricing_snapshot_id: string;
    ordinal: number;
    variant_metadata: Record<string, unknown>;
  },
): Promise<EvaluationExperimentVariant> {
  return post(input, `/experiments/${encodeURIComponent(experimentId)}/variants`, payload);
}

export function finalizeExperiment(input: AuthInput, experimentId: string): Promise<EvaluationExperimentDetail> {
  return post(input, `/experiments/${encodeURIComponent(experimentId)}/finalize`);
}

// ---------- Experiment runs ----------

export type EvaluationExperimentRun = {
  id: string;
  workspace_id: string;
  experiment_id: string;
  status: string;
  git_commit: string;
  dataset_version_id: string;
  dataset_hash: string;
  experiment_spec_hash: string;
  split: string;
  purpose: string;
  repetitions: number;
  started_at: string | null;
  completed_at: string | null;
  failure_code: string | null;
  safe_failure_message: string | null;
  holdout_exposure_index: number | null;
  created_by: string;
  created_at: string;
};

export type EvaluationExperimentRunProgress = {
  total: number;
  pending: number;
  running: number;
  completed: number;
  failed: number;
  cancelled: number;
  progress: number;
};

export function getExperimentRun(input: AuthInput, runId: string): Promise<EvaluationExperimentRun> {
  return get(input, `/experiment-runs/${encodeURIComponent(runId)}`);
}

export function cancelExperimentRun(input: AuthInput, runId: string): Promise<EvaluationExperimentRun> {
  return post(input, `/experiment-runs/${encodeURIComponent(runId)}/cancel`);
}

export function getExperimentRunProgress(
  input: AuthInput,
  runId: string,
): Promise<EvaluationExperimentRunProgress> {
  return get(input, `/experiment-runs/${encodeURIComponent(runId)}/progress`);
}

export function startExperimentRun(input: AuthInput, experimentId: string): Promise<EvaluationExperimentRun> {
  return post(input, `/experiments/${encodeURIComponent(experimentId)}/runs`);
}

// ---------- Metric snapshots ----------

export type MetricValueView = {
  name: string;
  status: string;
  value: number | string | null;
  sample_count: number;
  numerator: number | string | null;
  denominator: number | string | null;
  reason: string | null;
  evaluator_version: string;
  details: Record<string, unknown>;
};

export type MetricSnapshotPayload = {
  run_id: string;
  variants: Record<string, Record<string, unknown>>;
  overall: Record<string, unknown>;
  snapshot_id: string;
  evaluator_manifest_hash: string;
  case_result_set_hash: string;
  metrics_hash: string;
};

export function getExperimentRunMetrics(input: AuthInput, runId: string): Promise<MetricSnapshotPayload> {
  return get(input, `/experiment-runs/${encodeURIComponent(runId)}/metrics`);
}

export function materializeExperimentRunMetrics(input: AuthInput, runId: string): Promise<MetricSnapshotPayload> {
  return post(input, `/experiment-runs/${encodeURIComponent(runId)}/metrics`);
}

// ---------- Comparisons ----------

export type PairedMetricView = {
  baseline: MetricValueView;
  candidate: MetricValueView;
  absolute_delta: number | null;
  relative_delta: number | null;
  paired_win: number;
  paired_tie: number;
  paired_loss: number;
  applicable_pairs: number;
  missing_pairs: number;
  direction: string;
  status: string;
  reason: string | null;
};

export type EvaluationComparison = {
  id: string;
  workspace_id: string;
  experiment_run_id: string;
  baseline_variant_id: string;
  candidate_variant_id: string;
  metric_snapshot_id: string | null;
  metric_snapshot_hash: string | null;
  baseline_variant_hash: string | null;
  candidate_variant_hash: string | null;
  evaluator_manifest_hash: string | null;
  comparison_hash: string | null;
  status: string;
  evaluator_versions: Record<string, unknown>;
  metrics: Record<string, PairedMetricView>;
  missing_pairs: number;
  paired_pairs: number;
  created_by: string | null;
  created_at: string;
};

export function listExperimentComparisons(input: AuthInput, runId: string): Promise<EvaluationComparison[]> {
  return get(input, `/experiment-runs/${encodeURIComponent(runId)}/comparisons`);
}

export function getExperimentComparison(
  input: AuthInput,
  runId: string,
  comparisonId: string,
): Promise<EvaluationComparison> {
  return get(input, `/experiment-runs/${encodeURIComponent(runId)}/comparisons/${encodeURIComponent(comparisonId)}`);
}

export function createExperimentComparison(
  input: AuthInput,
  runId: string,
  payload: { baseline_variant_id: string; candidate_variant_id: string },
): Promise<EvaluationComparison> {
  return post(input, `/experiment-runs/${encodeURIComponent(runId)}/comparisons`, payload);
}

// ---------- Ablation ----------

export type EvaluationAblation = {
  id: string;
  workspace_id: string;
  comparison_id: string;
  experiment_run_id: string;
  baseline_variant_id: string;
  candidate_variant_id: string;
  factor: string;
  changed_paths: string[];
  baseline_factor_hash: string;
  candidate_factor_hash: string;
  analysis_hash: string;
  created_by: string;
  created_at: string;
};

export function getExperimentAblation(
  input: AuthInput,
  runId: string,
  comparisonId: string,
): Promise<EvaluationAblation> {
  return get(input, `/experiment-runs/${encodeURIComponent(runId)}/comparisons/${encodeURIComponent(comparisonId)}/ablation`);
}

export function createExperimentAblation(
  input: AuthInput,
  runId: string,
  comparisonId: string,
): Promise<EvaluationAblation> {
  return post(input, `/experiment-runs/${encodeURIComponent(runId)}/comparisons/${encodeURIComponent(comparisonId)}/ablation`);
}

// ---------- Release gate policies ----------

export type GateRule = {
  metric: string;
  rule: string;
  required: boolean;
  safety: boolean;
  tolerance?: number;
  threshold?: number;
  compensation_metric?: string;
  min_compensation_gain?: number;
};

export type ReleaseGatePolicy = {
  id: string;
  workspace_id: string;
  name: string;
  description: string | null;
  policy_json: { rules: GateRule[] };
  policy_hash: string;
  created_by: string;
  created_at: string;
};

export function listReleaseGatePolicies(input: AuthInput): Promise<ReleaseGatePolicy[]> {
  return get(input, "/release-gate-policies");
}

export function getReleaseGatePolicy(input: AuthInput, policyId: string): Promise<ReleaseGatePolicy> {
  return get(input, `/release-gate-policies/${encodeURIComponent(policyId)}`);
}

export function createReleaseGatePolicy(
  input: AuthInput,
  payload: { name: string; description: string | null; policy_json: { rules: GateRule[] } },
): Promise<ReleaseGatePolicy> {
  return post(input, "/release-gate-policies", payload);
}

// ---------- Release gate decisions ----------

export type GateRuleResult = {
  metric: string;
  rule: string;
  status: string;
  baseline?: number | null;
  candidate?: number | null;
  reason?: string | null;
  compensation?: {
    metric: string;
    status: string;
    baseline?: number | null;
    candidate?: number | null;
    gain?: number | null;
    reason?: string | null;
  } | null;
};

export type ReleaseGateDecision = {
  id: string;
  workspace_id: string;
  comparison_id: string;
  policy_id: string;
  status: string;
  rule_results: GateRuleResult[];
  reasons: string[];
  comparison_hash: string;
  policy_hash: string;
  decision_hash: string;
  created_by: string;
  created_at: string;
};

export function listReleaseGateDecisions(
  input: AuthInput,
  runId: string,
  comparisonId: string,
): Promise<ReleaseGateDecision[]> {
  return get(input, `/experiment-runs/${encodeURIComponent(runId)}/comparisons/${encodeURIComponent(comparisonId)}/release-gates`);
}

export function getReleaseGateDecision(
  input: AuthInput,
  runId: string,
  comparisonId: string,
  policyId: string,
): Promise<ReleaseGateDecision> {
  return get(
    input,
    `/experiment-runs/${encodeURIComponent(runId)}/comparisons/${encodeURIComponent(comparisonId)}/release-gates/${encodeURIComponent(policyId)}`,
  );
}

export function createReleaseGateDecision(
  input: AuthInput,
  runId: string,
  comparisonId: string,
  payload: { policy_id: string },
): Promise<ReleaseGateDecision> {
  return post(
    input,
    `/experiment-runs/${encodeURIComponent(runId)}/comparisons/${encodeURIComponent(comparisonId)}/release-gates`,
    payload,
  );
}

// ---------- Agent discovery (real APIs, used for variant selectors) ----------

export type AgentSummary = {
  id: string;
  workspace_id: string;
  name: string;
  description: string | null;
  created_at: string;
};

export type AgentVersionSummary = {
  id: string;
  workspace_id: string;
  agent_id: string;
  version_number: number;
  resolved_spec_hash: string;
  created_at: string;
};

export function listAgents(input: AuthInput): Promise<AgentSummary[]> {
  return apiRequest<AgentSummary[]>(
    `${BASE}/${encodeURIComponent(input.workspaceId)}/agents`,
    input.accessToken,
  );
}

export function listAgentVersions(input: AuthInput, agentId: string): Promise<AgentVersionSummary[]> {
  return apiRequest<AgentVersionSummary[]>(
    `${BASE}/${encodeURIComponent(input.workspaceId)}/agents/${encodeURIComponent(agentId)}/versions`,
    input.accessToken,
  );
}

// ---------- View helpers (safe narrowing of dynamic payloads) ----------

export type MetricGroup = { metrics: MetricValueView[]; categories: Record<string, MetricValueView[]> };

/** Narrow a variant/overall metric payload node into flat + category groups. */
export function narrowMetricGroup(node: unknown): MetricGroup {
  const group: MetricGroup = { metrics: [], categories: {} };
  if (typeof node !== "object" || node === null) return group;
  const record = node as Record<string, unknown>;
  for (const [key, value] of Object.entries(record)) {
    if (key === "categories" && typeof value === "object" && value !== null) {
      for (const [category, metrics] of Object.entries(value as Record<string, unknown>)) {
        group.categories[category] = narrowMetricList(metrics);
      }
      continue;
    }
    if (isMetricValue(value)) group.metrics.push(value);
  }
  return group;
}

function narrowMetricList(node: unknown): MetricValueView[] {
  if (typeof node !== "object" || node === null) return [];
  return Object.entries(node as Record<string, unknown>)
    .filter(([, value]) => isMetricValue(value))
    .map(([, value]) => value as MetricValueView);
}

function isMetricValue(value: unknown): value is MetricValueView {
  if (typeof value !== "object" || value === null) return false;
  const record = value as Record<string, unknown>;
  return typeof record.name === "string" && typeof record.status === "string";
}

/** Terminal experiment run statuses; polling stops on these. */
export const TERMINAL_RUN_STATUSES = new Set(["SUCCEEDED", "FAILED", "CANCELLED"]);
/** Statuses where the backend accepts a cancel request. */
export const CANCELLABLE_RUN_STATUSES = new Set(["QUEUED", "RUNNING"]);

/** Compare metrics whose paired status is COMPLETE, sorted by name. */
export function comparableMetricEntries(comparison: EvaluationComparison): Array<[string, PairedMetricView]> {
  return Object.entries(comparison.metrics ?? {}).sort(([a], [b]) => a.localeCompare(b));
}

/** Keep server-returned artifacts keyed by their durable identity. */
export function uniqueById<T extends { id: string }>(items: T[]): T[] {
  const seen = new Set<string>();
  return items.filter((item) => {
    if (seen.has(item.id)) return false;
    seen.add(item.id);
    return true;
  });
}

/** Idempotently add or replace an artifact returned by a create endpoint. */
export function upsertById<T extends { id: string }>(items: T[] | null, item: T): T[] {
  const current = uniqueById(items ?? []);
  const index = current.findIndex((existing) => existing.id === item.id);
  if (index < 0) return [...current, item];
  return current.map((existing, currentIndex) => (currentIndex === index ? item : existing));
}
