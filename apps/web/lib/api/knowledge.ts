import { ApiError, isRecord, apiRequest, apiUpload, type AuthInput } from "@/lib/api/client";
import { listItems } from "@/lib/api/tenancy";

/**
 * Knowledge manager client: knowledge bases, documents, revisions and
 * immutable snapshots.
 *
 * Ingestion status is reported by the backend; the UI never infers or
 * simulates progress.
 */

export type KnowledgeBase = {
  id: string;
  workspace_id: string;
  name: string;
  created_at: string;
};

export type KnowledgeDocument = {
  id: string;
  workspace_id: string;
  knowledge_base_id: string;
  name: string;
  created_at: string;
  // Projection of the latest revision: the ingestion state machine lives on
  // the revision, and the list carries it so the table can show progress
  // without opening each document.
  current_revision_id: string | null;
  current_revision_number: number | null;
  current_revision_status: string | null;
  current_revision_lifecycle_status: string | null;
  current_revision_created_at: string | null;
  effective_date: string | null;
  superseded_by_document_id: string | null;
};

export type DocumentRevision = {
  id: string;
  document_id: string;
  revision_number: number;
  original_filename: string;
  media_type: string;
  file_size: number;
  lifecycle_status: string;
  ingestion_status: string;
  created_at: string;
};

export type IngestionJob = {
  id: string;
  document_revision_id: string;
  status: string;
  stage: string;
  attempt_count: number;
  last_error_code: string | null;
  safe_error_message: string | null;
  created_at: string;
  updated_at: string;
};

export type DocumentRevisionStatus = {
  revision: DocumentRevision;
  ingestion_job: IngestionJob;
};

export type DocumentUploadResult = {
  document: KnowledgeDocument;
  revision: DocumentRevision;
  ingestion_job: IngestionJob;
};

export type KnowledgeSnapshot = {
  id: string;
  workspace_id: string;
  knowledge_base_id: string;
  content_hash: string;
  snapshot_schema_version: number;
  item_count: number;
  created_at: string;
};

export type SnapshotChunkPage = {
  workspace_id: string; knowledge_base_id: string; snapshot_id: string;
  total: number; offset: number; limit: number;
  items: { chunk_id: string; document_revision_id: string; ordinal: number; text: string; truncated: boolean }[];
};

export async function previewSnapshotChunks(input: AuthInput, kb: string, snapshot: string, offset: number): Promise<SnapshotChunkPage> {
  const payload = await apiRequest<unknown>(`${kbPath(input.workspaceId, kb)}/snapshots/${encodeURIComponent(snapshot)}/chunks?limit=10&offset=${offset}`, input.accessToken);
  if (!isRecord(payload) || payload.workspace_id !== input.workspaceId || payload.knowledge_base_id !== kb || payload.snapshot_id !== snapshot ||
      payload.offset !== offset || payload.limit !== 10 || !Number.isInteger(payload.total) || (payload.total as number) < 0 ||
      !Array.isArray(payload.items) || payload.items.length > 10 || !payload.items.every((item: unknown) => isRecord(item) &&
        typeof item.chunk_id === "string" && typeof item.document_revision_id === "string" && Number.isInteger(item.ordinal) &&
        typeof item.text === "string" && item.text.length <= 4096 && typeof item.truncated === "boolean")) {
    throw new ApiError("INVALID_RESPONSE", "Invalid snapshot preview response.", 502);
  }
  return payload as SnapshotChunkPage;
}

function knowledgeBase(workspaceId: string): string {
  return `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/knowledge-bases`;
}

function kbPath(workspaceId: string, knowledgeBaseId: string): string {
  return `${knowledgeBase(workspaceId)}/${encodeURIComponent(knowledgeBaseId)}`;
}

export async function listKnowledgeBases(input: AuthInput): Promise<KnowledgeBase[]> {
  const payload = await apiRequest<unknown>(knowledgeBase(input.workspaceId), input.accessToken);
  return listItems<KnowledgeBase>(payload);
}

export function createKnowledgeBase(input: AuthInput, body: { name: string }): Promise<KnowledgeBase> {
  return apiRequest<KnowledgeBase>(knowledgeBase(input.workspaceId), input.accessToken, {
    method: "POST",
    body: { name: body.name },
  });
}

export async function listDocuments(
  input: AuthInput,
  knowledgeBaseId: string,
): Promise<KnowledgeDocument[]> {
  const payload = await apiRequest<unknown>(
    `${kbPath(input.workspaceId, knowledgeBaseId)}/documents`,
    input.accessToken,
  );
  return listItems<KnowledgeDocument>(payload);
}

/** Uploads a new document. The backend field name is `file`. */
export function uploadDocument(
  input: AuthInput,
  knowledgeBaseId: string,
  file: File,
): Promise<DocumentUploadResult> {
  const formData = new FormData();
  formData.append("file", file);
  return apiUpload<DocumentUploadResult>(
    `${kbPath(input.workspaceId, knowledgeBaseId)}/documents`,
    input.accessToken,
    formData,
  );
}

export function uploadDocumentRevision(
  input: AuthInput,
  knowledgeBaseId: string,
  documentId: string,
  file: File,
): Promise<DocumentUploadResult> {
  const formData = new FormData();
  formData.append("file", file);
  return apiUpload<DocumentUploadResult>(
    `${kbPath(input.workspaceId, knowledgeBaseId)}/documents/${encodeURIComponent(documentId)}/revisions`,
    input.accessToken,
    formData,
  );
}

/** Requeues the latest revision's failed ingestion; 409 when it has not failed. */
export function retryDocumentIngestion(
  input: AuthInput,
  knowledgeBaseId: string,
  documentId: string,
): Promise<DocumentUploadResult> {
  return apiRequest<DocumentUploadResult>(
    `${kbPath(input.workspaceId, knowledgeBaseId)}/documents/${encodeURIComponent(documentId)}/retries`,
    input.accessToken,
    { method: "POST", body: {} },
  );
}

export async function listDocumentRevisions(
  input: AuthInput,
  knowledgeBaseId: string,
  documentId: string,
): Promise<DocumentRevisionStatus[]> {
  const payload = await apiRequest<unknown>(
    `${kbPath(input.workspaceId, knowledgeBaseId)}/documents/${encodeURIComponent(documentId)}/revisions`,
    input.accessToken,
  );
  return listItems<DocumentRevisionStatus>(payload);
}

export async function listSnapshots(
  input: AuthInput,
  knowledgeBaseId: string,
): Promise<KnowledgeSnapshot[]> {
  const payload = await apiRequest<unknown>(
    `${kbPath(input.workspaceId, knowledgeBaseId)}/snapshots`,
    input.accessToken,
  );
  return listItems<KnowledgeSnapshot>(payload);
}

export function getSnapshot(
  input: AuthInput,
  knowledgeBaseId: string,
  snapshotId: string,
): Promise<KnowledgeSnapshot> {
  return apiRequest<KnowledgeSnapshot>(
    `${kbPath(input.workspaceId, knowledgeBaseId)}/snapshots/${encodeURIComponent(snapshotId)}`,
    input.accessToken,
  );
}

export function createSnapshot(input: AuthInput, knowledgeBaseId: string): Promise<KnowledgeSnapshot> {
  return apiRequest<KnowledgeSnapshot>(
    `${kbPath(input.workspaceId, knowledgeBaseId)}/snapshots`,
    input.accessToken,
    { method: "POST", body: {} },
  );
}
