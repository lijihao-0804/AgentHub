import { ApiError, apiRequest, type AuthInput } from "@/lib/api/client";
import { listEnvelope } from "@/lib/api/tenancy";

export type WikiCompilationStatus =
  | "PENDING"
  | "PROCESSING"
  | "READY"
  | "FAILED"
  | "PUBLISHED";

export type WikiCompilation = {
  id: string;
  source_snapshot_id: string;
  source_snapshot_hash: string;
  identity_hash: string;
  compiler_spec: Record<string, unknown>;
  status: WikiCompilationStatus;
  attempt_count: number;
  error_code: string | null;
  content_hash: string | null;
  telemetry: Record<string, unknown>;
  created_at: string;
  published_at: string | null;
  published_by: string | null;
};

export type WikiClaim = {
  page_key: string;
  topic_key?: string;
  quote: string;
  chunk_id: string;
  document_id: string;
  document_revision_id: string;
  locator: Record<string, unknown>;
  predicate?: unknown;
  value?: unknown;
  condition?: unknown;
  conflict?: boolean;
  conflict_peers?: string[];
  conflict_origin?: string;
  origin?: string;
  publication_id?: string;
  publication_hash?: string;
  wiki_page_id?: string;
};

export type WikiRelation = string | { target?: string; [key: string]: unknown };

export type WikiPage = {
  page_key: string;
  title: string;
  kind: string;
  markdown: string;
  claims: WikiClaim[];
  relations: WikiRelation[];
};

export type WikiLintIssue = {
  code: string;
  severity: "error" | "warning";
  page_key?: string;
};

export type WikiEvidence = {
  origin?: string;
  source?: string;
  quote: string;
  chunk_id: string;
  document_id: string;
  document_revision_id: string;
  locator: Record<string, unknown>;
  page_key?: string;
  topic_key?: string;
  predicate?: unknown;
  value?: unknown;
  condition?: unknown;
  conflict?: boolean;
  conflict_peers?: string[];
  retrieval_score?: number | null;
  rerank_score?: number | null;
  publication_id?: string;
  publication_hash?: string;
  wiki_page_id?: string;
};

export type WikiQueryResult = {
  binding: {
    schema_version: number;
    workspace_id: string;
    knowledge_base_id: string;
    compilation_id: string;
    compilation_hash: string | null;
    excluded_revision_ids: string[];
    source_snapshot_id: string;
    source_snapshot_hash: string;
    compilation_status: string;
    require_current: boolean;
    validated: boolean;
  };
  wiki_pages: WikiPage[];
  evidence: WikiEvidence[];
  route: string;
  intent: string;
  insufficient_evidence: boolean;
  supplemental_required: boolean;
  conflicts: WikiEvidence[];
  answer: string;
  generated_answer?: {
    answer: string;
    citations: Array<Record<string, unknown>>;
    insufficient_evidence: boolean;
    telemetry: Record<string, unknown>;
  };
};

export type WikiCompileInput = {
  model_profile_id: string;
  review_model_profile_id?: string;
  review_mode?: "BASIC" | "MODEL";
  manual_conflict_groups?: Array<Array<{ chunk_id: string; quote: string }>>;
  source_snapshot_id?: string | "LATEST";
  output_mode?: "PROMPT_JSON" | "JSON_SCHEMA";
};

export type WikiQueryInput = {
  source_snapshot_id: string;
  query: string;
  mode?: "AUTO" | "WIKI" | "RAG" | "HYBRID";
  intent?: "overview" | "fact" | "synthesis";
  require_current?: boolean;
  model_profile_id?: string;
};

function wikiBase(workspaceId: string, knowledgeBaseId: string): string {
  return `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/wiki`;
}

function compilationPath(
  input: AuthInput,
  knowledgeBaseId: string,
  compilationId?: string,
): string {
  const base = `${wikiBase(input.workspaceId, knowledgeBaseId)}/compilations`;
  return compilationId ? `${base}/${encodeURIComponent(compilationId)}` : base;
}

function expectList<T>(payload: unknown, surface: string): T[] {
  const rows = listEnvelope<T>(payload);
  if (rows === null) {
    throw new ApiError(
      "INVALID_RESPONSE",
      `The server returned an invalid ${surface} list.`,
      502,
    );
  }
  return rows;
}

export async function listCompilations(
  input: AuthInput,
  knowledgeBaseId: string,
): Promise<WikiCompilation[]> {
  const payload = await apiRequest<unknown>(
    compilationPath(input, knowledgeBaseId),
    input.accessToken,
  );
  return expectList<WikiCompilation>(payload, "Wiki compilation");
}

export function getCompilation(
  input: AuthInput,
  knowledgeBaseId: string,
  compilationId: string,
): Promise<WikiCompilation> {
  return apiRequest<WikiCompilation>(
    compilationPath(input, knowledgeBaseId, compilationId),
    input.accessToken,
  );
}

export function createCompilation(
  input: AuthInput,
  knowledgeBaseId: string,
  body: WikiCompileInput,
): Promise<WikiCompilation> {
  return apiRequest<WikiCompilation>(
    compilationPath(input, knowledgeBaseId),
    input.accessToken,
    {
      method: "POST",
      body: {
        model_profile_id: body.model_profile_id,
        source_snapshot_id: body.source_snapshot_id ?? "LATEST",
        output_mode: body.output_mode ?? "PROMPT_JSON",
        ...(body.review_model_profile_id
          ? { review_model_profile_id: body.review_model_profile_id }
          : {}),
        ...(body.review_mode ? { review_mode: body.review_mode } : {}),
        ...(body.manual_conflict_groups
          ? { manual_conflict_groups: body.manual_conflict_groups }
          : {}),
      },
    },
  );
}

export async function getCompilationPages(
  input: AuthInput,
  knowledgeBaseId: string,
  compilationId: string,
): Promise<WikiPage[]> {
  const payload = await apiRequest<unknown>(
    `${compilationPath(input, knowledgeBaseId, compilationId)}/pages`,
    input.accessToken,
  );
  return expectList<WikiPage>(payload, "Wiki page");
}

export async function getCompilationLint(
  input: AuthInput,
  knowledgeBaseId: string,
  compilationId: string,
): Promise<WikiLintIssue[]> {
  const payload = await apiRequest<unknown>(
    `${compilationPath(input, knowledgeBaseId, compilationId)}/lint`,
    input.accessToken,
  );
  return expectList<WikiLintIssue>(payload, "Wiki lint");
}

export function publishCompilation(
  input: AuthInput,
  knowledgeBaseId: string,
  compilationId: string,
): Promise<WikiCompilation> {
  return apiRequest<WikiCompilation>(
    `${compilationPath(input, knowledgeBaseId, compilationId)}/publish`,
    input.accessToken,
    { method: "POST", body: {} },
  );
}

export function retryCompilation(
  input: AuthInput,
  knowledgeBaseId: string,
  compilationId: string,
): Promise<WikiCompilation> {
  return apiRequest<WikiCompilation>(
    `${compilationPath(input, knowledgeBaseId, compilationId)}/retry`,
    input.accessToken,
    { method: "POST", body: {} },
  );
}

export function queryWikiCompilation(
  input: AuthInput,
  knowledgeBaseId: string,
  compilationId: string,
  body: WikiQueryInput,
): Promise<WikiQueryResult> {
  return apiRequest<WikiQueryResult>(
    `${compilationPath(input, knowledgeBaseId, compilationId)}/query`,
    input.accessToken,
    {
      method: "POST",
      body: {
        source_snapshot_id: body.source_snapshot_id,
        query: body.query,
        mode: body.mode ?? "AUTO",
        intent: body.intent ?? "overview",
        require_current: body.require_current ?? true,
        ...(body.model_profile_id ? { model_profile_id: body.model_profile_id } : {}),
      },
    },
  );
}
