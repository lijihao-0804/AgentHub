import { apiRequest, isRecord, type AuthInput } from "@/lib/api/client";
import { listItems } from "@/lib/api/tenancy";

/**
 * Artifact client for the research workbench.
 *
 * The table is generic, the contents are not: v1 carries exactly the two
 * research types below, and their shape is the same contract the backend
 * validator enforces. Content is still read defensively — an artifact whose
 * body does not match is rendered as empty rather than as a guess.
 */

function workspaceBase(workspaceId: string): string {
  return `/api/v1/workspaces/${encodeURIComponent(workspaceId)}`;
}

export const PAPER_SEARCH = "research.paper_search";
export const PAPER_SHORTLIST = "research.paper_shortlist";

export type ArtifactType = typeof PAPER_SEARCH | typeof PAPER_SHORTLIST;

/** Where one paper came from. Absent on a paper the reader saved by hand. */
export type PaperProvenance = {
  run_id: string;
  tool_call_id: string;
  tool_identity: string;
  step_sequence: number | null;
};

export type PaperRecord = {
  paper_id: string;
  title: string;
  authors: string[];
  year: number | null;
  venue: string | null;
  doi: string | null;
  abstract: string | null;
  url: string | null;
  citation_count: number | null;
  provenance?: PaperProvenance;
};

export type PaperSearchContent = {
  query: string;
  source: string;
  filters: Record<string, unknown>;
  total: number | null;
  papers: PaperRecord[];
};

export type PaperShortlistContent = {
  note: string | null;
  papers: PaperRecord[];
};

export type Artifact = {
  id: string;
  workspace_id: string;
  thread_id: string;
  /** Set means the artifact is the record of one run and cannot be edited. */
  run_id: string | null;
  type: string;
  title: string;
  content: Record<string, unknown>;
  created_by: string;
  created_at: string;
  updated_at: string;
};

// ---------- Content readers ----------

function text(value: unknown): string | null {
  return typeof value === "string" && value.trim() !== "" ? value : null;
}

function integer(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function provenance(value: unknown): PaperProvenance | undefined {
  if (!isRecord(value)) return undefined;
  const runId = text(value.run_id);
  const toolCallId = text(value.tool_call_id);
  const toolIdentity = text(value.tool_identity);
  if (!runId || !toolCallId || !toolIdentity) return undefined;
  return {
    run_id: runId,
    tool_call_id: toolCallId,
    tool_identity: toolIdentity,
    step_sequence: integer(value.step_sequence),
  };
}

export function readPaper(value: unknown): PaperRecord | null {
  if (!isRecord(value)) return null;
  const paperId = text(value.paper_id);
  const title = text(value.title);
  if (!paperId || !title) return null;
  const rawAuthors = Array.isArray(value.authors) ? value.authors : [];
  const record: PaperRecord = {
    paper_id: paperId,
    title,
    authors: rawAuthors.filter((author): author is string => typeof author === "string"),
    year: integer(value.year),
    venue: text(value.venue),
    doi: text(value.doi),
    abstract: text(value.abstract),
    url: text(value.url),
    citation_count: integer(value.citation_count),
  };
  const source = provenance(value.provenance);
  if (source) record.provenance = source;
  return record;
}

export function readPapers(value: unknown): PaperRecord[] {
  if (!Array.isArray(value)) return [];
  return value
    .map(readPaper)
    .filter((paper): paper is PaperRecord => paper !== null);
}

export function readPaperSearchContent(content: Record<string, unknown>): PaperSearchContent {
  return {
    query: text(content.query) ?? "",
    source: text(content.source) ?? "",
    filters: isRecord(content.filters) ? content.filters : {},
    total: integer(content.total),
    papers: readPapers(content.papers),
  };
}

export function readPaperShortlistContent(content: Record<string, unknown>): PaperShortlistContent {
  return { note: text(content.note), papers: readPapers(content.papers) };
}

/** Every paper an artifact holds, whatever of the two types it is. */
export function artifactPapers(artifact: Artifact): PaperRecord[] {
  return readPapers(artifact.content.papers);
}

/**
 * The wire form of a paper. Undefined optional fields are sent as null so a
 * saved copy keeps exactly the fields the source had, and the backend's
 * validator sees the shape it expects.
 */
export function paperPayload(paper: PaperRecord): Record<string, unknown> {
  return {
    paper_id: paper.paper_id,
    title: paper.title,
    authors: paper.authors,
    year: paper.year,
    venue: paper.venue,
    doi: paper.doi,
    abstract: paper.abstract,
    url: paper.url,
    citation_count: paper.citation_count,
    ...(paper.provenance ? { provenance: { ...paper.provenance } } : {}),
  };
}

// ---------- Requests ----------

export async function listThreadArtifacts(
  input: AuthInput,
  threadId: string,
  query?: { type?: string; limit?: number; offset?: number },
): Promise<Artifact[]> {
  const params = new URLSearchParams();
  if (query?.type?.trim()) params.set("type", query.type.trim());
  if (query?.limit !== undefined) params.set("limit", String(query.limit));
  if (query?.offset !== undefined) params.set("offset", String(query.offset));
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const payload = await apiRequest<unknown>(
    `${workspaceBase(input.workspaceId)}/threads/${encodeURIComponent(threadId)}/artifacts${suffix}`,
    input.accessToken,
  );
  return listItems<Artifact>(payload);
}

export function createThreadArtifact(
  input: AuthInput,
  threadId: string,
  body: { type: string; title: string; content: Record<string, unknown> },
): Promise<Artifact> {
  return apiRequest<Artifact>(
    `${workspaceBase(input.workspaceId)}/threads/${encodeURIComponent(threadId)}/artifacts`,
    input.accessToken,
    { method: "POST", body },
  );
}

export function getArtifact(input: AuthInput, artifactId: string): Promise<Artifact> {
  return apiRequest<Artifact>(
    `${workspaceBase(input.workspaceId)}/artifacts/${encodeURIComponent(artifactId)}`,
    input.accessToken,
  );
}

/**
 * An artifact never changes kind, so `type` is absent here. A run-produced
 * artifact is refused with ARTIFACT_NOT_EDITABLE (409); the UI does not offer
 * the action in the first place.
 */
export function patchArtifact(
  input: AuthInput,
  artifactId: string,
  body: { title?: string; content?: Record<string, unknown> },
): Promise<Artifact> {
  return apiRequest<Artifact>(
    `${workspaceBase(input.workspaceId)}/artifacts/${encodeURIComponent(artifactId)}`,
    input.accessToken,
    { method: "PATCH", body },
  );
}

export async function deleteArtifact(input: AuthInput, artifactId: string): Promise<void> {
  await apiRequest<unknown>(
    `${workspaceBase(input.workspaceId)}/artifacts/${encodeURIComponent(artifactId)}`,
    input.accessToken,
    { method: "DELETE" },
  );
}
