"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";

import HashValue, { InlineConfirm } from "@/components/evaluation/hash-value";
import { useFrontendSession } from "@/components/providers/session-provider";
import { Markdown } from "@/components/ui/markdown";
import Select from "@/components/ui/select";
import {
  EmptyState,
  ErrorState,
  InlineError,
  LoadingState,
  Panel,
  SessionRequired,
} from "@/components/ui/states";
import StatusBadge from "@/components/ui/status-badge";
import { useWorkspaceData, useWorkspaceMutation } from "@/hooks/use-workspace-data";
import { useI18n } from "@/i18n/provider";
import { ApiError, errorHintKey, type AuthInput } from "@/lib/api/client";
import {
  listKnowledgeBases,
  listSnapshots,
  type KnowledgeBase,
  type KnowledgeSnapshot,
} from "@/lib/api/knowledge";
import { listModelProfiles, type ModelProfile } from "@/lib/api/models";
import {
  createCompilation,
  getCompilation,
  getCompilationLint,
  getCompilationPages,
  listCompilations,
  publishCompilation,
  queryWikiCompilation,
  retryCompilation,
  type WikiCompilation,
  type WikiCompilationStatus,
  type WikiEvidence,
  type WikiLintIssue,
  type WikiPage,
  type WikiQueryResult,
  type WikiRelation,
} from "@/lib/api/wiki";

import { wikiWorkbenchCopy, type WikiWorkbenchCopy } from "./wiki-copy";

type Tab = "pages" | "lint" | "query";
type QueryMode = "AUTO" | "WIKI" | "RAG" | "HYBRID";
type QueryIntent = "overview" | "fact" | "synthesis";

type QueryDisplay = {
  query: string;
  mode: QueryMode;
  intent: QueryIntent;
  requireCurrent: boolean;
  generatedModelId: string;
  result: WikiQueryResult;
};

const ACTIVE_COMPILATION_STATUSES = new Set<WikiCompilationStatus>(["PENDING", "PROCESSING"]);
const COMPILATION_POLL_MS = 5000;

function statusGuidance(status: WikiCompilationStatus, copy: WikiWorkbenchCopy): string {
  return copy.statusGuidance[status] ?? status;
}

function relationTarget(relation: WikiRelation): string {
  if (typeof relation === "string") return relation;
  return typeof relation.target === "string" ? relation.target : JSON.stringify(relation);
}

function displayValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function shortId(value: string): string {
  return value.length > 12 ? `${value.slice(0, 8)}…` : value;
}

function canInspectCompilation(
  status: WikiCompilationStatus,
  canRead: boolean,
  canEdit: boolean,
): boolean {
  return status === "PUBLISHED" ? canRead : status === "READY" && canEdit;
}

export default function WikiWorkbenchPage() {
  const { sessionId, workspaceId } = useFrontendSession();
  return <WikiWorkbenchContent key={`${sessionId}:${workspaceId}`} />;
}

function WikiWorkbenchContent() {
  const { locale, t, formatDateTime, formatNumber } = useI18n();
  const copy = wikiWorkbenchCopy(locale);
  const { connected, workspaceId, permissions, accessToken } = useFrontendSession();
  const canRead = permissions?.includes("knowledge_run") === true;
  const canEdit = permissions?.includes("knowledge_edit") === true;
  const accessTokenRef = useRef(accessToken);
  const tokenTransition = accessTokenRef.current !== accessToken;

  const [selectedKbId, setSelectedKbId] = useState("");
  const [selectedCompilationId, setSelectedCompilationId] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<Tab>("pages");
  const [selectedPageKey, setSelectedPageKey] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [showCompileForm, setShowCompileForm] = useState(false);
  const [publishConfirm, setPublishConfirm] = useState(false);

  const [compileModelId, setCompileModelId] = useState("");
  const [sourceSnapshotId, setSourceSnapshotId] = useState("LATEST");
  const [reviewMode, setReviewMode] = useState<"BASIC" | "MODEL">("BASIC");
  const [outputMode, setOutputMode] = useState<"PROMPT_JSON" | "JSON_SCHEMA">("PROMPT_JSON");

  const [queryInput, setQueryInput] = useState("");
  const [queryMode, setQueryMode] = useState<QueryMode>("AUTO");
  const [queryIntent, setQueryIntent] = useState<QueryIntent>("overview");
  const [requireCurrent, setRequireCurrent] = useState(true);
  const [generateAnswer, setGenerateAnswer] = useState(false);
  const [queryModelId, setQueryModelId] = useState("");
  const [queryDisplay, setQueryDisplay] = useState<QueryDisplay | null>(null);
  const [queryGeneration, setQueryGeneration] = useState(0);

  const loadBases = useCallback((auth: AuthInput) => listKnowledgeBases(auth), []);
  const bases = useWorkspaceData<KnowledgeBase[]>(
    loadBases,
    `wiki-bases:${workspaceId}`,
    { enabled: connected && canRead },
  );

  const loadModels = useCallback((auth: AuthInput) => listModelProfiles(auth), []);
  const models = useWorkspaceData<ModelProfile[]>(
    loadModels,
    `wiki-model-profiles:${workspaceId}`,
    { enabled: connected && canRead },
  );

  const loadSnapshots = useCallback(
    (auth: AuthInput) => listSnapshots(auth, selectedKbId),
    [selectedKbId],
  );
  const snapshots = useWorkspaceData<KnowledgeSnapshot[]>(
    loadSnapshots,
    `wiki-snapshots:${workspaceId}:${selectedKbId}`,
    { enabled: connected && canEdit && Boolean(selectedKbId) },
  );

  const loadCompilations = useCallback(
    (auth: AuthInput) => listCompilations(auth, selectedKbId),
    [selectedKbId],
  );
  const compilations = useWorkspaceData<WikiCompilation[]>(
    loadCompilations,
    `wiki-compilations:${workspaceId}:${selectedKbId}`,
    { enabled: connected && canRead && Boolean(selectedKbId) },
  );

  const compilationList = compilations.data ?? [];
  const selectedCompilation =
    compilationList.find((row) => row.id === selectedCompilationId) ?? null;
  const selectedStatus = selectedCompilation?.status ?? null;
  const canInspectSelected =
    selectedStatus !== null && canInspectCompilation(selectedStatus, canRead, canEdit);

  const loadPages = useCallback(
    (auth: AuthInput) =>
      selectedKbId && selectedCompilationId
        ? getCompilationPages(auth, selectedKbId, selectedCompilationId)
        : Promise.resolve([]),
    [selectedKbId, selectedCompilationId],
  );
  const pages = useWorkspaceData<WikiPage[]>(
    loadPages,
    `wiki-pages:${workspaceId}:${selectedKbId}:${selectedCompilationId ?? ""}:${selectedStatus ?? ""}`,
    { enabled: connected && canInspectSelected && activeTab === "pages" },
  );

  const loadLint = useCallback(
    (auth: AuthInput) =>
      selectedKbId && selectedCompilationId
        ? getCompilationLint(auth, selectedKbId, selectedCompilationId)
        : Promise.resolve([]),
    [selectedKbId, selectedCompilationId],
  );
  const lint = useWorkspaceData<WikiLintIssue[]>(
    loadLint,
    `wiki-lint:${workspaceId}:${selectedKbId}:${selectedCompilationId ?? ""}:${selectedStatus ?? ""}`,
    { enabled: connected && canInspectSelected && activeTab === "lint" },
  );

  const createMutation = useWorkspaceMutation(
    `wiki-create:${workspaceId}:${selectedKbId}`,
  );
  const compilationMutation = useWorkspaceMutation(
    `wiki-compilation-action:${workspaceId}:${selectedKbId}:${selectedCompilationId ?? ""}`,
  );
  const queryMutation = useWorkspaceMutation(
    `wiki-query:${workspaceId}:${selectedKbId}:${selectedCompilationId ?? ""}:${queryGeneration}`,
  );

  useEffect(() => {
    if (!bases.loaded) return;
    const available = bases.data ?? [];
    setSelectedKbId((current) =>
      available.some((base) => base.id === current) ? current : (available[0]?.id ?? ""),
    );
  }, [bases.data, bases.loaded]);

  useEffect(() => {
    if (!models.loaded) return;
    const enabled = (models.data ?? []).filter((profile) => profile.enabled);
    setCompileModelId((current) =>
      enabled.some((profile) => profile.id === current) ? current : (enabled[0]?.id ?? ""),
    );
    setQueryModelId((current) =>
      enabled.some((profile) => profile.id === current) ? current : (enabled[0]?.id ?? ""),
    );
  }, [models.data, models.loaded]);

  useEffect(() => {
    setSelectedCompilationId(null);
    setSelectedPageKey(null);
    setActiveTab("pages");
    setNotice(null);
    setPublishConfirm(false);
    setShowCompileForm(false);
    setSourceSnapshotId("LATEST");
    setQueryDisplay(null);
    setQueryGeneration((current) => current + 1);
  }, [selectedKbId]);

  useEffect(() => {
    if (!compilations.loaded) return;
    const available = compilations.data ?? [];
    setSelectedCompilationId((current) =>
      current && available.some((row) => row.id === current) ? current : (available[0]?.id ?? null),
    );
  }, [compilations.data, compilations.loaded]);

  useEffect(() => {
    if (!snapshots.loaded) return;
    const available = snapshots.data ?? [];
    setSourceSnapshotId((current) =>
      current === "LATEST" || available.some((snapshot) => snapshot.id === current)
        ? current
        : "LATEST",
    );
  }, [snapshots.data, snapshots.loaded]);

  useEffect(() => {
    setActiveTab((current) => {
      if (current === "query" && selectedStatus !== "PUBLISHED") return "pages";
      return current;
    });
    setSelectedPageKey(null);
    setPublishConfirm(false);
    setQueryDisplay(null);
  }, [selectedCompilationId, selectedStatus]);

  const reloadCompilations = compilations.reload;
  const compilationInFlight =
    selectedStatus !== null && ACTIVE_COMPILATION_STATUSES.has(selectedStatus);
  useEffect(() => {
    if (!compilationInFlight) return;
    const interval = window.setInterval(() => {
      if (!document.hidden) reloadCompilations();
    }, COMPILATION_POLL_MS);
    return () => window.clearInterval(interval);
  }, [compilationInFlight, reloadCompilations]);

  useEffect(() => {
    if (!tokenTransition) return;
    accessTokenRef.current = accessToken;
    setQueryDisplay(null);
    setQueryGeneration((current) => current + 1);
    setNotice(null);
    setPublishConfirm(false);
    createMutation.clearError();
    compilationMutation.clearError();
    queryMutation.clearError();
  }, [
    accessToken,
    tokenTransition,
    createMutation.clearError,
    compilationMutation.clearError,
    queryMutation.clearError,
  ]);

  const pageList = pages.data ?? [];
  const selectedPage =
    pageList.find((page) => page.page_key === selectedPageKey) ?? pageList[0] ?? null;
  const lintIssues = lint.data ?? [];
  const queryMatchesInput =
    queryDisplay !== null &&
    queryDisplay.query === queryInput.trim() &&
    queryDisplay.mode === queryMode &&
    queryDisplay.intent === queryIntent &&
    queryDisplay.requireCurrent === requireCurrent &&
    queryDisplay.generatedModelId === (generateAnswer ? queryModelId : "");
  const visibleQuery = queryMatchesInput ? queryDisplay?.result ?? null : null;

  function invalidateQuery(next: () => void) {
    next();
    setQueryDisplay(null);
    setQueryGeneration((current) => current + 1);
  }

  async function createWikiCompilation(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canEdit || !selectedKbId || !compileModelId) return;
    setNotice(null);
    const result = await createMutation.run(async (auth) => {
      const created = await createCompilation(auth, selectedKbId, {
        model_profile_id: compileModelId,
        source_snapshot_id: sourceSnapshotId,
        review_mode: reviewMode,
        output_mode: outputMode,
      });
      return getCompilation(auth, selectedKbId, created.id);
    });
    if (!result) return;
    setSelectedCompilationId(result.id);
    setShowCompileForm(false);
    setNotice(copy.compilationAccepted(result.status));
    compilations.reload();
  }

  async function publishSelectedCompilation() {
    if (!canEdit || !selectedKbId || !selectedCompilation) return;
    const knowledgeBaseId = selectedKbId;
    const compilationId = selectedCompilation.id;
    setNotice(null);
    const result = await compilationMutation.run(async (auth) => {
      await publishCompilation(auth, knowledgeBaseId, compilationId);
      const confirmed = await getCompilation(auth, knowledgeBaseId, compilationId);
      if (confirmed.status !== "PUBLISHED") {
        throw new ApiError(
          "SERVER_STATE_NOT_CONFIRMED",
          copy.serverStateMismatch(confirmed.status),
          409,
        );
      }
      return confirmed;
    });
    if (!result) return;
    setPublishConfirm(false);
    setNotice(copy.publicationConfirmed);
    compilations.reload();
    pages.reload();
    lint.reload();
  }

  async function retrySelectedCompilation() {
    if (!canEdit || !selectedKbId || !selectedCompilation) return;
    const knowledgeBaseId = selectedKbId;
    const compilationId = selectedCompilation.id;
    setNotice(null);
    const result = await compilationMutation.run(async (auth) => {
      await retryCompilation(auth, knowledgeBaseId, compilationId);
      return getCompilation(auth, knowledgeBaseId, compilationId);
    });
    if (!result) return;
    setNotice(copy.retryAccepted(result.status));
    compilations.reload();
  }

  async function submitWikiQuery(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const query = queryInput.trim();
    if (!canRead || !selectedKbId || !selectedCompilation || !query) return;
    if (selectedCompilation.status !== "PUBLISHED") return;
    if (generateAnswer && !queryModelId) return;

    const request = {
      query,
      mode: queryMode,
      intent: queryIntent,
      requireCurrent,
      generatedModelId: generateAnswer ? queryModelId : "",
    };
    setQueryDisplay(null);
    const result = await queryMutation.run((auth) =>
      queryWikiCompilation(auth, selectedKbId, selectedCompilation.id, {
        source_snapshot_id: selectedCompilation.source_snapshot_id,
        query: request.query,
        mode: request.mode,
        intent: request.intent,
        require_current: request.requireCurrent,
        model_profile_id: request.generatedModelId || undefined,
      }),
    );
    if (!result) return;
    setQueryDisplay({ ...request, result });
  }

  if (!connected) {
    return (
      <div className="page">
        <header className="page-header">
          <h1>{copy.title}</h1>
        </header>
        <SessionRequired contextKey="session.context.knowledge" />
      </div>
    );
  }

  if (tokenTransition || permissions === null) {
    return (
      <div className="page">
        <header className="page-header">
          <h1>{copy.title}</h1>
        </header>
        <LoadingState
          label={tokenTransition ? copy.refreshingSession : copy.loadingPermissions}
        />
      </div>
    );
  }

  if (!canRead) {
    return (
      <div className="page">
        <header className="page-header">
          <h1>{copy.title}</h1>
          <p className="page-lead">
            {copy.lead}
          </p>
        </header>
        <Panel title={copy.accessRequired}>
          <EmptyState
            title={copy.accessTitle}
            hint={copy.accessHint}
            actions={
              <Link className="button button-ghost" href="/knowledge">
                {t("nav.knowledge")}
              </Link>
            }
          />
        </Panel>
      </div>
    );
  }

  const modelList = models.data ?? [];
  const enabledModelOptions = modelList.map((profile) => ({
    value: profile.id,
    label: `${profile.model}${profile.enabled ? "" : copy.modelDisabled}`,
    disabled: !profile.enabled,
  }));
  const snapshotOptions = [
    { value: "LATEST", label: copy.latestSnapshot },
    ...(snapshots.data ?? []).map((snapshot) => ({
      value: snapshot.id,
      label: `${shortId(snapshot.id)} · ${copy.snapshotItems(formatNumber(snapshot.item_count))}`,
    })),
  ];

  return (
    <div className="page">
      <header className="page-header">
        <div>
          <h1>{copy.title}</h1>
          <p className="page-lead">
            {copy.lead}
          </p>
        </div>
        <div className="page-actions">
          <Link className="button button-ghost" href="/knowledge">
            {t("nav.knowledge")}
          </Link>
        </div>
      </header>

      <div className="page-toolbar">
        <label>
          {copy.knowledgeBase}
          <Select
            value={selectedKbId}
            onChange={setSelectedKbId}
            options={(bases.data ?? []).map((base) => ({ value: base.id, label: base.name }))}
            placeholder={copy.selectKnowledgeBase}
            ariaLabel={copy.wikiKnowledgeBaseAria}
          />
        </label>
      </div>

      {notice && <p className="inline-notice">{notice}</p>}

      {bases.error && (
        <ErrorState
          code={bases.error.code}
          message={bases.error.message || copy.loadBases}
          hint={errorHintKey(bases.error) ? t(errorHintKey(bases.error)!) : undefined}
          onRetry={bases.reload}
        />
      )}
      {bases.loading && !bases.error && <LoadingState />}
      {bases.loaded && !bases.error && (bases.data ?? []).length === 0 && (
        <EmptyState
          title={t("knowledge.noBases")}
          hint={t("knowledge.noBasesHint")}
          actions={
            <Link className="button button-primary" href="/knowledge">
              {t("knowledge.createBase")}
            </Link>
          }
        />
      )}

      {selectedKbId && (
        <>
          <Panel
            title={copy.compilations}
            ariaLabel={copy.wikiCompilationsAria}
            actions={
              canEdit ? (
                <button
                  type="button"
                  className="button button-primary"
                  onClick={() => setShowCompileForm((current) => !current)}
                >
                  {copy.newCompilation}
                </button>
              ) : undefined
            }
          >
            {canEdit && showCompileForm && (
              <form className="eval-form" onSubmit={createWikiCompilation} noValidate>
                <p className="eval-form-title">{copy.compileSnapshotTitle}</p>
                <div className="form-grid">
                  <label>
                    {copy.modelProfile}
                    <Select
                      value={compileModelId}
                      onChange={setCompileModelId}
                      options={enabledModelOptions}
                      placeholder={
                        models.loading ? copy.loadingProfiles : copy.configureModel
                      }
                      disabled={models.loading}
                      ariaLabel={copy.compileModelProfileAria}
                    />
                  </label>
                  <label>
                    {copy.sourceSnapshot}
                    <Select
                      value={sourceSnapshotId}
                      onChange={setSourceSnapshotId}
                      options={snapshotOptions}
                      disabled={snapshots.loading}
                      ariaLabel={copy.wikiSourceSnapshotAria}
                    />
                  </label>
                  <label>
                    {copy.outputContract}
                    <Select
                      value={outputMode}
                      onChange={(value) =>
                        setOutputMode(value as "PROMPT_JSON" | "JSON_SCHEMA")
                      }
                      options={[
                        { value: "PROMPT_JSON", label: copy.promptJson },
                        { value: "JSON_SCHEMA", label: copy.nativeJson },
                      ]}
                      ariaLabel={copy.compilationOutputModeAria}
                    />
                  </label>
                  <label>
                    {copy.conflictReview}
                    <Select
                      value={reviewMode}
                      onChange={(value) => setReviewMode(value as "BASIC" | "MODEL")}
                      options={[
                        { value: "BASIC", label: copy.basicReview },
                        { value: "MODEL", label: copy.modelReview },
                      ]}
                      ariaLabel={copy.conflictsReviewModeAria}
                    />
                  </label>
                </div>
                {models.error && (
                  <ErrorState
                    code={models.error.code}
                    message={models.error.message || copy.loadModels}
                    hint={errorHintKey(models.error) ? t(errorHintKey(models.error)!) : undefined}
                    onRetry={models.reload}
                  />
                )}
                {snapshots.error && (
                  <ErrorState
                    code={snapshots.error.code}
                    message={snapshots.error.message || copy.loadSnapshots}
                    hint={
                      errorHintKey(snapshots.error) ? t(errorHintKey(snapshots.error)!) : undefined
                    }
                    onRetry={snapshots.reload}
                  />
                )}
                <p className="state-hint">
                  {copy.neverPublishAutomatically}
                </p>
                <InlineError
                  error={createMutation.error}
                  fallback={t("errors.requestFailed")}
                />
                <div className="form-actions">
                  <button
                    type="submit"
                    className="button button-primary"
                    disabled={
                      createMutation.pending ||
                      !compileModelId ||
                      !selectedKbId ||
                      models.loading
                    }
                  >
                    {createMutation.pending ? copy.requesting : copy.createCompilation}
                  </button>
                  <button
                    type="button"
                    className="button button-ghost"
                    onClick={() => setShowCompileForm(false)}
                    disabled={createMutation.pending}
                  >
                    {t("session.cancel")}
                  </button>
                </div>
              </form>
            )}

            {compilations.error && (
              <ErrorState
                code={compilations.error.code}
                message={compilations.error.message || copy.loadCompilations}
                hint={
                  errorHintKey(compilations.error)
                    ? t(errorHintKey(compilations.error)!)
                    : undefined
                }
                onRetry={compilations.reload}
              />
            )}
            {compilations.loading && !compilations.error && <LoadingState />}
            {compilations.loaded &&
              !compilations.error &&
              compilationList.length === 0 && (
                <EmptyState
                  title={copy.noCompilations}
                  hint={
                    canEdit
                      ? copy.createCompilationHint
                      : copy.readOnlyCompilationHint
                  }
                />
              )}

            {compilationList.length > 0 && (
              <div className="data-table">
                <table>
                  <thead>
                    <tr>
                      <th scope="col">{t("common.id")}</th>
                      <th scope="col">{t("runs.columns.status")}</th>
                      <th scope="col">{copy.attempts}</th>
                      <th scope="col">{t("common.created")}</th>
                      <th scope="col">{t("common.actions")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {compilationList.map((compilation) => (
                      <tr key={compilation.id}>
                        <td data-label={t("common.id")}>
                          <HashValue value={compilation.id} label={t("common.id")} />
                        </td>
                        <td data-label={t("runs.columns.status")}>
                          <StatusBadge status={compilation.status} />
                        </td>
                        <td data-label={copy.attempts} className="numeric-cell">
                          {formatNumber(compilation.attempt_count)}
                        </td>
                        <td data-label={t("common.created")}>
                          {formatDateTime(compilation.created_at)}
                        </td>
                        <td data-label={t("common.actions")}>
                          <button
                            type="button"
                            className="button button-ghost"
                            onClick={() => setSelectedCompilationId(compilation.id)}
                          >
                            {selectedCompilationId === compilation.id ? copy.selected : copy.review}
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Panel>

          {selectedCompilation ? (
            <Panel
              title={copy.compilationReview}
              eyebrow={`ID ${shortId(selectedCompilation.id)}`}
              ariaLabel={copy.selectedCompilationAria}
              actions={
                canEdit && selectedCompilation.status === "FAILED" ? (
                  <button
                    type="button"
                    className="button button-ghost"
                    onClick={() => void retrySelectedCompilation()}
                    disabled={compilationMutation.pending}
                  >
                    {compilationMutation.pending ? copy.retrying : copy.retryFailed}
                  </button>
                ) : canEdit && selectedCompilation.status === "READY" ? (
                  <button
                    type="button"
                    className="button button-primary"
                    onClick={() => setPublishConfirm(true)}
                    disabled={compilationMutation.pending || publishConfirm}
                  >
                    {copy.publishOpening}
                  </button>
                ) : undefined
              }
            >
              <div className="approval-state-block">
                <div className="approval-facts">
                  <div className="key-value-row">
                    <span>{t("runs.columns.status")}</span>
                    <StatusBadge status={selectedCompilation.status} />
                  </div>
                  <div className="key-value-row">
                    <span>{copy.sourceSnapshot}</span>
                    <HashValue
                      value={selectedCompilation.source_snapshot_id}
                      label={copy.sourceSnapshot}
                    />
                  </div>
                  <div className="key-value-row">
                    <span>{copy.sourceHash}</span>
                    <HashValue
                      value={selectedCompilation.source_snapshot_hash}
                      label={copy.sourceHash}
                    />
                  </div>
                  <div className="key-value-row">
                    <span>{copy.contentHash}</span>
                    <HashValue
                      value={selectedCompilation.content_hash}
                      label={copy.contentHash}
                    />
                  </div>
                  <div className="key-value-row">
                    <span>{copy.published}</span>
                    <span>
                      {selectedCompilation.published_at
                        ? formatDateTime(selectedCompilation.published_at)
                        : t("common.none")}
                    </span>
                  </div>
                </div>
                <p className="approval-state-meta">
                  {statusGuidance(selectedCompilation.status, copy)}
                </p>
                {selectedCompilation.error_code && (
                  <p className="inline-error" role="alert">
                    {copy.failureCode} <code className="state-code">{selectedCompilation.error_code}</code>
                  </p>
                )}
              </div>

              {publishConfirm && (
                <InlineConfirm
                  title={copy.publishTitle}
                  text={copy.publishText}
                  confirmLabel={copy.publishConfirm}
                  pendingLabel={copy.publishing}
                  cancelLabel={t("session.cancel")}
                  onConfirm={() => void publishSelectedCompilation()}
                  onCancel={() => setPublishConfirm(false)}
                  pending={compilationMutation.pending}
                />
              )}
              <InlineError
                error={compilationMutation.error}
                fallback={t("errors.requestFailed")}
              />

              {canInspectSelected && (
                <div
                  className="tab-strip"
                  role="tablist"
                  aria-label={copy.tabsAria}
                >
                  <button
                    type="button"
                    role="tab"
                    aria-selected={activeTab === "pages"}
                    className={activeTab === "pages" ? "button button-primary" : "button button-ghost"}
                    onClick={() => setActiveTab("pages")}
                  >
                    {copy.pagesTab}
                  </button>
                  <button
                    type="button"
                    role="tab"
                    aria-selected={activeTab === "lint"}
                    className={activeTab === "lint" ? "button button-primary" : "button button-ghost"}
                    onClick={() => setActiveTab("lint")}
                  >
                    {copy.lintTab}
                  </button>
                  {selectedCompilation.status === "PUBLISHED" && (
                    <button
                      type="button"
                      role="tab"
                      aria-selected={activeTab === "query"}
                      className={
                        activeTab === "query" ? "button button-primary" : "button button-ghost"
                      }
                      onClick={() => setActiveTab("query")}
                    >
                      {copy.queryTab}
                    </button>
                  )}
                </div>
              )}

              {activeTab === "pages" && canInspectSelected && (
                <section aria-label={copy.pagesAria}>
                  {pages.error && (
                    <ErrorState
                      code={pages.error.code}
                      message={pages.error.message || copy.loadPages}
                      hint={errorHintKey(pages.error) ? t(errorHintKey(pages.error)!) : undefined}
                      onRetry={pages.reload}
                    />
                  )}
                  {pages.loading && !pages.error && <LoadingState />}
                  {pages.loaded && !pages.error && pageList.length === 0 && (
                    <EmptyState
                      title={copy.noPages}
                      hint={copy.noPagesHint}
                    />
                  )}
                  {pageList.length > 0 && (
                    <>
                      <div className="page-toolbar">
                        <div className="form-actions">
                          {pageList.map((page) => (
                            <button
                              key={page.page_key}
                              type="button"
                              className={
                                selectedPage?.page_key === page.page_key
                                  ? "button button-primary"
                                  : "button button-ghost"
                              }
                              onClick={() => setSelectedPageKey(page.page_key)}
                            >
                              {page.title}
                            </button>
                          ))}
                        </div>
                      </div>
                      {selectedPage && (
                        <div className="playground-stage">
                          <div className="playground-stage-header">
                            <div>
                              <h3>{selectedPage.title}</h3>
                              <p className="state-hint">
                                {selectedPage.kind} · {copy.sourceClaims(selectedPage.claims.length)}
                              </p>
                            </div>
                            <HashValue value={selectedPage.page_key} label={copy.pageKey} />
                          </div>
                          <Markdown text={selectedPage.markdown} />
                          {selectedPage.relations.length > 0 && (
                            <p className="evidence-meta">
                              <span>{copy.relations}</span>
                              {selectedPage.relations.map((relation, index) => (
                                <code key={`${relationTarget(relation)}:${index}`}>
                                  {shortId(relationTarget(relation))}
                                </code>
                              ))}
                            </p>
                          )}
                          <details className="advanced-options">
                            <summary>{copy.structuredClaims}</summary>
                            <div className="data-table">
                              <table>
                                <thead>
                                  <tr>
                                    <th scope="col">{copy.quote}</th>
                                    <th scope="col">{copy.predicateValue}</th>
                                    <th scope="col">{copy.sourceRevision}</th>
                                    <th scope="col">{copy.conflict}</th>
                                  </tr>
                                </thead>
                                <tbody>
                                  {selectedPage.claims.map((claim, index) => (
                                    <tr key={`${claim.chunk_id}:${index}`}>
                                      <td data-label={copy.quote}>{claim.quote}</td>
                                      <td data-label={copy.predicateValue}>
                                        <code>{displayValue(claim.predicate)}</code>
                                        <span className="state-hint">
                                          {displayValue(claim.value)}
                                        </span>
                                      </td>
                                      <td data-label={copy.sourceRevision}>
                                        <HashValue
                                          value={claim.document_revision_id}
                                          label={copy.sourceRevision}
                                        />
                                      </td>
                                      <td data-label={copy.conflict}>
                                        {claim.conflict ? copy.needsReview : t("common.none")}
                                      </td>
                                    </tr>
                                  ))}
                                </tbody>
                              </table>
                            </div>
                          </details>
                        </div>
                      )}
                    </>
                  )}
                </section>
              )}

              {activeTab === "lint" && canInspectSelected && (
                <section aria-label={copy.lintAria}>
                  {lint.error && (
                    <ErrorState
                      code={lint.error.code}
                      message={lint.error.message || copy.loadLint}
                      hint={errorHintKey(lint.error) ? t(errorHintKey(lint.error)!) : undefined}
                      onRetry={lint.reload}
                    />
                  )}
                  {lint.loading && !lint.error && <LoadingState />}
                  {lint.loaded && !lint.error && lintIssues.length === 0 && (
                    <p className="inline-notice">
                      {copy.noLint}
                    </p>
                  )}
                  {lintIssues.length > 0 && (
                    <div className="data-table">
                      <table>
                        <thead>
                          <tr>
                            <th scope="col">{copy.severity}</th>
                            <th scope="col">{copy.code}</th>
                            <th scope="col">{copy.pageKeyLabel}</th>
                          </tr>
                        </thead>
                        <tbody>
                          {lintIssues.map((issue, index) => (
                            <tr key={`${issue.code}:${issue.page_key ?? ""}:${index}`}>
                              <td data-label={copy.severity}>
                                <StatusBadge status={issue.severity.toUpperCase()} />
                              </td>
                              <td data-label={copy.code}>
                                <code>{issue.code}</code>
                              </td>
                              <td data-label={copy.pageKeyLabel}>
                                {issue.page_key ? (
                                  <HashValue value={issue.page_key} label={copy.lintPageKey} />
                                ) : (
                                  t("common.none")
                                )}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </section>
              )}

              {activeTab === "query" && selectedCompilation.status === "PUBLISHED" && (
                <section aria-label={copy.queryAria}>
                  <form className="eval-form" onSubmit={submitWikiQuery} noValidate>
                    <div className="form-grid">
                      <label className="query-field">
                        {copy.queryTab}
                        <textarea
                          value={queryInput}
                          onChange={(event) =>
                            invalidateQuery(() => setQueryInput(event.target.value))
                          }
                          maxLength={4000}
                          placeholder={copy.queryPlaceholder}
                        />
                      </label>
                      <label>
                        {copy.routeMode}
                        <Select
                          value={queryMode}
                          onChange={(value) =>
                            invalidateQuery(() => setQueryMode(value as QueryMode))
                          }
                          options={[
                            { value: "AUTO", label: "AUTO" },
                            { value: "WIKI", label: "WIKI" },
                            { value: "RAG", label: "RAG" },
                            { value: "HYBRID", label: "HYBRID" },
                          ]}
                          ariaLabel={copy.queryModeAria}
                        />
                      </label>
                      <label>
                        {copy.intent}
                        <Select
                          value={queryIntent}
                          onChange={(value) =>
                            invalidateQuery(() => setQueryIntent(value as QueryIntent))
                          }
                          options={[
                            { value: "overview", label: copy.intents.overview },
                            { value: "fact", label: copy.intents.fact },
                            { value: "synthesis", label: copy.intents.synthesis },
                          ]}
                          ariaLabel={copy.queryIntentAria}
                        />
                      </label>
                      <label className="rule-checkbox">
                        <input
                          type="checkbox"
                          checked={requireCurrent}
                          onChange={(event) =>
                            invalidateQuery(() => setRequireCurrent(event.target.checked))
                          }
                        />
                        {copy.requireCurrent}
                      </label>
                      <label className="rule-checkbox">
                        <input
                          type="checkbox"
                          checked={generateAnswer}
                          onChange={(event) =>
                            invalidateQuery(() => setGenerateAnswer(event.target.checked))
                          }
                        />
                        {copy.generateAnswer}
                      </label>
                      {generateAnswer && (
                        <label>
                          {copy.answerModel}
                          <Select
                            value={queryModelId}
                            onChange={(value) =>
                              invalidateQuery(() => setQueryModelId(value))
                            }
                            options={enabledModelOptions}
                            placeholder={copy.configureAnswerModel}
                            ariaLabel={copy.answerModelAria}
                          />
                        </label>
                      )}
                    </div>
                    <InlineError
                      error={queryMutation.error}
                      fallback={t("errors.requestFailed")}
                    />
                    <div className="form-actions">
                      <button
                        type="submit"
                        className="button button-primary"
                        disabled={
                          queryMutation.pending ||
                          !queryInput.trim() ||
                          (generateAnswer && !queryModelId)
                        }
                      >
                        {queryMutation.pending ? copy.querying : copy.runQuery}
                      </button>
                    </div>
                  </form>

                  {visibleQuery && (
                    <div className="playground-stage">
                      <div className="playground-overview">
                        <div>
                          <span>{copy.route}</span>
                          <strong>{visibleQuery.route}</strong>
                        </div>
                        <div>
                          <span>{copy.evidence}</span>
                          <strong>{formatNumber(visibleQuery.evidence.length)}</strong>
                        </div>
                        <div>
                          <span>{copy.conflicts}</span>
                          <strong>{formatNumber(visibleQuery.conflicts.length)}</strong>
                        </div>
                      </div>
                      {visibleQuery.insufficient_evidence && (
                        <p className="inline-error">
                          {copy.insufficientEvidence}
                        </p>
                      )}
                      <h3>{copy.answer}</h3>
                      <Markdown
                        text={
                          visibleQuery.generated_answer?.answer ??
                          visibleQuery.answer
                        }
                      />
                      {visibleQuery.evidence.length > 0 && (
                        <div className="data-table">
                          <table>
                            <thead>
                              <tr>
                                <th scope="col">{copy.evidence}</th>
                                <th scope="col">{copy.origin}</th>
                                <th scope="col">{copy.evidenceSourceRevision}</th>
                                <th scope="col">{copy.conflict}</th>
                              </tr>
                            </thead>
                            <tbody>
                              {visibleQuery.evidence.map((evidence, index) => (
                                <EvidenceRow
                                  key={`${evidence.chunk_id}:${index}`}
                                  evidence={evidence}
                                  copy={copy}
                                  noneLabel={t("common.none")}
                                />
                              ))}
                            </tbody>
                          </table>
                        </div>
                      )}
                    </div>
                  )}
                </section>
              )}
            </Panel>
          ) : (
            <Panel title={copy.compilationReview}>
              <EmptyState
                title={copy.noCompilationTitle}
                hint={copy.noCompilationHint}
              />
            </Panel>
          )}
        </>
      )}
    </div>
  );
}

function EvidenceRow({
  evidence,
  copy,
  noneLabel,
}: {
  evidence: WikiEvidence;
  copy: WikiWorkbenchCopy;
  noneLabel: string;
}) {
  return (
    <tr>
      <td data-label={copy.evidence}>{evidence.quote}</td>
      <td data-label={copy.origin}>
        <code>{evidence.origin ?? evidence.source ?? noneLabel}</code>
      </td>
      <td data-label={copy.evidenceSourceRevision}>
        <HashValue
          value={evidence.document_revision_id}
          label={copy.evidenceSourceRevision}
        />
      </td>
      <td data-label={copy.conflict}>
        {evidence.conflict ? copy.needsReview : noneLabel}
      </td>
    </tr>
  );
}
