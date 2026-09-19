import { apiRequest, ApiError } from "./api-client";

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

export function listApprovals(workspaceId: string, accessToken: string): Promise<Approval[]> {
  return apiRequest<Approval[]>(
    `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/approvals`,
    accessToken,
  );
}

export function decideApproval(
  workspaceId: string,
  approvalId: string,
  decision: "approve" | "deny",
  accessToken: string,
): Promise<ApprovalDecision> {
  return apiRequest<ApprovalDecision>(
    `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/approvals/${encodeURIComponent(approvalId)}/${decision}`,
    accessToken,
    { method: "POST", body: {} },
  );
}
