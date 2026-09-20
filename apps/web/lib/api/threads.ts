import { apiRequest, type AuthInput } from "@/lib/api/client";
import { listItems } from "@/lib/api/tenancy";

/**
 * Thread and turn client for the research workbench.
 *
 * A thread owns the conversation; every turn it holds is answered by one
 * agent run, and `agent_run_id` is what keeps the chat view attached to the
 * engineering view instead of replacing it.
 */

function workspaceBase(workspaceId: string): string {
  return `/api/v1/workspaces/${encodeURIComponent(workspaceId)}`;
}

export type Thread = {
  id: string;
  workspace_id: string;
  agent_id: string;
  title: string;
  created_by: string;
  created_at: string;
  updated_at: string;
};

export type ThreadTurn = {
  id: string;
  thread_id: string;
  sequence: number;
  user_input: string;
  /** Null only while the turn has not been attached to its run yet. */
  agent_run_id: string | null;
  created_at: string;
  status: string | null;
  final_output: string | null;
  failure_code: string | null;
};

export type ThreadTurnSubmitResult = {
  turn: Omit<ThreadTurn, "status" | "final_output" | "failure_code">;
  run_id: string;
  agent_version_id: string;
  /** True when a repeated client_token returned the existing turn. */
  reused: boolean;
};

export function createThread(
  input: AuthInput,
  agentId: string,
  body: { title: string },
): Promise<Thread> {
  return apiRequest<Thread>(
    `${workspaceBase(input.workspaceId)}/agents/${encodeURIComponent(agentId)}/threads`,
    input.accessToken,
    { method: "POST", body },
  );
}

export async function listThreads(
  input: AuthInput,
  query?: { agentId?: string; limit?: number; offset?: number },
): Promise<Thread[]> {
  const params = new URLSearchParams();
  if (query?.agentId?.trim()) params.set("agent_id", query.agentId.trim());
  if (query?.limit !== undefined) params.set("limit", String(query.limit));
  if (query?.offset !== undefined) params.set("offset", String(query.offset));
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const payload = await apiRequest<unknown>(
    `${workspaceBase(input.workspaceId)}/threads${suffix}`,
    input.accessToken,
  );
  return listItems<Thread>(payload);
}

export function getThread(input: AuthInput, threadId: string): Promise<Thread> {
  return apiRequest<Thread>(
    `${workspaceBase(input.workspaceId)}/threads/${encodeURIComponent(threadId)}`,
    input.accessToken,
  );
}

/** Only the title is mutable; a thread never changes the agent it belongs to. */
export function patchThread(
  input: AuthInput,
  threadId: string,
  body: { title: string },
): Promise<Thread> {
  return apiRequest<Thread>(
    `${workspaceBase(input.workspaceId)}/threads/${encodeURIComponent(threadId)}`,
    input.accessToken,
    { method: "PATCH", body },
  );
}

export async function deleteThread(input: AuthInput, threadId: string): Promise<void> {
  await apiRequest<unknown>(
    `${workspaceBase(input.workspaceId)}/threads/${encodeURIComponent(threadId)}`,
    input.accessToken,
    { method: "DELETE" },
  );
}

export async function listThreadTurns(input: AuthInput, threadId: string): Promise<ThreadTurn[]> {
  const payload = await apiRequest<unknown>(
    `${workspaceBase(input.workspaceId)}/threads/${encodeURIComponent(threadId)}/turns`,
    input.accessToken,
  );
  return listItems<ThreadTurn>(payload);
}

/**
 * Submits one turn and waits for the run that answers it.
 *
 * `client_token` is the only thing standing between a retried submission and
 * two runs for one question, so the caller mints one per submission and
 * reuses it across retries.
 */
export function submitThreadTurn(
  input: AuthInput,
  threadId: string,
  body: { input_text: string; client_token?: string },
): Promise<ThreadTurnSubmitResult> {
  return apiRequest<ThreadTurnSubmitResult>(
    `${workspaceBase(input.workspaceId)}/threads/${encodeURIComponent(threadId)}/turns`,
    input.accessToken,
    { method: "POST", body },
  );
}
