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

export class ApprovalApiError extends Error {
  code: string;

  constructor(code: string, message: string) {
    super(message);
    this.name = "ApprovalApiError";
    this.code = code;
  }
}

const apiBaseUrl = (process.env.NEXT_PUBLIC_API_BASE_URL?.trim() ?? "").replace(/\/$/, "");

async function request<T>(path: string, accessToken: string, init?: RequestInit): Promise<T> {
  const token = accessToken.trim();
  if (!token) {
    throw new ApprovalApiError("AUTHENTICATION_REQUIRED", "An access token is required.");
  }
  const response = await fetch(`${apiBaseUrl}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
      ...(init?.headers ?? {}),
    },
    credentials: "include",
  });
  const body = (await response.json().catch(() => null)) as unknown;
  if (!response.ok) {
    const error = isRecord(body) && isRecord(body.error) ? body.error : {};
    throw new ApprovalApiError(
      typeof error.code === "string" ? error.code : "REQUEST_FAILED",
      typeof error.message === "string" ? error.message : "Approval request failed.",
    );
  }
  return body as T;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

export function listApprovals(workspaceId: string, accessToken: string): Promise<Approval[]> {
  return request<Approval[]>(
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
  return request<ApprovalDecision>(
    `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/approvals/${encodeURIComponent(approvalId)}/${decision}`,
    accessToken,
    { method: "POST", body: "{}" },
  );
}
