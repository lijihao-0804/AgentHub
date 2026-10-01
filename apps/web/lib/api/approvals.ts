import { apiRequest, ApiError } from "@/lib/api/client";

export { ApiError as ApprovalApiError };

export type Approval = {
  id: string;
  workspace_id: string;
  run_id: string;
  agent_version_id: string;
  logical_action_id: string;
  tool_revision_id: string | null;
  tool_identity: string;
  canonical_arguments: Record<string, unknown>;
  canonical_args_hash: string;
  decision_status: string;
  execution_status: string;
  requested_by: string;
  decided_by: string | null;
  decided_at: string | null;
  claimed_at: string | null;
  executed_at: string | null;
  expires_at: string | null;
  failure_code: string | null;
  safe_failure_message: string | null;
  safe_result: Record<string, unknown> | null;
  execution_attempt_count: number;
  created_at: string;
  updated_at: string;
};

export type ApprovalDecision = {
  approval: Approval;
  run_id: string;
  run_status: string;
};

export type ApprovalDecisionTab = "PENDING" | "DECIDED";

export type ApprovalList = {
  items: Approval[];
  total: number;
};

/**
 * Paged approval inbox. `decision` narrows to a tab (PENDING = undecided,
 * DECIDED = every terminal decision); without it the endpoint returns the
 * whole workspace history. Newest first.
 */
export function listApprovals(
  workspaceId: string,
  accessToken: string,
  options: { decision?: ApprovalDecisionTab; limit?: number; offset?: number } = {},
): Promise<ApprovalList> {
  const params = new URLSearchParams();
  if (options.decision) params.set("decision", options.decision);
  if (options.limit !== undefined) params.set("limit", String(options.limit));
  if (options.offset !== undefined) params.set("offset", String(options.offset));
  const query = params.toString();
  return apiRequest<ApprovalList>(
    `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/approvals${query ? `?${query}` : ""}`,
    accessToken,
  );
}

/**
 * Run-scoped approvals from the runtime API. The backend already orders
 * them by (created_at, id), so the newest PENDING entry is the action the
 * run is currently waiting on. Same Approval contract as the inbox.
 */
export function listRunApprovals(
  workspaceId: string,
  runId: string,
  accessToken: string,
): Promise<Approval[]> {
  return apiRequest<Approval[]>(
    `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/agent-runs/${encodeURIComponent(runId)}/approvals`,
    accessToken,
  );
}

export function decideApproval(
  workspaceId: string,
  approvalId: string,
  decision: "approve" | "deny",
  accessToken: string,
  options: { reason?: string } = {},
): Promise<ApprovalDecision> {
  // The reason rides in the body and lands in the decision's audit entry;
  // the backend treats it as optional so older callers keep working.
  const body =
    decision === "deny" && options.reason ? { reason: options.reason } : {};
  return apiRequest<ApprovalDecision>(
    `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/approvals/${encodeURIComponent(approvalId)}/${decision}`,
    accessToken,
    { method: "POST", body },
  );
}
