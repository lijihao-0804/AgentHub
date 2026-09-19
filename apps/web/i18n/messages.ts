import type { FlattenMessageKeys, Locale } from "./types";

/**
 * Canonical English message schema. The zh-CN dictionary below is typed as
 * `MessageSchema`, so the compiler rejects any missing or extra key — both
 * locales must always expose the same key set.
 */
export const enUS = {
  common: {
    loading: "Loading…",
    retry: "Retry",
    technicalDetails: "Technical details",
    refresh: "Refresh",
    unknown: "Unknown",
    utcBucket: "UTC · {bucket}",
    none: "—",
  },
  nav: {
    overview: "Overview",
    knowledge: "Knowledge",
    comingLater: "Coming later",
    dashboard: "Dashboard",
    runs: "Runs",
    approvals: "Approvals",
    retrievalPlayground: "Retrieval Playground",
    evaluations: "Evaluations",
    planned: "Planned",
  },
  brand: {
    subtitle: "Enterprise Agent Runtime & Control Plane",
  },
  shell: {
    menu: "Menu",
    skipToContent: "Skip to content",
    footnote: "Session lives in memory only · no token storage",
    primaryNav: "Primary",
    language: "Language",
    languageEn: "English",
    languageZh: "简体中文",
  },
  session: {
    connect: "Connect workspace",
    change: "Change",
    clear: "Clear session",
    use: "Use session",
    cancel: "Cancel",
    workspaceLabel: "Workspace",
    active: "Session active",
    notConnected: "Not connected",
    panelConnect: "Connect workspace",
    panelSession: "Workspace session",
    workspaceId: "Workspace ID",
    accessToken: "Access token",
    tokenPlaceholder: "Bearer access token",
    replaceTokenPlaceholder: "Enter a new token to replace the current session",
    requiredFields: "Workspace ID and access token are both required.",
    connectedNote:
      "Session active for workspace {id}. The token stays in memory and is cleared when you refresh.",
    notConnectedNote:
      "Connect a workspace session to load dashboards, runs and approvals. Credentials stay in memory only — nothing is persisted.",
    requiredTitle: "Session required",
    requiredHint:
      "Configure a workspace session to view {context}. The session lives in memory only and is cleared on refresh.",
    configure: "Configure session",
    context: {
      home: "the workspace overview",
      dashboard: "the workspace dashboard",
      runs: "the run history",
      run: "this run",
      approvals: "the approval inbox",
      playground: "the retrieval playground",
    },
  },
  home: {
    eyebrow: "Overview",
    title: "Workspace overview",
    lede: "Operational control plane for running, approving and observing enterprise agents.",
    sessionEyebrow: "Session",
    sessionTitle: "Workspace connection",
    quickLinks: "Quick navigation",
    cardCta: "Open",
    cards: {
      dashboard: {
        title: "Dashboard",
        description: "Success rate, latency, usage, cost and failure analytics for this workspace.",
      },
      runs: {
        title: "Runs",
        description: "Workspace-scoped run history with safe status, usage and cost projections.",
      },
      approvals: {
        title: "Approvals",
        description: "Review pending tool approvals and reconcile actions that need attention.",
      },
      playground: {
        title: "Retrieval Playground",
        description: "Inspect dense, sparse, fused and reranked evidence for one knowledge snapshot.",
      },
    },
  },
  status: {
    RUNNING: "Running",
    WAITING_APPROVAL: "Waiting approval",
    SUCCEEDED: "Succeeded",
    FAILED: "Failed",
    NEEDS_ATTENTION: "Needs attention",
    CANCEL_REQUESTED: "Cancel requested",
    CANCELLED: "Cancelled",
    PENDING: "Pending",
    APPROVED: "Approved",
    DENIED: "Denied",
    EXPIRED: "Expired",
    NOT_STARTED: "Not started",
    CLAIMED: "Claimed",
    UNKNOWN_OUTCOME: "Unknown outcome",
    COMPLETED: "Completed",
    tone: {
      neutral: "Status",
      info: "Status: active",
      success: "Status: ok",
      warning: "Status: waiting",
      danger: "Status: failed",
      attention: "Status: needs attention",
    },
  },
  failureCategory: {
    MODEL: "Model",
    ACTION: "Action",
    APPROVAL: "Approval",
    TOOL: "Tool",
    KNOWLEDGE: "Knowledge",
  },
  errors: {
    loadDashboard: "Could not load observability data.",
    loadRuns: "Could not load runs.",
    loadRunDetail: "Could not load run detail.",
    loadApprovals: "Could not load approvals.",
    decideApproval: "Could not record the approval decision.",
    retrieval: "Retrieval failed.",
    hint: {
      sessionInvalid:
        "The workspace session may be missing or the access token is no longer valid. Reconnect the session from the top bar.",
      forbidden: "The current token does not have permission for this workspace resource.",
      notFound: "The resource was not found in this workspace.",
    },
  },
  dashboard: {
    eyebrow: "Observability",
    title: "Workspace dashboard",
    lede: "Bounded success, latency, usage, cost and failure analytics. Percentages always show their sample denominator; raw prompts and tool payloads never enter this view.",
    window: "Window",
    windows: { d1: "24 hours", d7: "7 days", d30: "30 days", d90: "90 days" },
    kpi: {
      successRate: "Success rate",
      successHint: "{numerator} / {denominator} finished",
      p95Latency: "p95 latency",
      p95Hint: "p50 {p50} · {count} samples",
      tokensPerRun: "Tokens / run",
      tokensHint: "{known} known · {unknown} unknown",
      costPerSuccess: "Cost / successful run",
      costHint: "{count} successful cost samples",
    },
    ops: {
      running: "Running",
      waitingApproval: "Waiting approval",
      needsAttention: "Needs attention",
      unknownOutcome: "Unknown outcome",
      unknownOutcomeHint: "Actions whose result could not be confirmed",
    },
    failures: {
      eyebrow: "Failure distribution",
      title: "Failure categories",
      empty: "No failures in this window.",
      emptyHint: "Failed runs and their categories will appear here.",
    },
    cost: {
      eyebrow: "Usage cost",
      title: "Cost by currency",
      mixed: "Mixed",
      mixedNote:
        "Mixed currencies detected — values are grouped per currency and never summed.",
      empty: "No cost data in this window.",
      emptyHint: "Unknown cost is shown as Unknown, never as zero.",
      samples: "{estimated} estimated · {exact} exact",
      perSuccess: "/ success {value}",
    },
    trend: {
      eyebrow: "Trend",
      title: "Runs over time",
      legendSucceeded: "Succeeded",
      legendFailed: "Failed",
      legendNeedsAttention: "Needs attention",
      ariaPrefix: "Runs per bucket:",
      ariaEntry:
        "{date}: {succeeded} succeeded, {failed} failed, {needsAttention} needs attention",
      barTitle:
        "{date} — {runs} runs: {succeeded} succeeded, {failed} failed, {needsAttention} needs attention, {tokens} tokens",
      note: "Peak {peak} runs per bucket. Exact values: {values}",
    },
    versions: {
      eyebrow: "Traffic by version",
      title: "AgentVersion breakdown",
      noRanking: "No winner ranking",
      empty: "No AgentVersion activity.",
      runCountOne: "1 run",
      runCountOther: "{count} runs",
      costUnknown: "cost unknown",
    },
    failureRuns: {
      eyebrow: "Run list",
      title: "Failure runs",
      allCategories: "All categories",
      empty: "No failure runs in this window.",
      emptyHint: "Select a category above to filter.",
      actionPrefix: "action {code}",
    },
  },
  runs: {
    eyebrow: "Runs",
    title: "Runs",
    lede: "Workspace-scoped runtime history with safe status, usage, cost and approval projections.",
    filters: {
      status: "Status",
      allStatuses: "All statuses",
      agentVersionId: "Agent Version ID",
      agentVersionPlaceholder: "Leave empty for all versions",
      apply: "Apply",
      clear: "Clear",
    },
    columns: {
      run: "Run",
      status: "Status",
      version: "Version",
      started: "Started",
      duration: "Duration",
      tokens: "Tokens",
      cost: "Cost",
      tools: "Tools",
      failure: "Failure",
    },
    empty: "No runs found.",
    emptyFilteredHint: "No runs match the current filters. Clear them to see everything.",
    emptyHint: "Runs appear here once agents execute in this workspace.",
    loadMore: "Load more",
  },
  run: {
    eyebrow: "Run detail",
    title: "Run",
    backToRuns: "← Back to runs",
    agentVersionEyebrow: "AgentVersion v{version}",
    waitingCallout: "Waiting for an approval decision before action execution.",
    attentionCallout:
      "Needs attention: the run requires operator follow-up (for example an unconfirmed action outcome).",
    openApprovals: "Open approvals",
    facts: {
      duration: "Duration",
      tokens: "Tokens",
      cost: "Cost",
      modelSteps: "Model steps",
      toolCalls: "Tool calls",
      failure: "Failure",
    },
    approvalsLabel: "Approvals:",
    chips: {
      pending: "Pending {count}",
      approved: "Approved {count}",
      denied: "Denied {count}",
      executionFailed: "Execution failed {count}",
      unknownOutcome: "Unknown outcome {count}",
    },
    notFound: "Run not found.",
    notFoundHint: "This run does not exist in the connected workspace.",
    technicalMetadata: "Technical metadata",
  },
  timeline: {
    eyebrow: "Safe provider-neutral events",
    title: "Timeline",
    empty: "No timeline events.",
    emptyHint: "Events appear once the run executes.",
    kind: {
      RUN_STARTED: "Run started",
      MODEL: "Model step",
      RETRIEVAL: "Retrieval",
      TOOL: "Tool call",
      APPROVAL_WAIT: "Approval required",
      APPROVAL_DECISION: "Approval decision",
      ACTION_EXECUTION: "Action execution",
      RESUME: "Resumed",
      FINISH: "Run completed",
      FAILURE: "Run failed",
    },
    subtitle: {
      decision: "Decision: {value}",
      execution: "Execution: {value}",
      executionUnknown:
        "Execution: UNKNOWN_OUTCOME — the side effect could not be confirmed and needs attention",
      waitingFor: "Waiting for {tool}",
      waitingGeneric: "Waiting for a decision before action execution",
      agentVersion: "AgentVersion {id}",
      failureCode: "Failure code: {code}",
    },
  },
  approvals: {
    eyebrow: "Approvals",
    title: "Approvals",
    lede: "Durable human-in-the-loop decisions for tool actions. Decision and execution states are tracked separately — an approved action can still fail, and an unknown outcome needs attention rather than a retry.",
    inboxEyebrow: "Human-in-the-loop",
    inboxTitle: "Approval inbox",
    loaded: "{count} loaded",
    countsReflect: "Counts reflect the currently loaded list.",
    counts: {
      pending: "Pending {count}",
      approved: "Approved {count}",
      denied: "Denied {count}",
      needsAttention: "Needs attention {count}",
    },
    card: {
      toolIdentity: "Tool identity",
      runLink: "Run {id} →",
      decision: "Decision",
      execution: "Execution",
      decidedBy: "Decided {time} by {user}",
      decidedAt: "Decided {time}",
      requested: "Requested {time}",
      executed: "Executed {time}",
      attempts: "Attempts: {count}",
      unknownWarning:
        "Needs attention: the action ran but its outcome could not be confirmed. Do not retry blindly.",
      failure: "Failure {code} — {message}",
      failureCodeOnly: "Failure {code}",
      approve: "Approve",
      deny: "Deny",
      approving: "Approving…",
      denying: "Denying…",
      technicalArguments: "Technical arguments",
    },
    empty: "No approvals in this workspace.",
    emptyHint:
      "Pending approval requests appear here when an agent requests a risky tool action.",
  },
  playground: {
    eyebrow: "Knowledge",
    title: "Retrieval Playground",
    lede: "Inspect dense, sparse, fused and reranked evidence for one concrete knowledge snapshot. Retrieval runs against the workspace from the active session.",
    queryEyebrow: "Query",
    queryTitle: "Run snapshot-scoped retrieval",
    knowledgeBaseId: "Knowledge Base ID",
    snapshotId: "Snapshot ID",
    query: "Query",
    queryPlaceholder: "Search the selected snapshot…",
    advancedConfig: "Advanced retrieval config",
    run: "Run retrieval",
    running: "Retrieving…",
    initial: "Enter a query and snapshot to begin.",
    invalidRequest: "Complete the required fields first.",
    sessionRequired: "Configure a workspace session to run the playground.",
    empty: "No evidence found for this snapshot.",
    emptyHint: "The retrieval stages ran but returned no chunks.",
    overview: {
      snapshot: "Snapshot",
      totalLatency: "Total latency",
      finalEvidence: "Final evidence",
    },
    stage: {
      eyebrow: "Stage",
      results: "{count} results",
      noResults: "No results",
    },
    evidence: {
      eyebrow: "Final evidence",
      title: "Reranked evidence",
      chunks: "{count} chunks",
    },
  },
};

export type MessageSchema = typeof enUS;

export type MessageKey = FlattenMessageKeys<MessageSchema>;

/** Simplified Chinese dictionary. Must mirror the en-US key set exactly. */
export const zhCN: MessageSchema = {
  common: {
    loading: "加载中…",
    retry: "重试",
    technicalDetails: "技术详情",
    refresh: "刷新",
    unknown: "未知",
    utcBucket: "UTC · {bucket}",
    none: "—",
  },
  nav: {
    overview: "总览",
    knowledge: "知识库",
    comingLater: "即将推出",
    dashboard: "仪表板",
    runs: "运行",
    approvals: "审批",
    retrievalPlayground: "检索实验台",
    evaluations: "评估",
    planned: "规划中",
  },
  brand: {
    subtitle: "企业级 Agent 运行时与控制平面",
  },
  shell: {
    menu: "菜单",
    skipToContent: "跳到主要内容",
    footnote: "会话仅存于内存 · 不存储任何令牌",
    primaryNav: "主导航",
    language: "语言",
    languageEn: "English",
    languageZh: "简体中文",
  },
  session: {
    connect: "连接工作区",
    change: "更换",
    clear: "清除会话",
    use: "使用会话",
    cancel: "取消",
    workspaceLabel: "工作区",
    active: "会话已激活",
    notConnected: "未连接",
    panelConnect: "连接工作区",
    panelSession: "工作区会话",
    workspaceId: "工作区 ID",
    accessToken: "访问令牌",
    tokenPlaceholder: "Bearer 访问令牌",
    replaceTokenPlaceholder: "输入新令牌以替换当前会话",
    requiredFields: "工作区 ID 和访问令牌均为必填项。",
    connectedNote: "工作区 {id} 的会话已激活。令牌仅存于内存，刷新后清除。",
    notConnectedNote:
      "连接工作区会话后，即可使用仪表板、运行与审批。凭据仅保存在内存中，不会写入任何存储。",
    requiredTitle: "需要会话",
    requiredHint:
      "请配置工作区会话以查看{context}。会话仅存于内存，刷新后即清除。",
    configure: "配置会话",
    context: {
      home: "工作区总览",
      dashboard: "工作区仪表板",
      runs: "运行历史",
      run: "该运行",
      approvals: "审批收件箱",
      playground: "检索实验台",
    },
  },
  home: {
    eyebrow: "总览",
    title: "工作区总览",
    lede: "面向企业 Agent 的运行、审批与观测控制平面。",
    sessionEyebrow: "会话",
    sessionTitle: "工作区连接",
    quickLinks: "快捷导航",
    cardCta: "打开",
    cards: {
      dashboard: {
        title: "仪表板",
        description: "该工作区的成功率、延迟、用量、成本与失败分析。",
      },
      runs: {
        title: "运行",
        description: "工作区范围内的运行历史，展示脱敏后的状态、用量与成本信息。",
      },
      approvals: {
        title: "审批",
        description: "审核待处理的工具审批，跟进需要关注的操作。",
      },
      playground: {
        title: "检索实验台",
        description: "查看单个知识快照的 Dense、Sparse、Fused、Rerank 证据。",
      },
    },
  },
  status: {
    RUNNING: "运行中",
    WAITING_APPROVAL: "等待审批",
    SUCCEEDED: "已成功",
    FAILED: "已失败",
    NEEDS_ATTENTION: "需要处理",
    CANCEL_REQUESTED: "取消请求中",
    CANCELLED: "已取消",
    PENDING: "待审批",
    APPROVED: "已批准",
    DENIED: "已拒绝",
    EXPIRED: "已过期",
    NOT_STARTED: "未开始",
    CLAIMED: "已认领",
    UNKNOWN_OUTCOME: "结果未知",
    COMPLETED: "已完成",
    tone: {
      neutral: "状态",
      info: "状态：进行中",
      success: "状态：正常",
      warning: "状态：等待中",
      danger: "状态：已失败",
      attention: "状态：需要处理",
    },
  },
  failureCategory: {
    MODEL: "模型",
    ACTION: "动作",
    APPROVAL: "审批",
    TOOL: "工具",
    KNOWLEDGE: "知识库",
  },
  errors: {
    loadDashboard: "无法加载可观测性数据。",
    loadRuns: "无法加载运行记录。",
    loadRunDetail: "无法加载运行详情。",
    loadApprovals: "无法加载审批。",
    decideApproval: "无法记录审批决定。",
    retrieval: "检索失败。",
    hint: {
      sessionInvalid:
        "工作区会话可能缺失，或访问令牌已失效。请在顶栏重新连接会话。",
      forbidden: "当前令牌没有访问该工作区资源的权限。",
      notFound: "在该工作区中未找到对应资源。",
    },
  },
  dashboard: {
    eyebrow: "可观测性",
    title: "工作区仪表板",
    lede: "成功率、延迟、用量、成本与失败分析，所有百分比均附样本分母；原始提示词与工具负载数据不会出现在此视图中。",
    window: "时间范围",
    windows: { d1: "24 小时", d7: "7 天", d30: "30 天", d90: "90 天" },
    kpi: {
      successRate: "成功率",
      successHint: "已完成 {numerator} / {denominator}",
      p95Latency: "p95 延迟",
      p95Hint: "p50 {p50} · {count} 个样本",
      tokensPerRun: "每次运行 Token 数",
      tokensHint: "{known} 已知 · {unknown} 未知",
      costPerSuccess: "每次成功运行成本",
      costHint: "基于 {count} 次成功运行的成本",
    },
    ops: {
      running: "运行中",
      waitingApproval: "等待审批",
      needsAttention: "需要处理",
      unknownOutcome: "结果未知",
      unknownOutcomeHint: "无法确认结果的动作",
    },
    failures: {
      eyebrow: "失败分布",
      title: "失败分类",
      empty: "该时间段内没有失败的运行。",
      emptyHint: "失败的运行及其分类会显示在这里。",
    },
    cost: {
      eyebrow: "使用成本",
      title: "各币种成本",
      mixed: "混合",
      mixedNote: "检测到混合货币——金额按币种分组显示，绝不合计。",
      empty: "该时间段内没有成本数据。",
      emptyHint: "未知成本显示为未知，绝不显示为零。",
      samples: "估计 {estimated} · 精确 {exact}",
      perSuccess: "/ 成功运行 {value}",
    },
    trend: {
      eyebrow: "趋势",
      title: "运行趋势",
      legendSucceeded: "成功",
      legendFailed: "失败",
      legendNeedsAttention: "需要处理",
      ariaPrefix: "各时间桶运行数：",
      ariaEntry: "{date}：成功 {succeeded}，失败 {failed}，需要处理 {needsAttention}",
      barTitle:
        "{date} —— 共 {runs} 次运行：成功 {succeeded}，失败 {failed}，需要处理 {needsAttention}，Token {tokens}",
      note: "每个时间桶峰值 {peak} 次运行；精确数值：{values}",
    },
    versions: {
      eyebrow: "按版本统计",
      title: "AgentVersion 分布",
      noRanking: "不做优劣排名",
      empty: "暂无 AgentVersion 运行数据。",
      runCountOne: "1 次运行",
      runCountOther: "{count} 次运行",
      costUnknown: "成本未知",
    },
    failureRuns: {
      eyebrow: "运行列表",
      title: "失败运行",
      allCategories: "全部分类",
      empty: "该时间段内没有失败的运行。",
      emptyHint: "在上方选择一个分类进行筛选。",
      actionPrefix: "动作失败 {code}",
    },
  },
  runs: {
    eyebrow: "运行",
    title: "运行",
    lede: "工作区范围内的运行历史，展示脱敏后的状态、用量、成本与审批信息。",
    filters: {
      status: "状态",
      allStatuses: "全部状态",
      agentVersionId: "Agent Version ID",
      agentVersionPlaceholder: "留空则查询全部版本",
      apply: "应用",
      clear: "清空",
    },
    columns: {
      run: "运行",
      status: "状态",
      version: "版本",
      started: "开始时间",
      duration: "持续时间",
      tokens: "Token",
      cost: "成本",
      tools: "工具",
      failure: "失败",
    },
    empty: "未找到运行记录。",
    emptyFilteredHint: "没有符合当前筛选条件的运行。清空筛选可查看全部。",
    emptyHint: "当 Agent 在该工作区中执行后，运行记录会显示在这里。",
    loadMore: "加载更多",
  },
  run: {
    eyebrow: "运行详情",
    title: "运行",
    backToRuns: "← 返回运行列表",
    agentVersionEyebrow: "AgentVersion v{version}",
    waitingCallout: "等待审批决定后才会执行操作。",
    attentionCallout:
      "需要处理：该运行需要人工跟进（例如未确认的操作结果）。",
    openApprovals: "打开审批",
    facts: {
      duration: "持续时间",
      tokens: "Token",
      cost: "成本",
      modelSteps: "模型步数",
      toolCalls: "工具调用",
      failure: "失败",
    },
    approvalsLabel: "审批：",
    chips: {
      pending: "待审批 {count}",
      approved: "已批准 {count}",
      denied: "已拒绝 {count}",
      executionFailed: "执行失败 {count}",
      unknownOutcome: "结果未知 {count}",
    },
    notFound: "未找到该运行。",
    notFoundHint: "该运行在已连接的工作区中不存在。",
    technicalMetadata: "技术元数据",
  },
  timeline: {
    eyebrow: "脱敏事件 · 供应商中立",
    title: "时间线",
    empty: "暂无时间线事件。",
    emptyHint: "运行开始执行后，事件会显示在这里。",
    kind: {
      RUN_STARTED: "运行开始",
      MODEL: "模型步骤",
      RETRIEVAL: "检索",
      TOOL: "工具调用",
      APPROVAL_WAIT: "需要审批",
      APPROVAL_DECISION: "审批决定",
      ACTION_EXECUTION: "动作执行",
      RESUME: "已恢复",
      FINISH: "运行完成",
      FAILURE: "运行失败",
    },
    subtitle: {
      decision: "决定：{value}",
      execution: "执行：{value}",
      executionUnknown:
        "执行：结果未知——动作可能已经产生副作用，但系统无法确认最终结果，需要人工处理",
      waitingFor: "等待 {tool}",
      waitingGeneric: "等待审批决定后才会执行操作",
      agentVersion: "AgentVersion {id}",
      failureCode: "失败代码：{code}",
    },
  },
  approvals: {
    eyebrow: "审批",
    title: "审批",
    lede: "对工具操作进行人工审批，审批决定持久保存、不会丢失。决定与执行状态分开跟踪——已批准的操作仍可能执行失败，结果未知时需要人工处理，而不是盲目重试。",
    inboxEyebrow: "人工审批",
    inboxTitle: "审批收件箱",
    loaded: "已加载 {count} 项",
    countsReflect: "计数基于当前已加载的列表。",
    counts: {
      pending: "待审批 {count}",
      approved: "已批准 {count}",
      denied: "已拒绝 {count}",
      needsAttention: "需要处理 {count}",
    },
    card: {
      toolIdentity: "工具标识",
      runLink: "运行 {id} →",
      decision: "决定",
      execution: "执行",
      decidedBy: "决定时间：{time}（{user}）",
      decidedAt: "决定时间：{time}",
      requested: "申请时间：{time}",
      executed: "执行时间：{time}",
      attempts: "尝试次数：{count}",
      unknownWarning:
        "需要处理：动作已执行，但无法确认其结果。请勿盲目重试。",
      failure: "失败 {code} —— {message}",
      failureCodeOnly: "失败 {code}",
      approve: "批准",
      deny: "拒绝",
      approving: "批准中…",
      denying: "拒绝中…",
      technicalArguments: "技术参数",
    },
    empty: "该工作区暂无审批。",
    emptyHint: "当 Agent 请求执行有风险的工具操作时，待审批请求会出现在这里。",
  },
  playground: {
    eyebrow: "知识库",
    title: "检索实验台",
    lede: "查看单个知识快照在 Dense、Sparse、Fused、Rerank 各阶段的证据。检索基于当前会话所属的工作区执行。",
    queryEyebrow: "查询",
    queryTitle: "针对指定快照运行检索",
    knowledgeBaseId: "知识库 ID",
    snapshotId: "快照 ID",
    query: "查询",
    queryPlaceholder: "在所选快照中搜索…",
    advancedConfig: "高级检索配置",
    run: "运行检索",
    running: "检索中…",
    initial: "输入查询和快照即可开始。",
    invalidRequest: "请先填写所有必填字段。",
    sessionRequired: "请先配置工作区会话再运行实验台。",
    empty: "该快照未找到证据。",
    emptyHint: "各检索阶段已执行，但未返回任何内容块。",
    overview: {
      snapshot: "快照",
      totalLatency: "总延迟",
      finalEvidence: "最终证据",
    },
    stage: {
      eyebrow: "阶段",
      results: "{count} 条结果",
      noResults: "无结果",
    },
    evidence: {
      eyebrow: "最终证据",
      title: "Rerank 证据",
      chunks: "{count} 个块",
    },
  },
};

const DICTIONARIES: Record<Locale, MessageSchema> = {
  "en-US": enUS,
  "zh-CN": zhCN,
};

function lookup(dict: unknown, key: string): string | undefined {
  let node: unknown = dict;
  for (const part of key.split(".")) {
    if (typeof node !== "object" || node === null) return undefined;
    node = (node as Record<string, unknown>)[part];
  }
  return typeof node === "string" ? node : undefined;
}

/** Resolve a message key; falls back to en-US, then to the raw key. */
export function resolveMessage(locale: Locale, key: string, values?: Record<string, string | number>): string {
  const template = lookup(DICTIONARIES[locale], key) ?? lookup(enUS, key);
  if (template === undefined) return key;
  if (!values) return template;
  return template.replace(/\{(\w+)\}/g, (match, name: string) =>
    name in values ? String(values[name]) : match,
  );
}

/**
 * Human label for a backend status enum value. The raw value stays the
 * contract; this is presentation only. Unknown statuses fall back to the
 * raw value and never throw.
 */
export function statusLabel(status: string, locale: Locale): string {
  return lookup(DICTIONARIES[locale], `status.${status.toUpperCase()}`) ?? status;
}

/**
 * Human label for a backend failure category. Unknown categories fall back
 * to the raw value.
 */
export function failureCategoryLabel(category: string, locale: Locale): string {
  return lookup(DICTIONARIES[locale], `failureCategory.${category.toUpperCase()}`) ?? category;
}

/** Human label for a timeline entry kind. Unknown kinds fall back to the raw value. */
export function timelineKindLabel(kind: string, locale: Locale): string {
  return lookup(DICTIONARIES[locale], `timeline.kind.${kind}`) ?? kind;
}
