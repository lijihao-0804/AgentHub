export type RateMetric = { numerator: number; denominator: number; rate: number | null };

export type ObservabilitySummary = {
  window: { from: string; to: string };
  finished_runs: {
    succeeded: number;
    failed: number;
    cancelled: number;
    needs_attention: number;
    denominator: number;
  };
  success_rate: RateMetric;
  failure_rate: RateMetric;
  cancelled_rate: RateMetric;
  needs_attention_rate: RateMetric;
  latency: { p50_ms: number | null; p95_ms: number | null; avg_ms: number | null; sample_count: number };
  usage: {
    avg_tokens_per_run: number | null;
    avg_input_tokens: number | null;
    avg_output_tokens: number | null;
    avg_cached_tokens: number | null;
    total_tokens: number | null;
    known_usage_count: number;
    unknown_usage_count: number;
  };
  cost: {
    currency: string | null;
    total_cost: number | string | null;
    avg_cost_per_run: number | string | null;
    cost_per_successful_run: number | string | null;
    successful_cost_denominator: number;
    estimated_count: number;
    exact_count: number;
    mixed_currency: boolean;
    currencies: Array<{
      currency: string;
      total_cost: number | string | null;
      avg_cost_per_run: number | string | null;
      cost_per_successful_run: number | string | null;
      successful_cost_denominator: number;
      estimated_count: number;
      exact_count: number;
    }>;
  };
  current: {
    running_count: number;
    waiting_approval_count: number;
    cancel_requested_count: number;
    needs_attention_count: number;
    unknown_outcome_action_count: number;
  };
  approvals: {
    approval_total: number;
    pending: number;
    approved: number;
    denied: number;
    expired: number;
    cancelled: number;
    execution_not_started: number;
    claimed: number;
    succeeded: number;
    failed: number;
    unknown_outcome: number;
    wait_latency: { p50_ms: number | null; p95_ms: number | null; sample_count: number };
  };
};

export type FailureAnalytics = {
  window: { from: string; to: string };
  total_failure_runs: number;
  categories: Array<{
    failure_category: string;
    count: number;
    percentage: number | null;
    top_failure_codes: Array<{ failure_code: string; count: number }>;
  }>;
  items: Array<{
    run_id: string;
    agent_version_id: string;
    agent_version_number: number;
    status: string;
    failure_category: string;
    failure_code: string;
    started_at: string;
    completed_at: string | null;
    duration_ms: number | null;
    tool_call_count: number;
    approval_id: string | null;
    logical_action_id: string | null;
    tool_identity: string | null;
    execution_status: string | null;
    action_failure_code: string | null;
  }>;
};

export type TimeseriesResponse = {
  window: { from: string; to: string };
  bucket: string;
  items: Array<{
    bucket: string;
    runs: number;
    succeeded: number;
    failed: number;
    needs_attention: number;
    tokens: number | null;
    cost_by_currency: Array<{ currency: string; total_cost: number | string | null }>;
  }>;
};

export type AgentVersionBreakdown = {
  window: { from: string; to: string };
  items: Array<{
    agent_version_id: string;
    version_number: number;
    run_count: number;
    success_count: number;
    failed_count: number;
    needs_attention_count: number;
    p95_latency_ms: number | null;
    avg_tokens: number | null;
    cost_by_currency: Array<{ currency: string; total_cost: number | string | null }>;
  }>;
};

import { apiRequest, ApiError } from "@/lib/api/client";

export { ApiError as ObservabilityApiError };

function querySuffix(input: {
  from?: string;
  to?: string;
  bucket?: string;
  agentVersionId?: string;
  category?: string;
}): string {
  const query = new URLSearchParams();
  if (input.from) query.set("from", input.from);
  if (input.to) query.set("to", input.to);
  if (input.bucket) query.set("bucket", input.bucket);
  if (input.agentVersionId) query.set("agent_version_id", input.agentVersionId);
  if (input.category) query.set("category", input.category);
  const encoded = query.toString();
  return encoded ? `?${encoded}` : "";
}

type QueryInput = { workspaceId: string; accessToken: string; from?: string; to?: string; agentVersionId?: string };

export function getObservabilitySummary(input: QueryInput): Promise<ObservabilitySummary> {
  return apiRequest(
    `/api/v1/workspaces/${encodeURIComponent(input.workspaceId)}/observability/summary${querySuffix(input)}`,
    input.accessToken,
  );
}

export function getObservabilityTimeseries(
  input: QueryInput & { bucket?: string },
): Promise<TimeseriesResponse> {
  return apiRequest(
    `/api/v1/workspaces/${encodeURIComponent(input.workspaceId)}/observability/timeseries${querySuffix(input)}`,
    input.accessToken,
  );
}

export function getObservabilityFailures(
  input: QueryInput & { category?: string },
): Promise<FailureAnalytics> {
  return apiRequest(
    `/api/v1/workspaces/${encodeURIComponent(input.workspaceId)}/observability/failures${querySuffix(input)}`,
    input.accessToken,
  );
}

export function getAgentVersionBreakdown(input: QueryInput): Promise<AgentVersionBreakdown> {
  return apiRequest(
    `/api/v1/workspaces/${encodeURIComponent(input.workspaceId)}/observability/agent-versions${querySuffix(input)}`,
    input.accessToken,
  );
}
