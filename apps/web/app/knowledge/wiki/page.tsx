"use client";

import "@/app/styles/wiki.css";

import Link from "next/link";
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent as ReactKeyboardEvent,
  type SyntheticEvent,
} from "react";

import HashValue from "@/components/evaluation/hash-value";
import { CatalogPageHeader } from "@/components/catalog/catalog-layout";
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
import type { Locale } from "@/i18n/types";
import { toApiError, type ApiError, type AuthInput } from "@/lib/api/client";
import { listKnowledgeBases, type KnowledgeBase } from "@/lib/api/knowledge";
import { listModelProfiles, type ModelProfile } from "@/lib/api/models";
import {
  createCompilation,
  getCompilationLint,
  getCompilationPages,
  listCompilations,
  publishCompilation,
  queryWikiCompilation,
  retryCompilation,
  type WikiCompilation,
  type WikiLintReport,
  type WikiPage,
  type WikiQueryResult,
} from "@/lib/api/wiki";

const EN_COPY = {
  title: "Wiki workbench",
  lead: "Compile knowledge into reviewable pages, inspect evidence quality, and test answers against a selected compilation.",
  backToKnowledge: "Knowledge bases",
  newCompilation: "New compilation",
  knowledgeBase: "Knowledge base",
  selectKnowledgeBase: "Select a knowledge base",
  loadingKnowledgeBases: "Loading knowledge bases",
  loadKnowledgeBasesFailed: "Knowledge bases could not be loaded.",
  knowledgeBasesHint: "Check the workspace connection, then try again.",
  noKnowledgeBases: "No knowledge bases available",
  noKnowledgeBasesHint: "Create a knowledge base before compiling a Wiki.",
  createKnowledgeBase: "Go to knowledge bases",
  compilations: "Compilations",
  selectKnowledgeBaseHint: "Choose a knowledge base to view its compilation history.",
  loadingCompilations: "Loading compilations",
  loadCompilationsFailed: "Compilations could not be loaded.",
  compilationsHint: "Your selection is still in place. Retry this list without starting a new compilation.",
  noCompilations: "No compilations yet",
  noCompilationsHint: "Create a compilation to extract claims and build structured Wiki pages from this knowledge base.",
  selectCompilation: "Select compilation",
  compilationId: "Compilation ID",
  contentHash: "Content hash",
  compilationError: "Compilation error",
  selectCompilationTitle: "Choose a compilation",
  selectCompilationHint: "Select an entry on the left to review its pages, lint report, and query behavior.",
  compilationDetails: "Compilation details",
  attempts: "Attempts",
  publish: "Publish Wiki",
  publishing: "Publishing",
  retry: "Retry compilation",
  retrying: "Retrying",
  publishFailed: "The Wiki publication could not be completed.",
  retryFailed: "The compilation could not be retried.",
  tabsLabel: "Compilation views",
  pages: "Pages",
  lint: "Lint",
  query: "Query",
  loadingPages: "Loading Wiki pages",
  loadingLint: "Loading lint report",
  pagesFailed: "Wiki pages could not be loaded.",
  lintFailed: "The lint report could not be loaded.",
  subresourceHint: "Retry the current view. No compilation work will be restarted.",
  noPages: "No Wiki pages",
  noPagesHint: "This compilation has not produced any pages.",
  pageList: "Wiki pages",
  slug: "Slug",
  selectPageHint: "Select a page to read it.",
  totalClaims: "Total claims",
  conflicts: "Conflicts",
  ungrounded: "Ungrounded",
  circularDependencies: "Circular links",
  issues: "Issues",
  noLintData: "No lint report",
  noLintDataHint: "Lint data is not available for this compilation.",
  lintClean: "No lint issues found. This compilation meets the current consistency checks.",
  severity: "Severity",
  kind: "Kind",
  page: "Page",
  message: "Message",
  severityError: "Error",
  severityWarning: "Warning",
  queryLabel: "Ask a question about this compilation",
  queryPlaceholder: "Ask about claims, routes, or evidence in this Wiki...",
  queryMode: "Query mode",
  runQuery: "Run query",
  querying: "Running query",
  queryFailed: "The Wiki query could not be completed.",
  route: "Route",
  citations: "Citations",
  evidenceConflictCount: "Conflicts",
  insufficientEvidence: "Evidence is insufficient for a supported answer.",
  supplementalRequired: "Supplemental evidence is required.",
  degraded: "Degraded response",
  answer: "Answer",
  modeAuto: "Automatic",
  modeWiki: "Wiki",
  modeRag: "Raw retrieval",
  modeHybrid: "Hybrid",
  createTitle: "Create Wiki compilation",
  createDescription: "Choose a model profile and how conflicts should be reviewed. This creates an asynchronous compilation job.",
  closeDialog: "Close dialog",
  modelProfile: "Model profile",
  selectModelProfile: "Select a model profile",
  loadingModels: "Loading model profiles",
  loadModelsFailed: "Model profiles could not be loaded.",
  modelsHint: "Configure or enable a model profile, then try again.",
  noModels: "No model profiles available",
  noModelsHint: "Add an enabled model profile in settings before creating a compilation.",
  openModelSettings: "Open settings",
  conflictReview: "Conflict review",
  reviewBasic: "Basic — deterministic claim comparison",
  reviewModel: "Model — secondary model review",
  cancel: "Cancel",
  startCompilation: "Start compilation",
  startingCompilation: "Starting compilation",
  createFailed: "The compilation could not be started.",
};

type WikiCopy = { [Key in keyof typeof EN_COPY]: string };

const ZH_COPY: WikiCopy = {
  title: "Wiki 工作台",
  lead: "将知识编译为可审阅页面，检查证据质量，并针对选定编译测试回答。",
  backToKnowledge: "知识库",
  newCompilation: "新建编译",
  knowledgeBase: "知识库",
  selectKnowledgeBase: "选择知识库",
  loadingKnowledgeBases: "正在加载知识库",
  loadKnowledgeBasesFailed: "知识库加载失败。",
  knowledgeBasesHint: "请检查工作区连接后重试。",
  noKnowledgeBases: "暂无可用知识库",
  noKnowledgeBasesHint: "请先创建知识库，再编译 Wiki。",
  createKnowledgeBase: "前往知识库",
  compilations: "编译记录",
  selectKnowledgeBaseHint: "选择知识库后可查看其编译历史。",
  loadingCompilations: "正在加载编译记录",
  loadCompilationsFailed: "编译记录加载失败。",
  compilationsHint: "当前选择仍会保留。可以重试列表，不会启动新的编译。",
  noCompilations: "暂无编译记录",
  noCompilationsHint: "创建编译后，系统会从该知识库提取主张并生成结构化 Wiki 页面。",
  selectCompilation: "选择编译",
  compilationId: "编译 ID",
  contentHash: "内容哈希",
  compilationError: "编译错误",
  selectCompilationTitle: "选择一个编译",
  selectCompilationHint: "从左侧选择一条记录，查看页面、检查报告和查询表现。",
  compilationDetails: "编译详情",
  attempts: "尝试次数",
  publish: "发布 Wiki",
  publishing: "正在发布",
  retry: "重试编译",
  retrying: "正在重试",
  publishFailed: "Wiki 发布未能完成。",
  retryFailed: "编译重试未能完成。",
  tabsLabel: "编译视图",
  pages: "页面",
  lint: "检查",
  query: "查询",
  loadingPages: "正在加载 Wiki 页面",
  loadingLint: "正在加载检查报告",
  pagesFailed: "Wiki 页面加载失败。",
  lintFailed: "检查报告加载失败。",
  subresourceHint: "请重试当前视图，不会重新启动编译任务。",
  noPages: "暂无 Wiki 页面",
  noPagesHint: "该编译尚未生成任何页面。",
  pageList: "Wiki 页面",
  slug: "路径",
  selectPageHint: "选择一个页面开始阅读。",
  totalClaims: "主张总数",
  conflicts: "冲突",
  ungrounded: "缺少依据",
  circularDependencies: "循环链接",
  issues: "问题",
  noLintData: "暂无检查报告",
  noLintDataHint: "当前编译没有可用的检查数据。",
  lintClean: "未发现检查问题。该编译已通过当前一致性检查。",
  severity: "级别",
  kind: "类型",
  page: "页面",
  message: "说明",
  severityError: "错误",
  severityWarning: "警告",
  queryLabel: "询问该编译中的内容",
  queryPlaceholder: "询问 Wiki 中的主张、路由或证据…",
  queryMode: "查询模式",
  runQuery: "执行查询",
  querying: "正在查询",
  queryFailed: "Wiki 查询未能完成。",
  route: "路由",
  citations: "引用",
  evidenceConflictCount: "冲突",
  insufficientEvidence: "现有证据不足以支持回答。",
  supplementalRequired: "还需要补充证据。",
  degraded: "降级回答",
  answer: "回答",
  modeAuto: "自动",
  modeWiki: "Wiki",
  modeRag: "原始检索",
  modeHybrid: "混合",
  createTitle: "新建 Wiki 编译",
  createDescription: "选择模型配置与冲突审查方式。提交后将创建一个异步编译任务。",
  closeDialog: "关闭对话框",
  modelProfile: "模型配置",
  selectModelProfile: "选择模型配置",
  loadingModels: "正在加载模型配置",
  loadModelsFailed: "模型配置加载失败。",
  modelsHint: "请配置或启用模型配置后重试。",
  noModels: "暂无模型配置",
  noModelsHint: "请先在设置中添加并启用模型配置，再新建编译。",
  openModelSettings: "打开设置",
  conflictReview: "冲突审查",
  reviewBasic: "基础 — 确定性的主张比较",
  reviewModel: "模型 — 由第二个模型复核",
  cancel: "取消",
  startCompilation: "开始编译",
  startingCompilation: "正在启动编译",
  createFailed: "编译未能启动。",
};

const COPY = {
  "en-US": EN_COPY,
  "zh-CN": ZH_COPY,
} satisfies Record<Locale, WikiCopy>;

type Tab = "pages" | "lint" | "query";
type QueryMode = "AUTO" | "WIKI" | "RAG" | "HYBRID";
type ReviewMode = "BASIC" | "MODEL";

const TAB_ORDER: readonly Tab[] = ["pages", "lint", "query"];
const QUERY_MODES: readonly QueryMode[] = ["AUTO", "WIKI", "RAG", "HYBRID"];

function isQueryMode(value: string): value is QueryMode {
  return QUERY_MODES.some((mode) => mode === value);
}

function handleTabKeyDown(
  event: ReactKeyboardEvent<HTMLButtonElement>,
  current: Tab,
  onChange: (tab: Tab) => void,
) {
  let next: Tab | null = null;
  if (event.key === "ArrowRight") next = TAB_ORDER[(TAB_ORDER.indexOf(current) + 1) % TAB_ORDER.length];
  if (event.key === "ArrowLeft") next = TAB_ORDER[(TAB_ORDER.indexOf(current) - 1 + TAB_ORDER.length) % TAB_ORDER.length];
  if (event.key === "Home") next = TAB_ORDER[0];
  if (event.key === "End") next = TAB_ORDER[TAB_ORDER.length - 1];
  if (!next) return;

  event.preventDefault();
  const tabs = event.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>('[role="tab"]');
  onChange(next);
  tabs?.item(TAB_ORDER.indexOf(next))?.focus();
}

/** Outer gate keeps every workbench query behind the active workspace session. */
export default function WikiWorkbenchPage() {
  const { locale } = useI18n();
  const { connected, workspaceId, sessionId } = useFrontendSession();
  const copy = COPY[locale];

  if (!connected) {
    return (
      <div className="page wiki-page">
        <CatalogPageHeader title={copy.title} description={copy.lead} />
        <SessionRequired contextKey="session.context.knowledge" />
      </div>
    );
  }

  return <WikiWorkbench key={`${workspaceId}:${sessionId}`} locale={locale} />;
}

function WikiWorkbench({ locale }: { locale: Locale }) {
  const copy = COPY[locale];
  const { formatDateTime } = useI18n();
  const { workspaceId, accessToken, permissions } = useFrontendSession();
  const canCompile = permissions?.includes("knowledge_edit") === true;
  const dialogRef = useRef<HTMLDialogElement>(null);

  const [selectedKbId, setSelectedKbId] = useState("");
  const [selectedCompId, setSelectedCompId] = useState<string | null>(null);
  const [createdCompilation, setCreatedCompilation] = useState<WikiCompilation | null>(null);
  const [compileModelId, setCompileModelId] = useState("");
  const [compileReviewMode, setCompileReviewMode] = useState<ReviewMode>("BASIC");

  const loadKbs = useCallback((auth: AuthInput) => listKnowledgeBases(auth), []);
  const kbs = useWorkspaceData<KnowledgeBase[]>(loadKbs, `knowledge-bases:${workspaceId}`);

  const loadModels = useCallback((auth: AuthInput) => listModelProfiles(auth), []);
  const models = useWorkspaceData<ModelProfile[]>(loadModels, `model-profiles:${workspaceId}`);

  const loadCompilations = useCallback(
    (auth: AuthInput) => (selectedKbId ? listCompilations(auth, selectedKbId) : Promise.resolve([])),
    [selectedKbId],
  );
  const compilations = useWorkspaceData<WikiCompilation[]>(
    loadCompilations,
    `wiki-compilations:${workspaceId}:${selectedKbId}`,
    { enabled: Boolean(selectedKbId) },
  );
  const createMutation = useWorkspaceMutation(`wiki-create:${workspaceId}:${selectedKbId}`);

  useEffect(() => {
    if (!kbs.data) return;
    if (selectedKbId && kbs.data.some((kb) => kb.id === selectedKbId)) return;
    setSelectedKbId(kbs.data[0]?.id ?? "");
    setSelectedCompId(null);
    setCreatedCompilation(null);
  }, [kbs.data, selectedKbId]);

  useEffect(() => {
    if (!models.data?.length) {
      if (compileModelId) setCompileModelId("");
      return;
    }
    if (models.data.some((profile) => profile.id === compileModelId)) return;
    const preferred = models.data.find((profile) => profile.enabled) ?? models.data[0];
    setCompileModelId(preferred.id);
  }, [compileModelId, models.data]);

  function handleKnowledgeBaseChange(next: string) {
    setSelectedKbId(next);
    setSelectedCompId(null);
    setCreatedCompilation(null);
  }

  function openCreateDialog() {
    if (!canCompile) return;
    const dialog = dialogRef.current;
    if (!dialog || dialog.open) return;
    createMutation.clearError();
    if (models.error) models.reload();
    dialog.showModal();
    requestAnimationFrame(() => {
      dialog.querySelector<HTMLElement>("button.select-control")?.focus();
    });
  }

  function closeCreateDialog() {
    const dialog = dialogRef.current;
    if (dialog?.open) dialog.close();
  }

  function handleDialogCancel(event: SyntheticEvent<HTMLDialogElement>) {
    event.preventDefault();
    if (!createMutation.pending) closeCreateDialog();
  }

  function handleDialogClose() {
    createMutation.clearError();
  }

  async function handleCreateCompilation(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canCompile || !selectedKbId || !compileModelId || createMutation.pending) return;

    const created = await createMutation.run((auth) =>
      createCompilation(auth, selectedKbId, {
        model_profile_id: compileModelId,
        review_mode: compileReviewMode,
      }),
    );
    if (!created) return;

    setCreatedCompilation(created);
    setSelectedCompId(created.id);
    compilations.reload();
    closeCreateDialog();
  }

  function handleCompilationChanged(updated: WikiCompilation) {
    setCreatedCompilation(updated);
    compilations.reload();
  }

  const kbList = kbs.data ?? [];
  const compList = compilations.data ?? [];
  const listedCompilation = compList.find((compilation) => compilation.id === selectedCompId) ?? null;
  const selectedComp =
    listedCompilation ??
    (createdCompilation?.id === selectedCompId ? createdCompilation : null);
  const modelOptions = (models.data ?? []).map((profile) => ({
    value: profile.id,
    label: profile.model,
  }));

  return (
    <div className="page wiki-page">
      <CatalogPageHeader
        title={copy.title}
        description={copy.lead}
        actions={
          <>
            <Link className="button button-ghost" href="/knowledge">
              {copy.backToKnowledge}
            </Link>
            <button
              type="button"
              className="button button-primary"
              disabled={!canCompile || !selectedKbId}
              title={!canCompile ? (locale === "zh-CN" ? "当前角色不能编译 Wiki 知识" : "Your role cannot compile Wiki knowledge") : undefined}
              onClick={openCreateDialog}
            >
              {copy.newCompilation}
            </button>
          </>
        }
      />

      {kbs.error ? (
        <ErrorState
          code={kbs.error.code}
          message={kbs.error.message || copy.loadKnowledgeBasesFailed}
          hint={copy.knowledgeBasesHint}
          onRetry={kbs.reload}
        />
      ) : kbs.loading && !kbs.data ? (
        <LoadingState label={copy.loadingKnowledgeBases} />
      ) : kbs.loaded && kbList.length === 0 ? (
        <EmptyState
          title={copy.noKnowledgeBases}
          hint={copy.noKnowledgeBasesHint}
          actions={
            <Link className="button button-primary" href="/knowledge">
              {copy.createKnowledgeBase}
            </Link>
          }
        />
      ) : (
        <>
          <section className="wiki-context-bar" aria-label={copy.knowledgeBase}>
            <label className="wiki-context-field">
              <span>{copy.knowledgeBase}</span>
              <Select
                value={selectedKbId}
                onChange={handleKnowledgeBaseChange}
                options={kbList.map((kb) => ({ value: kb.id, label: kb.name }))}
                placeholder={copy.selectKnowledgeBase}
                ariaLabel={copy.knowledgeBase}
              />
            </label>
          </section>

          <div className="wiki-workbench-grid">
            <Panel
              title={copy.compilations}
              actions={
                <span className="wiki-count" aria-label={`${copy.compilations}: ${compList.length}`}>
                  {compList.length}
                </span>
              }
            >
              {!selectedKbId ? (
                <EmptyState title={copy.selectKnowledgeBase} hint={copy.selectKnowledgeBaseHint} />
              ) : compilations.error ? (
                <ErrorState
                  code={compilations.error.code}
                  message={compilations.error.message || copy.loadCompilationsFailed}
                  hint={copy.compilationsHint}
                  onRetry={compilations.reload}
                />
              ) : compilations.loading ? (
                <LoadingState label={copy.loadingCompilations} />
              ) : compList.length === 0 ? (
                <EmptyState title={copy.noCompilations} hint={copy.noCompilationsHint} />
              ) : (
                <ul className="wiki-compilation-list">
                  {compList.map((compilation) => {
                    const isSelected = compilation.id === selectedCompId;
                    return (
                      <li
                        key={compilation.id}
                        className={`wiki-compilation-item${isSelected ? " wiki-compilation-item-selected" : ""}`}
                      >
                        <button
                          type="button"
                          className="wiki-compilation-select"
                          aria-pressed={isSelected}
                          aria-label={`${copy.selectCompilation}: ${compilation.id.slice(0, 8)}`}
                          onClick={() => setSelectedCompId(compilation.id)}
                        >
                          <span className="wiki-compilation-status-row">
                            <StatusBadge status={compilation.status} />
                            <span className="wiki-compilation-date">
                              {formatDateTime(compilation.created_at)}
                            </span>
                          </span>
                          <span className="wiki-compilation-id">
                            {copy.compilationId} <code>{compilation.id.slice(0, 8)}…</code>
                          </span>
                          {compilation.error_code ? (
                            <span className="wiki-compilation-error">
                              {copy.compilationError}: <code>{compilation.error_code}</code>
                            </span>
                          ) : null}
                        </button>
                        {compilation.content_hash ? (
                          <div className="wiki-compilation-hash">
                            <span>{copy.contentHash}</span>
                            <HashValue value={compilation.content_hash} label={copy.contentHash} />
                          </div>
                        ) : null}
                      </li>
                    );
                  })}
                </ul>
              )}
            </Panel>

            {selectedComp ? (
              <CompilationWorkspace
                key={`${selectedKbId}:${selectedComp.id}`}
                knowledgeBaseId={selectedKbId}
                compilation={selectedComp}
                copy={copy}
                onCompilationChanged={handleCompilationChanged}
              />
            ) : (
              <Panel title={copy.selectCompilationTitle}>
                <EmptyState title={copy.selectCompilation} hint={copy.selectCompilationHint} />
              </Panel>
            )}
          </div>
        </>
      )}

      <dialog
        ref={dialogRef}
        className="wiki-create-dialog"
        aria-labelledby="wiki-create-title"
        aria-describedby="wiki-create-description"
        onCancel={handleDialogCancel}
        onClose={handleDialogClose}
      >
        <div className="wiki-dialog-header">
          <div>
            <h2 id="wiki-create-title">{copy.createTitle}</h2>
            <p id="wiki-create-description" className="wiki-dialog-description">
              {copy.createDescription}
            </p>
          </div>
          <button
            type="button"
            className="button button-ghost wiki-dialog-close"
            onClick={closeCreateDialog}
            disabled={createMutation.pending}
            aria-label={copy.closeDialog}
          >
            {copy.closeDialog}
          </button>
        </div>

        <form className="wiki-dialog-form" onSubmit={handleCreateCompilation}>
          {models.error ? (
            <ErrorState
              code={models.error.code}
              message={models.error.message || copy.loadModelsFailed}
              hint={copy.modelsHint}
              onRetry={models.reload}
            />
          ) : models.loading && !models.data ? (
            <LoadingState label={copy.loadingModels} />
          ) : modelOptions.length === 0 ? (
            <EmptyState
              title={copy.noModels}
              hint={copy.noModelsHint}
              actions={
                <Link className="button button-ghost" href="/settings">
                  {copy.openModelSettings}
                </Link>
              }
            />
          ) : (
            <div className="wiki-dialog-field">
              <span className="wiki-field-label">{copy.modelProfile}</span>
              <Select
                value={compileModelId}
                onChange={setCompileModelId}
                options={modelOptions}
                placeholder={copy.selectModelProfile}
                ariaLabel={copy.modelProfile}
              />
            </div>
          )}

          <div className="wiki-dialog-field">
            <span className="wiki-field-label">{copy.conflictReview}</span>
            <Select
              value={compileReviewMode}
              onChange={(value) => setCompileReviewMode(value === "MODEL" ? "MODEL" : "BASIC")}
              options={[
                { value: "BASIC", label: copy.reviewBasic },
                { value: "MODEL", label: copy.reviewModel },
              ]}
              ariaLabel={copy.conflictReview}
            />
          </div>

          <InlineError error={createMutation.error} fallback={copy.createFailed} />

          <div className="wiki-dialog-actions">
            <button
              type="button"
              className="button button-ghost"
              onClick={closeCreateDialog}
              disabled={createMutation.pending}
            >
              {copy.cancel}
            </button>
            <button
              type="submit"
              className="button button-primary"
              disabled={!canCompile || !compileModelId || createMutation.pending || Boolean(models.error)}
            >
              {createMutation.pending ? copy.startingCompilation : copy.startCompilation}
            </button>
          </div>
        </form>
      </dialog>
    </div>
  );
}

function CompilationWorkspace({
  knowledgeBaseId,
  compilation,
  copy,
  onCompilationChanged,
}: {
  knowledgeBaseId: string;
  compilation: WikiCompilation;
  copy: WikiCopy;
  onCompilationChanged: (compilation: WikiCompilation) => void;
}) {
  const { formatDateTime } = useI18n();
  const { workspaceId, accessToken } = useFrontendSession();
  const mutation = useWorkspaceMutation(`wiki-compilation-action:${knowledgeBaseId}:${compilation.id}`);
  const queryRequestRef = useRef(0);

  const [localMutation, setLocalMutation] = useState<{
    base: WikiCompilation;
    updated: WikiCompilation;
  } | null>(null);
  const [activeTab, setActiveTab] = useState<Tab>("pages");
  const [pages, setPages] = useState<WikiPage[]>([]);
  const [selectedPage, setSelectedPage] = useState<WikiPage | null>(null);
  const [lintReport, setLintReport] = useState<WikiLintReport | null>(null);
  const [subLoading, setSubLoading] = useState(true);
  const [subError, setSubError] = useState<ApiError | null>(null);
  const [subresourceRetry, setSubresourceRetry] = useState(0);
  const [pendingAction, setPendingAction] = useState<"publish" | "retry" | null>(null);
  const [actionKind, setActionKind] = useState<"publish" | "retry">("publish");

  const [queryInput, setQueryInput] = useState("");
  const [queryMode, setQueryMode] = useState<QueryMode>("AUTO");
  const [queryResult, setQueryResult] = useState<WikiQueryResult | null>(null);
  const [querying, setQuerying] = useState(false);
  const [queryError, setQueryError] = useState<ApiError | null>(null);

  const displayCompilation = localMutation?.base === compilation ? localMutation.updated : compilation;

  useEffect(() => {
    if (activeTab === "query") {
      setSubLoading(false);
      setSubError(null);
      return;
    }

    let cancelled = false;
    setSubLoading(true);
    setSubError(null);
    const auth = { workspaceId, accessToken };

    void (async () => {
      try {
        if (activeTab === "pages") {
          const items = await getCompilationPages(auth, knowledgeBaseId, compilation.id);
          if (cancelled) return;
          setPages(items);
          setSelectedPage((current) => items.find((page) => page.id === current?.id) ?? items[0] ?? null);
        } else {
          const report = await getCompilationLint(auth, knowledgeBaseId, compilation.id);
          if (cancelled) return;
          setLintReport(report);
        }
      } catch (caught) {
        if (cancelled) return;
        setSubError(
          toApiError(caught, activeTab === "pages" ? copy.pagesFailed : copy.lintFailed),
        );
      } finally {
        if (!cancelled) setSubLoading(false);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [
    accessToken,
    activeTab,
    compilation.id,
    copy.lintFailed,
    copy.pagesFailed,
    knowledgeBaseId,
    subresourceRetry,
    workspaceId,
  ]);

  useEffect(() => () => {
    queryRequestRef.current += 1;
  }, []);

  function changeTab(next: Tab) {
    setActiveTab(next);
    if (next === "query") {
      setSubLoading(false);
      setSubError(null);
    } else {
      setSubLoading(true);
      setSubError(null);
    }
  }

  async function handlePublish() {
    if (mutation.pending) return;
    setActionKind("publish");
    setPendingAction("publish");
    try {
      const updated = await mutation.run((auth) =>
        publishCompilation(auth, knowledgeBaseId, compilation.id),
      );
      if (updated) {
        setLocalMutation({ base: compilation, updated });
        onCompilationChanged(updated);
        setActionKind("publish");
      }
    } finally {
      setPendingAction(null);
    }
  }

  async function handleRetry() {
    if (mutation.pending) return;
    setActionKind("retry");
    setPendingAction("retry");
    try {
      const updated = await mutation.run((auth) =>
        retryCompilation(auth, knowledgeBaseId, compilation.id),
      );
      if (updated) {
        setLocalMutation({ base: compilation, updated });
        onCompilationChanged(updated);
        setActionKind("retry");
      }
    } finally {
      setPendingAction(null);
    }
  }

  async function handleRunQuery(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const query = queryInput.trim();
    if (!query || querying) return;

    const request = ++queryRequestRef.current;
    setQuerying(true);
    setQueryError(null);
    setQueryResult(null);
    try {
      const result = await queryWikiCompilation(
        { workspaceId, accessToken },
        knowledgeBaseId,
        compilation.id,
        {
          query,
          source_snapshot_id: displayCompilation.source_snapshot_id || "LATEST",
          mode: queryMode,
        },
      );
      if (request === queryRequestRef.current) setQueryResult(result);
    } catch (caught) {
      if (request === queryRequestRef.current) {
        setQueryError(toApiError(caught, copy.queryFailed));
      }
    } finally {
      if (request === queryRequestRef.current) setQuerying(false);
    }
  }

  const queryModeOptions = QUERY_MODES.map((mode) => ({
    value: mode,
    label:
      mode === "AUTO"
        ? copy.modeAuto
        : mode === "WIKI"
          ? copy.modeWiki
          : mode === "RAG"
            ? copy.modeRag
            : copy.modeHybrid,
  }));
  const metrics = lintReport
    ? [
        { label: copy.totalClaims, value: lintReport.total_claims },
        { label: copy.conflicts, value: lintReport.total_conflicts },
        { label: copy.ungrounded, value: lintReport.ungrounded_claims },
        { label: copy.circularDependencies, value: lintReport.circular_dependencies },
      ]
    : [];

  return (
    <div className="wiki-detail-stack">
      <Panel title={copy.compilationDetails}>
        <div className="wiki-detail-summary">
          <div className="wiki-detail-meta">
            <StatusBadge status={displayCompilation.status} />
            <span>{formatDateTime(displayCompilation.created_at)}</span>
            <span>
              {copy.attempts}: <strong>{displayCompilation.attempt_count}</strong>
            </span>
            {displayCompilation.content_hash ? (
              <span className="wiki-detail-hash">
                {copy.contentHash}{" "}
                <HashValue value={displayCompilation.content_hash} label={copy.contentHash} />
              </span>
            ) : null}
          </div>
          {displayCompilation.status === "SUCCEEDED" ? (
            <button
              type="button"
              className="button button-primary"
              disabled={mutation.pending}
              onClick={handlePublish}
            >
              {pendingAction === "publish" ? copy.publishing : copy.publish}
            </button>
          ) : displayCompilation.status === "FAILED" ? (
            <button
              type="button"
              className="button button-ghost"
              disabled={mutation.pending}
              onClick={handleRetry}
            >
              {pendingAction === "retry" ? copy.retrying : copy.retry}
            </button>
          ) : null}
        </div>

        <InlineError
          error={mutation.error}
          fallback={actionKind === "retry" ? copy.retryFailed : copy.publishFailed}
        />

        <div className="wiki-tabs" role="tablist" aria-label={copy.tabsLabel}>
          <button
            id="wiki-tab-pages"
            type="button"
            role="tab"
            aria-selected={activeTab === "pages"}
            aria-controls="wiki-panel-pages"
            tabIndex={activeTab === "pages" ? 0 : -1}
            className={`wiki-tab${activeTab === "pages" ? " wiki-tab-active" : ""}`}
            onClick={() => changeTab("pages")}
            onKeyDown={(event) => handleTabKeyDown(event, "pages", changeTab)}
          >
            {copy.pages} ({pages.length})
          </button>
          <button
            id="wiki-tab-lint"
            type="button"
            role="tab"
            aria-selected={activeTab === "lint"}
            aria-controls="wiki-panel-lint"
            tabIndex={activeTab === "lint" ? 0 : -1}
            className={`wiki-tab${activeTab === "lint" ? " wiki-tab-active" : ""}`}
            onClick={() => changeTab("lint")}
            onKeyDown={(event) => handleTabKeyDown(event, "lint", changeTab)}
          >
            {copy.lint}
          </button>
          <button
            id="wiki-tab-query"
            type="button"
            role="tab"
            aria-selected={activeTab === "query"}
            aria-controls="wiki-panel-query"
            tabIndex={activeTab === "query" ? 0 : -1}
            className={`wiki-tab${activeTab === "query" ? " wiki-tab-active" : ""}`}
            onClick={() => changeTab("query")}
            onKeyDown={(event) => handleTabKeyDown(event, "query", changeTab)}
          >
            {copy.query}
          </button>
        </div>

        {subLoading && activeTab !== "query" ? (
          <LoadingState label={activeTab === "pages" ? copy.loadingPages : copy.loadingLint} />
        ) : subError && activeTab !== "query" ? (
          <ErrorState
            code={subError.code}
            message={subError.message}
            hint={copy.subresourceHint}
            onRetry={() => setSubresourceRetry((value) => value + 1)}
          />
        ) : null}

        <div
          id="wiki-panel-pages"
          role="tabpanel"
          aria-labelledby="wiki-tab-pages"
          hidden={activeTab !== "pages"}
          className="wiki-tab-panel"
        >
          {!subLoading && !subError ? (
            pages.length === 0 ? (
              <EmptyState title={copy.noPages} hint={copy.noPagesHint} />
            ) : (
              <div className="wiki-pages-grid">
                <nav className="wiki-page-nav" aria-label={copy.pageList}>
                  <ul className="wiki-page-list">
                    {pages.map((page) => (
                      <li key={page.id}>
                        <button
                          type="button"
                          className={`wiki-page-button${selectedPage?.id === page.id ? " wiki-page-button-active" : ""}`}
                          aria-pressed={selectedPage?.id === page.id}
                          onClick={() => setSelectedPage(page)}
                        >
                          <span>{page.title || page.slug}</span>
                          <code>{page.slug}</code>
                        </button>
                      </li>
                    ))}
                  </ul>
                </nav>
                <article className="wiki-reader" aria-live="polite">
                  {selectedPage ? (
                    <>
                      <header className="wiki-reader-header">
                        <h3>{selectedPage.title || selectedPage.slug}</h3>
                        <p>
                          {copy.slug}: <code>{selectedPage.slug}</code>
                        </p>
                      </header>
                      <div className="wiki-reader-divider" />
                      <Markdown text={selectedPage.body} />
                    </>
                  ) : (
                    <p className="state-hint">{copy.selectPageHint}</p>
                  )}
                </article>
              </div>
            )
          ) : null}
        </div>

        <div
          id="wiki-panel-lint"
          role="tabpanel"
          aria-labelledby="wiki-tab-lint"
          hidden={activeTab !== "lint"}
          className="wiki-tab-panel"
        >
          {!subLoading && !subError ? (
            lintReport ? (
              <div className="wiki-lint">
                <dl className="wiki-lint-metrics">
                  {metrics.map((metric) => (
                    <div key={metric.label} className="wiki-lint-metric">
                      <dt>{metric.label}</dt>
                      <dd>{metric.value}</dd>
                    </div>
                  ))}
                </dl>
                <div className="wiki-lint-issues-heading">
                  <h3>
                    {copy.issues} ({lintReport.issues.length})
                  </h3>
                </div>
                {lintReport.issues.length === 0 ? (
                  <p className="wiki-success-note" role="status">
                    {copy.lintClean}
                  </p>
                ) : (
                  <div className="wiki-table-scroll" tabIndex={0}>
                    <table className="wiki-lint-table">
                      <caption className="sr-only">{copy.issues}</caption>
                      <thead>
                        <tr>
                          <th scope="col">{copy.severity}</th>
                          <th scope="col">{copy.kind}</th>
                          <th scope="col">{copy.page}</th>
                          <th scope="col">{copy.message}</th>
                        </tr>
                      </thead>
                      <tbody>
                        {lintReport.issues.map((issue, index) => (
                          <tr key={`${issue.kind}:${issue.page_slug ?? ""}:${index}`}>
                            <td>
                              <span className={`wiki-severity wiki-severity-${issue.severity}`}>
                                {issue.severity === "error" ? copy.severityError : copy.severityWarning}
                              </span>
                            </td>
                            <td>{issue.kind}</td>
                            <td>{issue.page_slug || "—"}</td>
                            <td>{issue.message}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            ) : (
              <EmptyState title={copy.noLintData} hint={copy.noLintDataHint} />
            )
          ) : null}
        </div>

        <div
          id="wiki-panel-query"
          role="tabpanel"
          aria-labelledby="wiki-tab-query"
          hidden={activeTab !== "query"}
          className="wiki-tab-panel"
        >
          <form className="wiki-query-form" onSubmit={handleRunQuery}>
            <label className="wiki-field-label" htmlFor="wiki-query-input">
              {copy.queryLabel}
            </label>
            <div className="wiki-query-controls">
              <input
                id="wiki-query-input"
                type="text"
                value={queryInput}
                onChange={(event) => setQueryInput(event.target.value)}
                placeholder={copy.queryPlaceholder}
                disabled={querying}
              />
              <Select
                value={queryMode}
                onChange={(value) => {
                  if (isQueryMode(value)) setQueryMode(value);
                }}
                options={queryModeOptions}
                ariaLabel={copy.queryMode}
                disabled={querying}
              />
              <button
                type="submit"
                className="button button-primary"
                disabled={querying || !queryInput.trim()}
              >
                {querying ? copy.querying : copy.runQuery}
              </button>
            </div>
          </form>

          <InlineError error={queryError} fallback={copy.queryFailed} />

          {queryResult ? (
            <section className="wiki-query-result" aria-live="polite">
              <dl className="wiki-query-summary">
                <div>
                  <dt>{copy.route}</dt>
                  <dd>{queryResult.route}</dd>
                </div>
                <div>
                  <dt>{copy.citations}</dt>
                  <dd>{queryResult.evidence_refs?.length ?? 0}</dd>
                </div>
                <div>
                  <dt>{copy.evidenceConflictCount}</dt>
                  <dd>{queryResult.conflict_count}</dd>
                </div>
              </dl>
              {queryResult.insufficient_evidence ? (
                <p className="wiki-evidence-warning">{copy.insufficientEvidence}</p>
              ) : null}
              {queryResult.supplemental_required ? (
                <p className="wiki-evidence-warning">{copy.supplementalRequired}</p>
              ) : null}
              {queryResult.degraded ? (
                <p className="wiki-degraded">
                  {copy.degraded}
                  {queryResult.degraded_reason ? `: ${queryResult.degraded_reason}` : ""}
                </p>
              ) : null}
              <h3>{copy.answer}</h3>
              <Markdown text={queryResult.answer} />
            </section>
          ) : null}
        </div>
      </Panel>
    </div>
  );
}
