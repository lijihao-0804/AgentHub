import { apiRequest, type AuthInput } from "@/lib/api/client";

export const FEEDBACK_CATEGORIES = ["FACTUAL", "RETRIEVAL", "TOOL", "LATENCY", "EXPRESSION", "OTHER"] as const;
export type FeedbackCategory = typeof FEEDBACK_CATEGORIES[number];
export type Feedback = {
  id: string; run_id: string; turn_id: string | null; created_by: string;
  rating: number; category: FeedbackCategory; comment: string; corrected_answer: string | null;
  status: "PENDING" | "APPROVED" | "REJECTED"; review_version: number;
  reviewed_by: string | null; review_comment: string | null;
  imported_version_id: string | null; created_at: string;
};
function base(auth: AuthInput) { return `/api/v1/workspaces/${encodeURIComponent(auth.workspaceId)}`; }
export function listFeedback(auth: AuthInput, runId: string): Promise<Feedback[]> {
  return apiRequest(`${base(auth)}/runs/${encodeURIComponent(runId)}/feedback`, auth.accessToken);
}
export function submitFeedback(auth: AuthInput, runId: string, body: {
  client_key: string; turn_id?: string; rating: -1 | 1; category: FeedbackCategory;
  comment: string; corrected_answer: string | null;
}): Promise<Feedback> {
  return apiRequest(`${base(auth)}/runs/${encodeURIComponent(runId)}/feedback`, auth.accessToken, { method: "POST", body });
}
export function reviewFeedback(auth: AuthInput, feedback: Feedback, decision: "APPROVED" | "REJECTED"): Promise<Feedback> {
  return apiRequest(`${base(auth)}/feedback/${encodeURIComponent(feedback.id)}/review`, auth.accessToken,
    { method: "POST", body: { expected_version: feedback.review_version, decision } });
}
export function importFeedback(auth: AuthInput, feedback: Feedback, datasetId: string, baseVersionId: string): Promise<{ id: string; dataset_id: string }> {
  return apiRequest(`${base(auth)}/feedback/${encodeURIComponent(feedback.id)}/import-dev`, auth.accessToken,
    { method: "POST", body: { dataset_id: datasetId, base_version_id: baseVersionId, expected_review_version: feedback.review_version } });
}
