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

export class RetrievalPlaygroundError extends Error {
  code: string;

  constructor(code: string, message: string) {
    super(message);
    this.name = "RetrievalPlaygroundError";
    this.code = code;
  }
}

const apiBaseUrl = (process.env.NEXT_PUBLIC_API_BASE_URL?.trim() ?? "").replace(/\/$/, "");

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

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
  const accessToken = input.accessToken.trim();
  if (!accessToken) {
    throw new RetrievalPlaygroundError(
      "AUTHENTICATION_REQUIRED",
      "An access token is required to run the playground.",
    );
  }
  const response = await fetch(
    `${apiBaseUrl}/api/v1/workspaces/${encodeURIComponent(input.workspaceId)}/knowledge-bases/${encodeURIComponent(input.knowledgeBaseId)}/retrieval/playground`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${accessToken}`,
      },
      credentials: "include",
      body: JSON.stringify({
        query: input.query,
        knowledge_snapshot_id: input.snapshotId,
        dense_top_k: input.denseTopK,
        sparse_top_k: input.sparseTopK,
        candidate_top_k: input.candidateTopK,
        final_top_k: input.finalTopK,
      }),
    },
  );

  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  if (!response.ok) {
    const error = isRecord(body) && isRecord(body.error) ? body.error : {};
    const code = typeof error.code === "string" ? error.code : "REQUEST_FAILED";
    const message = typeof error.message === "string" ? error.message : "Retrieval failed.";
    throw new RetrievalPlaygroundError(code, message);
  }
  return body as RetrievalPlaygroundResponse;
}
