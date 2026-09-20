import { apiRequest, apiUpload, type AuthInput } from "@/lib/api/client";
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
