"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";

import { EmptyState, ErrorState, LoadingState, Panel, SessionRequired } from "../../../components/states";
import StatusBadge from "../../../components/status-badge";
import { useFrontendSession } from "../../../components/session-provider";
import { useWorkspaceData, useWorkspaceMutation } from "../../../components/use-workspace-data";
import { errorHintKey, type AuthInput } from "../../../lib/api-client";
import {
  createSnapshot,
  listDocumentRevisions,
  listDocuments,
  listKnowledgeBases,
  listSnapshots,
  uploadDocument,
  uploadDocumentRevision,
  type DocumentRevisionStatus,
  type KnowledgeBase,
  type KnowledgeDocument,
  type KnowledgeSnapshot,
} from "../../../lib/knowledge";
import { useI18n } from "../../../i18n/provider";

type Tab = "documents" | "snapshots";

export default function KnowledgeBaseDetailClient({ knowledgeBaseId }: { knowledgeBaseId: string }) {
  const { t, formatDateTime, formatNumber } = useI18n();
  const { connected, sessionId, workspaceId } = useFrontendSession();

  const [tab, setTab] = useState<Tab>("documents");
  const [selectedDocumentId, setSelectedDocumentId] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const revisionInputRef = useRef<HTMLInputElement | null>(null);

  // Resource identity includes both workspace and knowledge base, so a
  // workspace switch or a route change invalidates in-flight responses.
  const scope = `${workspaceId}:${knowledgeBaseId}`;

  const loadBases = useCallback((auth: AuthInput) => listKnowledgeBases(auth), []);
  const loadDocuments = useCallback(
    (auth: AuthInput) => listDocuments(auth, knowledgeBaseId),
    [knowledgeBaseId],
  );
  const loadSnapshots = useCallback(
    (auth: AuthInput) => listSnapshots(auth, knowledgeBaseId),
    [knowledgeBaseId],
  );
  const loadRevisions = useCallback(
    (auth: AuthInput) => listDocumentRevisions(auth, knowledgeBaseId, selectedDocumentId ?? ""),
    [knowledgeBaseId, selectedDocumentId],
  );

  const bases = useWorkspaceData<KnowledgeBase[]>(loadBases, `knowledge-bases:${workspaceId}`);
  const documents = useWorkspaceData<KnowledgeDocument[]>(loadDocuments, `kb-documents:${scope}`);
  const snapshots = useWorkspaceData<KnowledgeSnapshot[]>(loadSnapshots, `kb-snapshots:${scope}`);
  const revisions = useWorkspaceData<DocumentRevisionStatus[]>(
    loadRevisions,
    `kb-revisions:${scope}:${selectedDocumentId ?? ""}`,
    { enabled: Boolean(selectedDocumentId) },
  );

  const documentMutation = useWorkspaceMutation(`kb-documents:${scope}`);
  const snapshotMutation = useWorkspaceMutation(`kb-snapshots:${scope}`);

  useEffect(() => {
    setTab("documents");
    setSelectedDocumentId(null);
    setNotice(null);
    if (fileInputRef.current) fileInputRef.current.value = "";
    if (revisionInputRef.current) revisionInputRef.current.value = "";
  }, [sessionId, knowledgeBaseId]);

  if (!connected) {
    return (
      <div className="page">
        <header className="page-header">
          <p className="eyebrow">{t("knowledge.eyebrow")}</p>
          <h1>{t("knowledge.baseDetail")}</h1>
        </header>
        <SessionRequired contextKey="session.context.knowledge" />
      </div>
    );
  }

  const base = (bases.data ?? []).find((item) => item.id === knowledgeBaseId) ?? null;
  const documentList = documents.data ?? [];
  const snapshotList = snapshots.data ?? [];
  const revisionList = revisions.data ?? [];
  const selectedDocument = documentList.find((item) => item.id === selectedDocumentId) ?? null;

  async function submitUpload(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const file = fileInputRef.current?.files?.[0];
    if (!file) return;
    setNotice(null);
    const result = await documentMutation.run((auth) => uploadDocument(auth, knowledgeBaseId, file));
    if (result) {
      if (fileInputRef.current) fileInputRef.current.value = "";
      setNotice(t("knowledge.uploadAccepted"));
      documents.reload();
    }
  }

  async function submitRevision(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const file = revisionInputRef.current?.files?.[0];
    if (!file || !selectedDocumentId) return;
    const documentId = selectedDocumentId;
    setNotice(null);
    const result = await documentMutation.run((auth) =>
      uploadDocumentRevision(auth, knowledgeBaseId, documentId, file),
    );
    if (result) {
      if (revisionInputRef.current) revisionInputRef.current.value = "";
      setNotice(t("knowledge.uploadAccepted"));
      revisions.reload();
    }
  }

  async function submitSnapshot() {
    setNotice(null);
    const result = await snapshotMutation.run((auth) => createSnapshot(auth, knowledgeBaseId));
    if (result) {
      setNotice(t("knowledge.snapshotCreated"));
      snapshots.reload();
    }
  }

  return (
    <div className="page">
      <header className="page-header">
        <p className="eyebrow">{t("knowledge.eyebrow")}</p>
        <h1>{base?.name ?? t("knowledge.baseDetail")}</h1>
        <p className="page-lede">
          <code>{knowledgeBaseId}</code>
        </p>
      </header>

      <div className="page-toolbar">
        <Link className="button button-ghost" href="/knowledge">
          {t("knowledge.backToBases")}
        </Link>
        <Link className="button button-ghost" href="/knowledge/playground">
          {t("knowledge.openPlayground")}
        </Link>
      </div>

      {notice && <p className="inline-notice">{notice}</p>}

      <div className="tab-strip" role="tablist" aria-label={t("knowledge.sections")}>
        <button
          type="button"
          role="tab"
          aria-selected={tab === "documents"}
          className={tab === "documents" ? "button button-primary" : "button button-ghost"}
          onClick={() => setTab("documents")}
        >
          {t("knowledge.documents")}
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === "snapshots"}
          className={tab === "snapshots" ? "button button-primary" : "button button-ghost"}
          onClick={() => setTab("snapshots")}
        >
          {t("knowledge.snapshots")}
        </button>
      </div>

      {tab === "documents" && (
        <>
          <Panel ariaLabel={t("knowledge.documents")} title={t("knowledge.documents")}>
            <form className="eval-form" onSubmit={submitUpload} noValidate>
              <p className="eval-form-title">{t("knowledge.uploadDocument")}</p>
              <label>
                {t("knowledge.file")}
                <input type="file" ref={fileInputRef} />
              </label>
              {documentMutation.error && (
                <p className="session-error" role="alert">
                  <code>{documentMutation.error.code}</code>{" "}
                  {documentMutation.error.message || t("errors.requestFailed")}
                </p>
              )}
              <div className="form-actions">
                <button type="submit" className="button button-primary" disabled={documentMutation.pending}>
                  {t("knowledge.upload")}
                </button>
              </div>
            </form>

            {documents.error && (
              <ErrorState
                code={documents.error.code}
                message={documents.error.message || t("errors.loadDocuments")}
                hint={errorHintKey(documents.error) ? t(errorHintKey(documents.error)!) : undefined}
                onRetry={documents.reload}
              />
            )}
            {documents.loading && !documents.error && <LoadingState />}
            {documents.loaded && !documents.error && documentList.length === 0 && (
              <EmptyState title={t("knowledge.noDocuments")} hint={t("knowledge.noDocumentsHint")} />
            )}

            {documentList.length > 0 && (
              <div className="data-table">
                <table>
                  <thead>
                    <tr>
                      <th scope="col">{t("knowledge.documentName")}</th>
                      <th scope="col">{t("common.created")}</th>
                      <th scope="col">{t("settings.models.actions")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {documentList.map((document) => (
                      <tr key={document.id}>
                        <td data-label={t("knowledge.documentName")}>{document.name}</td>
                        <td data-label={t("common.created")}>
                          {document.created_at ? formatDateTime(document.created_at) : t("common.none")}
                        </td>
                        <td data-label={t("settings.models.actions")}>
                          <button
                            type="button"
                            className="button button-ghost"
                            onClick={() =>
                              setSelectedDocumentId((current) =>
                                current === document.id ? null : document.id,
                              )
                            }
                          >
                            {selectedDocumentId === document.id
                              ? t("knowledge.hideRevisions")
                              : t("knowledge.viewRevisions")}
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Panel>

          {selectedDocumentId && (
            <Panel
              ariaLabel={t("knowledge.revisions")}
              title={t("knowledge.revisions")}
              eyebrow={selectedDocument?.name ?? ""}
            >
              <form className="eval-form" onSubmit={submitRevision} noValidate>
                <p className="eval-form-title">{t("knowledge.uploadRevision")}</p>
                <label>
                  {t("knowledge.file")}
                  <input type="file" ref={revisionInputRef} />
                </label>
                <div className="form-actions">
                  <button type="submit" className="button button-primary" disabled={documentMutation.pending}>
                    {t("knowledge.upload")}
                  </button>
                </div>
              </form>

              {revisions.error && (
                <ErrorState
                  code={revisions.error.code}
                  message={revisions.error.message || t("errors.loadRevisions")}
                  hint={errorHintKey(revisions.error) ? t(errorHintKey(revisions.error)!) : undefined}
                  onRetry={revisions.reload}
                />
              )}
              {revisions.loading && !revisions.error && <LoadingState />}
              {revisions.loaded && !revisions.error && revisionList.length === 0 && (
                <EmptyState title={t("knowledge.noRevisions")} />
              )}

              {revisionList.length > 0 && (
                <div className="data-table">
                  <table>
                    <thead>
                      <tr>
                        <th scope="col">{t("knowledge.revision")}</th>
                        <th scope="col">{t("knowledge.fileName")}</th>
                        <th scope="col">{t("knowledge.lifecycle")}</th>
                        <th scope="col">{t("knowledge.ingestion")}</th>
                        <th scope="col">{t("knowledge.stage")}</th>
                        <th scope="col">{t("common.created")}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {revisionList.map((entry) => (
                        <tr key={entry.revision.id}>
                          <td data-label={t("knowledge.revision")}>#{entry.revision.revision_number}</td>
                          <td data-label={t("knowledge.fileName")}>
                            <code>{entry.revision.original_filename}</code>
                          </td>
                          <td data-label={t("knowledge.lifecycle")}>
                            <StatusBadge status={entry.revision.lifecycle_status} />
                          </td>
                          <td data-label={t("knowledge.ingestion")}>
                            <StatusBadge status={entry.revision.ingestion_status} />
                          </td>
                          <td data-label={t("knowledge.stage")}>
                            <code>{entry.ingestion_job?.stage ?? "—"}</code>
                            {entry.ingestion_job?.safe_error_message && (
                              <span className="state-hint">{entry.ingestion_job.safe_error_message}</span>
                            )}
                          </td>
                          <td data-label={t("common.created")}>
                            {formatDateTime(entry.revision.created_at)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </Panel>
          )}
        </>
      )}

      {tab === "snapshots" && (
        <Panel
          ariaLabel={t("knowledge.snapshots")}
          title={t("knowledge.snapshots")}
          actions={
            <button
              type="button"
              className="button button-primary"
              onClick={() => void submitSnapshot()}
              disabled={snapshotMutation.pending}
            >
              {t("knowledge.createSnapshot")}
            </button>
          }
        >
          <p className="state-hint">{t("knowledge.snapshotNote")}</p>
          {snapshotMutation.error && (
            <p className="session-error" role="alert">
              <code>{snapshotMutation.error.code}</code>{" "}
              {snapshotMutation.error.message || t("errors.requestFailed")}
            </p>
          )}

          {snapshots.error && (
            <ErrorState
              code={snapshots.error.code}
              message={snapshots.error.message || t("errors.loadSnapshots")}
              hint={errorHintKey(snapshots.error) ? t(errorHintKey(snapshots.error)!) : undefined}
              onRetry={snapshots.reload}
            />
          )}
          {snapshots.loading && !snapshots.error && <LoadingState />}
          {snapshots.loaded && !snapshots.error && snapshotList.length === 0 && (
            <EmptyState title={t("knowledge.noSnapshots")} hint={t("knowledge.noSnapshotsHint")} />
          )}

          {snapshotList.length > 0 && (
            <div className="data-table">
              <table>
                <thead>
                  <tr>
                    <th scope="col">{t("common.id")}</th>
                    <th scope="col">{t("knowledge.contentHash")}</th>
                    <th scope="col">{t("knowledge.items")}</th>
                    <th scope="col">{t("knowledge.schemaVersion")}</th>
                    <th scope="col">{t("common.created")}</th>
                  </tr>
                </thead>
                <tbody>
                  {snapshotList.map((snapshot) => (
                    <tr key={snapshot.id}>
                      <td data-label={t("common.id")}>
                        <code>{snapshot.id}</code>
                      </td>
                      <td data-label={t("knowledge.contentHash")}>
                        <code className="hash-value">{snapshot.content_hash}</code>
                      </td>
                      <td data-label={t("knowledge.items")}>{formatNumber(snapshot.item_count)}</td>
                      <td data-label={t("knowledge.schemaVersion")}>{snapshot.snapshot_schema_version}</td>
                      <td data-label={t("common.created")}>{formatDateTime(snapshot.created_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>
      )}
    </div>
  );
}
