import { apiRequest, ApiError } from "./api-client";

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
