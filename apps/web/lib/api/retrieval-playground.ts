import { apiRequest, ApiError, type AuthInput } from "@/lib/api/client";

type CitedBlock = { chunk_id: string; locator: Record<string, unknown>; excerpt: string };
export type SnapshotAnswer = {
  answer: string;
  insufficient_evidence: boolean;
  citations: (CitedBlock & {
    id: number; source: string; document_id: string; document_revision_id: string;
    components?: CitedBlock[];
  })[];
};

export function askSnapshot(auth: AuthInput, kb: string, snapshot: string, profile: string, query: string) {
  return apiRequest<SnapshotAnswer>("/api/v1/knowledge/query", auth.accessToken, {
    method: "POST",
    body: {
      workspace_id: auth.workspaceId, knowledge_base_id: kb, knowledge_snapshot_id: snapshot,
      model_profile_id: profile, query, output_mode: "PROMPT_JSON",
    },
  });
}

export class RetrievalPlaygroundError extends Error {
  code: string;

  constructor(code: string, message: string) {
    super(message);
    this.name = "RetrievalPlaygroundError";
    this.code = code;
  }
}

export type PlaygroundStageResult = {
  chunk_id: string;
  rank: number;
  score: number;
  document_revision_id: string | null;
  locator: Record<string, unknown> | null;
};

export type PlaygroundStage = {
  latency_ms: number;
  results: PlaygroundStageResult[];
};

export type RetrievalPlaygroundResponse = {
  snapshot_id: string;
  evidence: Array<{
    chunk_id: string;
    document_id: string;
    document_revision_id: string;
    source: string;
    locator: Record<string, unknown>;
    retrieval_score: number;
    rerank_score: number | null;
    snippet: string;
  }>;
  stages: {
    dense: PlaygroundStage;
    sparse: PlaygroundStage;
    fused: PlaygroundStage;
    rerank: PlaygroundStage;
  };
  total_latency_ms: number;
};

export async function runRetrievalPlayground(input: {
  workspaceId: string;
  knowledgeBaseId: string;
  accessToken: string;
  query: string;
  snapshotId: string;
  denseTopK: number;
  sparseTopK: number;
  candidateTopK: number;
  finalTopK: number;
}): Promise<RetrievalPlaygroundResponse> {
  try {
    return await apiRequest<RetrievalPlaygroundResponse>(
      `/api/v1/workspaces/${encodeURIComponent(input.workspaceId)}/knowledge-bases/${encodeURIComponent(input.knowledgeBaseId)}/retrieval/playground`,
      input.accessToken,
      {
        method: "POST",
        body: {
          query: input.query,
          knowledge_snapshot_id: input.snapshotId,
          dense_top_k: input.denseTopK,
          sparse_top_k: input.sparseTopK,
          candidate_top_k: input.candidateTopK,
          final_top_k: input.finalTopK,
        },
      },
    );
  } catch (error) {
    if (error instanceof ApiError) {
      throw new RetrievalPlaygroundError(error.code, error.message);
    }
    throw new RetrievalPlaygroundError("REQUEST_FAILED", "Retrieval failed.");
  }
}
