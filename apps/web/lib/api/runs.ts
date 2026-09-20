export type ApprovalSummary = {
  total: number;
  pending: number;
  approved: number;
  denied: number;
  expired: number;
  cancelled: number;
  not_started: number;
  claimed: number;
  succeeded: number;
  failed: number;
  unknown_outcome: number;
};

export type RunListItem = {
  id: string;
  trace_id: string;
  workspace_id: string;
  agent_version_id: string;
  agent_version_number: number;
  resolved_spec_hash: string | null;
  status: string;
  failure_code: string | null;
  failure_category: string | null;
  created_at: string;
  started_at: string;
  completed_at: string | null;
  duration_ms: number | null;
  model_step_count: number;
  tool_call_count: number;
  total_input_tokens: number | null;
  total_output_tokens: number | null;
  total_tokens: number | null;
  total_cached_tokens: number | null;
  total_cost_amount: number | string | null;
  cost_currency: string | null;
  cost_is_estimate: boolean | null;
  approval_summary: ApprovalSummary;
};

export type RunDetail = RunListItem & {
  effective_knowledge_snapshots: Array<Record<string, unknown>>;
  trace_url?: string | null;
};

export type RunTimelineEntry = {
  sequence: number;
  occurred_at: string;
  kind: string;
  status: string;
  duration_ms: number | null;
  metadata: Record<string, unknown>;
  failure_code: string | null;
  failure_category: string | null;
};

export type RunListResponse = {
  items: RunListItem[];
  next_cursor: string | null;
};

export type RunTimelineResponse = {
  run_id: string;
  workspace_id: string;
  items: RunTimelineEntry[];
};

import { apiRequest, ApiError } from "@/lib/api/client";

export { ApiError as RunsApiError };

export function listRuns(input: {
  workspaceId: string;
  accessToken: string;
  status?: string;
  agentVersionId?: string;
  limit?: number;
  cursor?: string | null;
}): Promise<RunListResponse> {
  const query = new URLSearchParams();
  if (input.status?.trim()) query.set("status", input.status.trim());
  if (input.agentVersionId?.trim()) query.set("agent_version_id", input.agentVersionId.trim());
  if (input.limit) query.set("limit", String(input.limit));
  if (input.cursor) query.set("cursor", input.cursor);
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return apiRequest<RunListResponse>(
    `/api/v1/workspaces/${encodeURIComponent(input.workspaceId)}/runs${suffix}`,
    input.accessToken,
  );
}

export function getRunDetail(
  workspaceId: string,
  runId: string,
  accessToken: string,
): Promise<RunDetail> {
  return apiRequest<RunDetail>(
    `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/runs/${encodeURIComponent(runId)}`,
    accessToken,
  );
}

export function getRunTimeline(
  workspaceId: string,
  runId: string,
  accessToken: string,
): Promise<RunTimelineResponse> {
  return apiRequest<RunTimelineResponse>(
    `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/runs/${encodeURIComponent(runId)}/steps`,
    accessToken,
  );
}
