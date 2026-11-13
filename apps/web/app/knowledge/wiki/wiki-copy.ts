import type { WikiCompilationStatus } from "@/lib/api/wiki";

export type WikiWorkbenchCopy = {
  knowledgeWorkbenchLink: string;
  title: string;
  lead: string;
  refreshingSession: string;
  loadingPermissions: string;
  accessRequired: string;
  accessTitle: string;
  accessHint: string;
  knowledgeBase: string;
  selectKnowledgeBase: string;
  wikiKnowledgeBaseAria: string;
  loadBases: string;
  compilations: string;
  wikiCompilationsAria: string;
  newCompilation: string;
  compileSnapshotTitle: string;
  modelProfile: string;
  loadingProfiles: string;
  configureModel: string;
  compileModelProfileAria: string;
  wikiSourceSnapshotAria: string;
  compilationOutputModeAria: string;
  conflictsReviewModeAria: string;
  sourceSnapshot: string;
  latestSnapshot: string;
  snapshotItems: (count: string) => string;
  outputContract: string;
  promptJson: string;
  nativeJson: string;
  conflictReview: string;
  basicReview: string;
  modelReview: string;
  neverPublishAutomatically: string;
  requesting: string;
  createCompilation: string;
  loadModels: string;
  loadSnapshots: string;
  loadCompilations: string;
  noCompilations: string;
  createCompilationHint: string;
  readOnlyCompilationHint: string;
  attempts: string;
  selected: string;
  review: string;
  compilationReview: string;
  selectedCompilationAria: string;
  retrying: string;
  retryFailed: string;
  publishOpening: string;
  sourceHash: string;
  contentHash: string;
  published: string;
  failureCode: string;
  publishTitle: string;
  publishText: string;
  publishConfirm: string;
  publishing: string;
  publicationConfirmed: string;
  tabsAria: string;
  pagesTab: string;
  lintTab: string;
  queryTab: string;
  pagesAria: string;
  loadPages: string;
  noPages: string;
  noPagesHint: string;
  sourceClaims: (count: number) => string;
  pageKey: string;
  relations: string;
  structuredClaims: string;
  quote: string;
  predicateValue: string;
  sourceRevision: string;
  conflict: string;
  needsReview: string;
  lintAria: string;
  loadLint: string;
  noLint: string;
  severity: string;
  code: string;
  pageKeyLabel: string;
  lintPageKey: string;
  queryAria: string;
  queryLabel: string;
  queryPlaceholder: string;
  routeMode: string;
  queryModeAria: string;
  intent: string;
  queryIntentAria: string;
  intents: { overview: string; fact: string; synthesis: string };
  requireCurrent: string;
  generateAnswer: string;
  answerModel: string;
  configureAnswerModel: string;
  answerModelAria: string;
  querying: string;
  runQuery: string;
  route: string;
  evidence: string;
  conflicts: string;
  insufficientEvidence: string;
  answer: string;
  origin: string;
  evidenceSourceRevision: string;
  noCompilationTitle: string;
  noCompilationHint: string;
  modelDisabled: string;
  compilationAccepted: (status: string) => string;
  retryAccepted: (status: string) => string;
  serverStateMismatch: (status: string) => string;
  statusGuidance: Record<WikiCompilationStatus, string>;
};

const ZH_CN: WikiWorkbenchCopy = {
  knowledgeWorkbenchLink: "Wiki 工作台",
  title: "Wiki 工作台",
  lead: "编译有来源依据的页面，检查 lint，显式发布，并基于固定 Wiki 绑定进行查询。",
  refreshingSession: "正在刷新工作区会话…",
  loadingPermissions: "正在加载工作区权限…",
  accessRequired: "需要知识库访问权限",
  accessTitle: "缺少 knowledge_run 权限",
  accessHint: "请联系工作区管理员授予知识库访问权限；服务端会执行同样的权限校验。",
  knowledgeBase: "知识库",
  selectKnowledgeBase: "选择知识库",
  wikiKnowledgeBaseAria: "Wiki 知识库",
  loadBases: "无法加载知识库。",
  compilations: "编译任务",
  wikiCompilationsAria: "Wiki 编译任务",
  newCompilation: "新建 Wiki 编译",
  compileSnapshotTitle: "编译固定的来源快照",
  modelProfile: "模型配置",
  loadingProfiles: "正在加载模型配置…",
  configureModel: "请配置已启用的模型",
  compileModelProfileAria: "编译模型配置",
  wikiSourceSnapshotAria: "Wiki 来源快照",
  compilationOutputModeAria: "编译输出格式",
  conflictsReviewModeAria: "冲突审核模式",
  sourceSnapshot: "来源快照",
  latestSnapshot: "LATEST · 最新服务端快照",
  snapshotItems: (count) => `${count} 项`,
  outputContract: "输出格式",
  promptJson: "提示词 JSON",
  nativeJson: "原生 JSON Schema",
  conflictReview: "冲突审核",
  basicReview: "基础确定性审核",
  modelReview: "模型辅助审核",
  neverPublishAutomatically: "编译不会自动发布；服务端会返回持久任务状态。",
  requesting: "正在提交…",
  createCompilation: "创建 Wiki 编译",
  loadModels: "无法加载模型配置。",
  loadSnapshots: "无法加载来源快照。",
  loadCompilations: "无法加载 Wiki 编译任务。",
  noCompilations: "暂无编译任务",
  createCompilationHint: "请从已就绪的知识快照创建 Wiki 编译。",
  readOnlyCompilationHint: "当前没有可查看的已发布 Wiki 编译。",
  attempts: "尝试次数",
  selected: "已选择",
  review: "查看",
  compilationReview: "编译审核",
  selectedCompilationAria: "当前 Wiki 编译",
  retrying: "正在重试…",
  retryFailed: "重试失败的编译",
  publishOpening: "发布…",
  sourceHash: "来源 hash",
  contentHash: "内容 hash",
  published: "发布时间",
  failureCode: "失败代码",
  publishTitle: "发布此 Wiki 编译？",
  publishText: "发布后会为调用方冻结已审核页面及其来源证据。服务端仍会阻止存在 lint 错误的发布。",
  publishConfirm: "发布",
  publishing: "正在发布…",
  publicationConfirmed: "服务端已确认发布。",
  tabsAria: "Wiki 编译审核区域",
  pagesTab: "页面",
  lintTab: "Lint",
  queryTab: "查询",
  pagesAria: "Wiki 页面",
  loadPages: "无法加载已编译的 Wiki 页面。",
  noPages: "暂无已编译页面",
  noPagesHint: "服务端尚未为此编译生成可审核页面。",
  sourceClaims: (count) => `${count} 条来源主张`,
  pageKey: "Wiki 页面键",
  relations: "关联",
  structuredClaims: "结构化来源主张",
  quote: "原文",
  predicateValue: "谓词 / 值",
  sourceRevision: "来源版本",
  conflict: "冲突",
  needsReview: "需要审核",
  lintAria: "Wiki lint",
  loadLint: "无法加载 Wiki lint 报告。",
  noLint: "服务端未报告此编译的 lint 问题。",
  severity: "严重程度",
  code: "代码",
  pageKeyLabel: "页面键",
  lintPageKey: "Lint 页面键",
  queryAria: "查询已发布 Wiki",
  queryLabel: "查询",
  queryPlaceholder: "针对此固定 Wiki 发布版本提问…",
  routeMode: "路由模式",
  queryModeAria: "Wiki 查询模式",
  intent: "意图",
  queryIntentAria: "Wiki 查询意图",
  intents: { overview: "概览", fact: "事实", synthesis: "综合" },
  requireCurrent: "仅使用当前来源版本",
  generateAnswer: "生成带引用的回答",
  answerModel: "回答模型配置",
  configureAnswerModel: "请配置模型",
  answerModelAria: "Wiki 回答模型配置",
  querying: "正在查询…",
  runQuery: "执行查询",
  route: "路由",
  evidence: "证据",
  conflicts: "冲突",
  insufficientEvidence: "服务端将该回答标记为证据不足。",
  answer: "回答",
  origin: "来源",
  evidenceSourceRevision: "证据来源版本",
  noCompilationTitle: "未选择编译任务",
  noCompilationHint: "请从服务端返回的列表中选择一个编译任务进行审核。",
  modelDisabled: "（已禁用）",
  compilationAccepted: (status) => `编译请求已接受。服务端状态：${status}。`,
  retryAccepted: (status) => `重试请求已接受。服务端状态：${status}。`,
  serverStateMismatch: (status) => `发布请求已返回，但服务端仍报告状态 ${status}。`,
  statusGuidance: {
    PENDING: "已进入编译队列；该状态由服务端维护。",
    PROCESSING: "编译器正在提取并检查有来源依据的主张。",
    READY: "页面与 lint 已可供审核；发布仍需编辑者显式操作。",
    FAILED: "编译失败。重试前请查看稳定的错误代码。",
    PUBLISHED: "该编译已发布且不可变。",
  },
};

const EN_US: WikiWorkbenchCopy = {
  knowledgeWorkbenchLink: "Wiki workbench",
  title: "Wiki workbench",
  lead: "Compile source-backed pages, review lint, publish explicitly, and query a fixed Wiki binding.",
  refreshingSession: "Refreshing workspace session…",
  loadingPermissions: "Loading workspace permissions…",
  accessRequired: "Knowledge access required",
  accessTitle: "knowledge_run is required",
  accessHint: "Ask a workspace administrator for knowledge access. The server enforces the same permission.",
  knowledgeBase: "Knowledge base",
  selectKnowledgeBase: "Select a knowledge base",
  wikiKnowledgeBaseAria: "Wiki knowledge base",
  loadBases: "Could not load knowledge bases.",
  compilations: "Compilations",
  wikiCompilationsAria: "Wiki compilations",
  newCompilation: "New compilation",
  compileSnapshotTitle: "Compile a frozen source snapshot",
  modelProfile: "Model profile",
  loadingProfiles: "Loading profiles…",
  configureModel: "Configure an enabled model profile",
  compileModelProfileAria: "Compilation model profile",
  wikiSourceSnapshotAria: "Wiki source snapshot",
  compilationOutputModeAria: "Compilation output mode",
  conflictsReviewModeAria: "Conflicts review mode",
  sourceSnapshot: "Source snapshot",
  latestSnapshot: "LATEST · newest server snapshot",
  snapshotItems: (count) => `${count} items`,
  outputContract: "Output contract",
  promptJson: "Prompt JSON",
  nativeJson: "Native JSON schema",
  conflictReview: "Conflict review",
  basicReview: "Basic deterministic review",
  modelReview: "Model-assisted review",
  neverPublishAutomatically: "Compilation never publishes automatically. The server returns the durable job state.",
  requesting: "Requesting…",
  createCompilation: "Create compilation",
  loadModels: "Could not load model profiles.",
  loadSnapshots: "Could not load source snapshots.",
  loadCompilations: "Could not load Wiki compilations.",
  noCompilations: "No compilations",
  createCompilationHint: "Create a compilation from a ready knowledge snapshot.",
  readOnlyCompilationHint: "No published or reviewable Wiki compilation is available.",
  attempts: "Attempts",
  selected: "Selected",
  review: "Review",
  compilationReview: "Compilation review",
  selectedCompilationAria: "Selected Wiki compilation",
  retrying: "Retrying…",
  retryFailed: "Retry failed compilation",
  publishOpening: "Publish…",
  sourceHash: "Source hash",
  contentHash: "Content hash",
  published: "Published",
  failureCode: "Failure code",
  publishTitle: "Publish this Wiki compilation?",
  publishText: "Publication freezes the reviewed pages and their source evidence for consumers. Lint errors still block the server-side action.",
  publishConfirm: "Publish",
  publishing: "Publishing…",
  publicationConfirmed: "Publication confirmed by the server.",
  tabsAria: "Wiki compilation review sections",
  pagesTab: "Pages",
  lintTab: "Lint",
  queryTab: "Query",
  pagesAria: "Wiki pages",
  loadPages: "Could not load compiled Wiki pages.",
  noPages: "No compiled pages",
  noPagesHint: "The server has not produced reviewable pages for this compilation.",
  sourceClaims: (count) => `${count} source claims`,
  pageKey: "Wiki page key",
  relations: "Relations",
  structuredClaims: "Structured source claims",
  quote: "Quote",
  predicateValue: "Predicate / value",
  sourceRevision: "Source revision",
  conflict: "Conflict",
  needsReview: "Needs review",
  lintAria: "Wiki lint",
  loadLint: "Could not load the Wiki lint report.",
  noLint: "The server reports no lint issues for this compilation.",
  severity: "Severity",
  code: "Code",
  pageKeyLabel: "Page key",
  lintPageKey: "Lint page key",
  queryAria: "Query published Wiki",
  queryLabel: "Query",
  queryPlaceholder: "Ask against this fixed Wiki publication…",
  routeMode: "Route mode",
  queryModeAria: "Wiki query mode",
  intent: "Intent",
  queryIntentAria: "Wiki query intent",
  intents: { overview: "Overview", fact: "Fact", synthesis: "Synthesis" },
  requireCurrent: "Require current source revision",
  generateAnswer: "Generate a cited answer",
  answerModel: "Answer model profile",
  configureAnswerModel: "Configure a model profile",
  answerModelAria: "Wiki answer model profile",
  querying: "Querying…",
  runQuery: "Run query",
  route: "Route",
  evidence: "Evidence",
  conflicts: "Conflicts",
  insufficientEvidence: "The server marked this answer as insufficiently evidenced.",
  answer: "Answer",
  origin: "Origin",
  evidenceSourceRevision: "Evidence source revision",
  noCompilationTitle: "No compilation selected",
  noCompilationHint: "Select a compilation from the server-backed list to review it.",
  modelDisabled: " (disabled)",
  compilationAccepted: (status) => `Compilation accepted. Server status: ${status}.`,
  retryAccepted: (status) => `Retry accepted. Server status: ${status}.`,
  serverStateMismatch: (status) => `Publication returned, but the server still reports ${status}.`,
  statusGuidance: {
    PENDING: "Queued for compilation. The server owns this state.",
    PROCESSING: "The compiler is extracting and checking source-backed claims.",
    READY: "Pages and lint are ready for review. Publication remains an explicit editor action.",
    FAILED: "Compilation failed. Review the stable error code before retrying.",
    PUBLISHED: "This compilation is published and immutable.",
  },
};

export function wikiWorkbenchCopy(locale: string | undefined): WikiWorkbenchCopy {
  return locale === "zh-CN" ? ZH_CN : EN_US;
}