import { apiRequest, isRecord, type AuthInput } from "@/lib/api/client";
import { listItems } from "@/lib/api/tenancy";

/**
 * Artifact client for the four applications.
 *
 * The table is generic, the contents are not: every type below has a backend
 * validator, and the readers here restate exactly that shape. Content is still
 * read defensively — an artifact whose body does not match is rendered as
 * empty rather than as a guess.
 *
 * The types split along one line that matters more than the application they
 * belong to. `paper_search`, `incident.timeline` and `analysis.query_result`
 * are recorded from tool results and carry provenance, so nothing in them was
 * written by a model. The rest are assembled by a person out of what the
 * thread already holds, which is why they may hold prose.
 */

function workspaceBase(workspaceId: string): string {
  return `/api/v1/workspaces/${encodeURIComponent(workspaceId)}`;
}

export const PAPER_SEARCH = "research.paper_search";
export const PAPER_SHORTLIST = "research.paper_shortlist";
export const INCIDENT_TIMELINE = "incident.timeline";
export const INCIDENT_REPORT = "incident.report";
export const ANALYSIS_QUERY_RESULT = "analysis.query_result";
export const ANALYSIS_FINDING = "analysis.finding";
export const SUPPORT_HANDOFF = "support.handoff";

export type ArtifactType =
  | typeof PAPER_SEARCH
  | typeof PAPER_SHORTLIST
  | typeof INCIDENT_TIMELINE
  | typeof INCIDENT_REPORT
  | typeof ANALYSIS_QUERY_RESULT
  | typeof ANALYSIS_FINDING
  | typeof SUPPORT_HANDOFF;

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

// ---------- Incident content ----------

export const TIMELINE_KINDS = ["METRIC", "LOG", "DEPLOYMENT", "COMMIT", "ACTION"] as const;

export type TimelineKind = (typeof TIMELINE_KINDS)[number];

/**
 * One line of an incident timeline.
 *
 * `at` stays the string the tool returned. The sources disagree about
 * precision and about whether they say Z or +00:00, and re-formatting here
 * would mean the browser deciding what a timestamp from an unfamiliar server
 * meant. Sorting is lexicographic for the same reason — ISO-8601 already sorts
 * correctly as text, and anything that does not is left where it was.
 */
export type TimelineEntry = {
  at: string;
  kind: TimelineKind;
  summary: string;
  detail: string | null;
  provenance?: PaperProvenance;
};

export type IncidentTimelineContent = {
  incident_ref: string | null;
  service: string | null;
  entries: TimelineEntry[];
};

export type IncidentReportContent = {
  incident_ref: string | null;
  severity: string | null;
  summary: string;
  impact: string | null;
  timeline: TimelineEntry[];
  root_cause: string | null;
  actions_taken: string[];
  remaining_risks: string[];
};

function timelineKind(value: unknown): TimelineKind | null {
  return TIMELINE_KINDS.includes(value as TimelineKind) ? (value as TimelineKind) : null;
}

export function readTimelineEntry(value: unknown): TimelineEntry | null {
  if (!isRecord(value)) return null;
  const at = text(value.at);
  const summary = text(value.summary);
  const kind = timelineKind(value.kind);
  if (!at || !summary || !kind) return null;
  const entry: TimelineEntry = { at, kind, summary, detail: text(value.detail) };
  const source = provenance(value.provenance);
  if (source) entry.provenance = source;
  return entry;
}

export function readTimelineEntries(value: unknown): TimelineEntry[] {
  if (!Array.isArray(value)) return [];
  return value
    .map(readTimelineEntry)
    .filter((entry): entry is TimelineEntry => entry !== null);
}

function textList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === "string" && item.trim() !== "");
}

export function readIncidentTimelineContent(
  content: Record<string, unknown>,
): IncidentTimelineContent {
  return {
    incident_ref: text(content.incident_ref),
    service: text(content.service),
    entries: readTimelineEntries(content.entries),
  };
}

export function readIncidentReportContent(
  content: Record<string, unknown>,
): IncidentReportContent {
  return {
    incident_ref: text(content.incident_ref),
    severity: text(content.severity),
    summary: text(content.summary) ?? "",
    impact: text(content.impact),
    timeline: readTimelineEntries(content.timeline),
    root_cause: text(content.root_cause),
    actions_taken: textList(content.actions_taken),
    remaining_risks: textList(content.remaining_risks),
  };
}

/**
 * Every recorded entry on a thread, in one order.
 *
 * Each `incident.timeline` artifact is the record of a single tool call, so a
 * thread accumulates a dozen of them and none is the timeline. Merging them is
 * a read-side operation on purpose: the artifacts stay exactly as the run
 * wrote them, and what the investigator looks at is a view.
 */
export function mergeTimelines(artifacts: Artifact[]): TimelineEntry[] {
  const merged: TimelineEntry[] = [];
  const seen = new Set<string>();
  for (const artifact of artifacts) {
    if (artifact.type !== INCIDENT_TIMELINE) continue;
    for (const entry of readTimelineEntries(artifact.content.entries)) {
      // The same deployment comes back on every `get_deployments` call, and
      // three of those in one investigation is normal. Identity is the event,
      // not the call that observed it, so provenance is left out of the key.
      const key = `${entry.at}|${entry.kind}|${entry.summary}`;
      if (seen.has(key)) continue;
      seen.add(key);
      merged.push(entry);
    }
  }
  return merged.sort((a, b) => (a.at < b.at ? -1 : a.at > b.at ? 1 : 0));
}

export function timelineEntryPayload(entry: TimelineEntry): Record<string, unknown> {
  return {
    at: entry.at,
    kind: entry.kind,
    summary: entry.summary,
    detail: entry.detail,
    ...(entry.provenance ? { provenance: { ...entry.provenance } } : {}),
  };
}

// ---------- Analysis content ----------

export type QueryCell = string | number | boolean | null;

export type AnalysisQueryResultContent = {
  question: string | null;
  sql: string;
  columns: string[];
  rows: QueryCell[][];
  row_count: number | null;
  truncated: boolean;
  provenance?: PaperProvenance;
};

export type AnalysisFindingContent = {
  question: string;
  conclusion: string;
  evidence: string[];
  queries: AnalysisQueryResultContent[];
};

function readRows(value: unknown, width: number): QueryCell[][] {
  if (!Array.isArray(value)) return [];
  const rows: QueryCell[][] = [];
  for (const row of value) {
    if (!Array.isArray(row) || row.length !== width) continue;
    rows.push(
      row.map((cell) =>
        cell === null ||
        typeof cell === "string" ||
        typeof cell === "number" ||
        typeof cell === "boolean"
          ? (cell as QueryCell)
          : null,
      ),
    );
  }
  return rows;
}

export function readAnalysisQueryResultContent(
  content: Record<string, unknown>,
): AnalysisQueryResultContent {
  const columns = textList(content.columns);
  const result: AnalysisQueryResultContent = {
    question: text(content.question),
    sql: text(content.sql) ?? "",
    columns,
    rows: readRows(content.rows, columns.length),
    row_count: integer(content.row_count),
    truncated: content.truncated === true,
  };
  const source = provenance(content.provenance);
  if (source) result.provenance = source;
  return result;
}

export function readAnalysisFindingContent(
  content: Record<string, unknown>,
): AnalysisFindingContent {
  const rawQueries = Array.isArray(content.queries) ? content.queries : [];
  return {
    question: text(content.question) ?? "",
    conclusion: text(content.conclusion) ?? "",
    evidence: textList(content.evidence),
    queries: rawQueries
      .filter(isRecord)
      .map(readAnalysisQueryResultContent)
      .filter((query) => query.sql !== ""),
  };
}

export function queryResultPayload(query: AnalysisQueryResultContent): Record<string, unknown> {
  return {
    question: query.question,
    sql: query.sql,
    columns: query.columns,
    rows: query.rows,
    row_count: query.row_count,
    truncated: query.truncated,
    ...(query.provenance ? { provenance: { ...query.provenance } } : {}),
  };
}

// ---------- Support content ----------

export type SupportHandoffContent = {
  customer_ref: string | null;
  case_ref: string | null;
  problem: string;
  checked: string[];
  findings: string[];
  recommended_action: string;
  reason: string | null;
};

export function readSupportHandoffContent(
  content: Record<string, unknown>,
): SupportHandoffContent {
  return {
    customer_ref: text(content.customer_ref),
    case_ref: text(content.case_ref),
    problem: text(content.problem) ?? "",
    checked: textList(content.checked),
    findings: textList(content.findings),
    recommended_action: text(content.recommended_action) ?? "",
    reason: text(content.reason),
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
