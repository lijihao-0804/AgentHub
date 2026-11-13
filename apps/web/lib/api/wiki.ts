import { apiRequest, type AuthInput } from "@/lib/api/client";

export type WikiCompilationStatus =
  | "PENDING"
  | "PROCESSING"
  | "SUCCEEDED"
  | "FAILED"
  | "PUBLISHED";

export type WikiCompilation = {
  id: string;
  source_snapshot_id: string | null;
  source_snapshot_hash: string | null;
  identity_hash: string | null;
  compiler_spec: Record<string, unknown>;
  status: WikiCompilationStatus;
  attempt_count: number;
  error_code: string | null;
  content_hash: string | null;
  telemetry: Record<string, unknown> | null;
  created_at: string;
  published_at: string | null;
  published_by: string | null;
};

export type WikiPage = {
  id: string;
  compilation_id: string;
  slug: string;
  title: string;
  body: string;
  created_at?: string;
};

export type WikiLintIssue = {
  kind: string;
  page_slug?: string;
  message: string;
  severity: "error" | "warning";
};

export type WikiLintReport = {
  compilation_id: string;
  status: string;
  total_claims: number;
  total_conflicts: number;
  ungrounded_claims: number;
  circular_dependencies: number;
  issues: WikiLintIssue[];
};

export type WikiQueryResult = {
  binding: {
    knowledge_base_id: string;
    wiki_publication_id: string;
    publication_hash: string;
    source_snapshot_id: string;
  };
  route: string;
  answer: string;
  insufficient_evidence: boolean;
  supplemental_required: boolean;
  conflict_count: number;
  degraded: boolean;
  degraded_reason: string | null;
  delivered_origins: string[];
  results: Array<Record<string, unknown>>;
  evidence_refs: Array<Record<string, unknown>>;
};

function wikiBase(workspaceId: string, knowledgeBaseId: string): string {
  return `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/wiki`;
}

export async function listCompilations(
  input: AuthInput,
  knowledgeBaseId: string,
): Promise<WikiCompilation[]> {
  const payload = await apiRequest<WikiCompilation[]>(
    `${wikiBase(input.workspaceId, knowledgeBaseId)}/compilations`,
    input.accessToken,
  );
  return Array.isArray(payload) ? payload : [];
}

export async function getCompilation(
  input: AuthInput,
  knowledgeBaseId: string,
  compilationId: string,
): Promise<WikiCompilation> {
  return apiRequest<WikiCompilation>(
    `${wikiBase(input.workspaceId, knowledgeBaseId)}/compilations/${encodeURIComponent(compilationId)}`,
    input.accessToken,
  );
}

export async function createCompilation(
  input: AuthInput,
  knowledgeBaseId: string,
  payload: {
    model_profile_id: string;
    source_snapshot_id?: string;
    review_model_profile_id?: string;
    review_mode?: "BASIC" | "MODEL";
    output_mode?: "PROMPT_JSON" | "JSON_SCHEMA";
  },
): Promise<WikiCompilation> {
  return apiRequest<WikiCompilation>(
    `${wikiBase(input.workspaceId, knowledgeBaseId)}/compilations`,
    input.accessToken,
    {
      method: "POST",
      body: JSON.stringify({
        model_profile_id: payload.model_profile_id,
        source_snapshot_id: payload.source_snapshot_id || "LATEST",
        review_model_profile_id: payload.review_model_profile_id,
        review_mode: payload.review_mode,
        output_mode: payload.output_mode || "PROMPT_JSON",
      }),
    },
  );
}

export async function getCompilationPages(
  input: AuthInput,
  knowledgeBaseId: string,
  compilationId: string,
): Promise<WikiPage[]> {
  const payload = await apiRequest<WikiPage[]>(
    `${wikiBase(input.workspaceId, knowledgeBaseId)}/compilations/${encodeURIComponent(compilationId)}/pages`,
    input.accessToken,
  );
  return Array.isArray(payload) ? payload : [];
}

export async function getCompilationLint(
  input: AuthInput,
  knowledgeBaseId: string,
  compilationId: string,
): Promise<WikiLintReport> {
  return apiRequest<WikiLintReport>(
    `${wikiBase(input.workspaceId, knowledgeBaseId)}/compilations/${encodeURIComponent(compilationId)}/lint`,
    input.accessToken,
  );
}

export async function publishCompilation(
  input: AuthInput,
  knowledgeBaseId: string,
  compilationId: string,
): Promise<WikiCompilation> {
  return apiRequest<WikiCompilation>(
    `${wikiBase(input.workspaceId, knowledgeBaseId)}/compilations/${encodeURIComponent(compilationId)}/publish`,
    input.accessToken,
    { method: "POST" },
  );
}

export async function retryCompilation(
  input: AuthInput,
  knowledgeBaseId: string,
  compilationId: string,
): Promise<WikiCompilation> {
  return apiRequest<WikiCompilation>(
    `${wikiBase(input.workspaceId, knowledgeBaseId)}/compilations/${encodeURIComponent(compilationId)}/retry`,
    input.accessToken,
    { method: "POST" },
  );
}

export async function queryWikiCompilation(
  input: AuthInput,
  knowledgeBaseId: string,
  compilationId: string,
  payload: {
    query: string;
    source_snapshot_id: string;
    mode?: "AUTO" | "WIKI" | "RAG" | "HYBRID";
    intent?: "overview" | "fact" | "synthesis";
  },
): Promise<WikiQueryResult> {
  return apiRequest<WikiQueryResult>(
    `${wikiBase(input.workspaceId, knowledgeBaseId)}/compilations/${encodeURIComponent(compilationId)}/query`,
    input.accessToken,
    {
      method: "POST",
      body: JSON.stringify({
        query: payload.query,
        source_snapshot_id: payload.source_snapshot_id,
        mode: payload.mode || "AUTO",
        intent: payload.intent || "overview",
      }),
    },
  );
}
