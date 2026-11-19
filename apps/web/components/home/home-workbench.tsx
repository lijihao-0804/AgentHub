"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState, type FormEvent } from "react";

import Icon, { type IconName } from "@/components/ui/icon";
import StatusBadge from "@/components/ui/status-badge";
import { SessionRequired } from "@/components/ui/states";
import { useFrontendSession } from "@/components/providers/session-provider";
import { useI18n } from "@/i18n/provider";
import type { MessageKey } from "@/i18n/messages";
import type { Locale } from "@/i18n/types";
import { listAgents } from "@/lib/api/agents";
import { listApprovals } from "@/lib/api/approvals";
import { listKnowledgeBases } from "@/lib/api/knowledge";
import { listRuns, type RunListItem } from "@/lib/api/runs";
import { listTools } from "@/lib/api/tools";

type HomeApplication = {
  href: string;
  icon: IconName;
  titleKey: MessageKey;
  actKey: MessageKey;
};

type HomeCopy = {
  heading: string;
  dashboard: string;
  launchTitle: string;
  taskLabel: string;
  taskPlaceholder: string;
  destination: string;
  start: string;
  pulseTitle: string;
  agents: string;
  knowledge: string;
  tools: string;
  approvals: string;
  unavailable: string;
  refresh: string;
  partialError: string;
  recentTitle: string;
  viewRuns: string;
  noRuns: string;
  runsUnavailable: string;
  openPlayground: string;
  applicationsTitle: string;
  loading: string;
};

const COPY: Record<Locale, HomeCopy> = {
  "en-US": {
    heading: "Workbench",
    dashboard: "Operations dashboard",
    launchTitle: "Start a task",
    taskLabel: "Task",
    taskPlaceholder: "Describe the outcome you need…",
    destination: "Run with",
    start: "Continue",
    pulseTitle: "Workspace resources",
    agents: "Agents",
    knowledge: "Knowledge bases",
    tools: "Tools",
    approvals: "Pending approvals",
    unavailable: "Unavailable",
    refresh: "Refresh",
    partialError: "Some details could not load. You can still use the rest of the workspace.",
    recentTitle: "Recent work",
    viewRuns: "View all runs",
    noRuns: "No runs yet",
    runsUnavailable: "Recent runs are unavailable right now.",
    openPlayground: "Test retrieval",
    applicationsTitle: "Applications",
    loading: "Loading workspace context…",
  },
  "zh-CN": {
    heading: "工作台",
    dashboard: "运营仪表板",
    launchTitle: "发起任务",
    taskLabel: "任务",
    taskPlaceholder: "描述你希望得到的结果…",
    destination: "使用应用",
    start: "继续",
    pulseTitle: "工作区资源",
    agents: "智能体",
    knowledge: "知识库",
    tools: "工具",
    approvals: "待审批",
    unavailable: "暂不可用",
    refresh: "刷新",
    partialError: "部分内容暂时无法加载，其余工作区功能仍可继续使用。",
    recentTitle: "最近工作",
    viewRuns: "查看全部运行",
    noRuns: "暂无运行",
    runsUnavailable: "最近运行暂时无法加载。",
    openPlayground: "测试检索",
    applicationsTitle: "工作应用",
    loading: "正在加载工作区上下文…",
  },
};

const APPLICATIONS: HomeApplication[] = [
  {
    href: "/research",
    icon: "research",
    titleKey: "home.applications.research.title",
    actKey: "home.applications.research.act",
  },
  {
    href: "/incidents",
    icon: "incidents",
    titleKey: "home.applications.incidents.title",
    actKey: "home.applications.incidents.act",
  },
  {
    href: "/analytics",
    icon: "analytics",
    titleKey: "home.applications.analytics.title",
    actKey: "home.applications.analytics.act",
  },
  {
    href: "/support",
    icon: "support",
    titleKey: "home.applications.support.title",
    actKey: "home.applications.support.act",
  },
];

type HomeWorkspaceData = {
  agentCount: number | null;
  knowledgeCount: number | null;
  toolCount: number | null;
  pendingApprovalCount: number | null;
  recentRuns: RunListItem[] | null;
};

const EMPTY_WORKSPACE_DATA: HomeWorkspaceData = {
  agentCount: null,
  knowledgeCount: null,
  toolCount: null,
  pendingApprovalCount: null,
  recentRuns: null,
};

function settledValue<T>(result: PromiseSettledResult<T>): T | null {
  return result.status === "fulfilled" ? result.value : null;
}

function displayCount(value: number | null, fallback: string, formatCount: (value: number) => string): string {
  return value === null ? fallback : formatCount(value);
}

export default function HomeWorkbench() {
  const { sessionId, workspaceId } = useFrontendSession();
  return <HomeWorkbenchContent key={`${sessionId}:${workspaceId}`} />;
}

function HomeWorkbenchContent() {
  const { t, locale, statusLabel, errorText, formatCount, formatRelativeTime } = useI18n();
  const { connected, workspaceId, accessToken, sessionId, queueTaskDraft } = useFrontendSession();
  const router = useRouter();
  const copy = COPY[locale];
  const [draft, setDraft] = useState("");
  const [appHref, setAppHref] = useState(APPLICATIONS[0].href);
  const [data, setData] = useState<HomeWorkspaceData>(EMPTY_WORKSPACE_DATA);
  const [loading, setLoading] = useState(false);
  const [hasLoaded, setHasLoaded] = useState(false);
  const [partialError, setPartialError] = useState(false);
  const [reloadToken, setReloadToken] = useState(0);
  const generationRef = useRef(0);

  useEffect(() => {
    if (!connected || !workspaceId || !accessToken) {
      setData(EMPTY_WORKSPACE_DATA);
      setPartialError(false);
      setLoading(false);
      setHasLoaded(false);
      return;
    }
    const generation = (generationRef.current += 1);
    let active = true;
    setLoading(true);
    setPartialError(false);
    const input = { workspaceId, accessToken };
    Promise.allSettled([
      listAgents(input),
      listKnowledgeBases(input),
      listTools(input),
      listApprovals(workspaceId, accessToken, { decision: "PENDING", limit: 1, offset: 0 }),
      listRuns({ ...input, limit: 5 }),
    ]).then((results) => {
      if (!active || generationRef.current !== generation) return;
      const agents = settledValue(results[0]);
      const knowledge = settledValue(results[1]);
      const tools = settledValue(results[2]);
      const approvals = settledValue(results[3]);
      const runs = settledValue(results[4]);
      setData({
        agentCount: agents?.length ?? null,
        knowledgeCount: knowledge?.length ?? null,
        toolCount: tools?.length ?? null,
        pendingApprovalCount: approvals?.total ?? null,
        recentRuns: runs?.items ?? null,
      });
      setPartialError(results.some((result) => result.status === "rejected"));
      setHasLoaded(true);
      setLoading(false);
    });
    return () => {
      active = false;
    };
  }, [connected, workspaceId, accessToken, sessionId, reloadToken]);

  function startTask(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    queueTaskDraft(appHref, draft.trim());
    router.push(appHref);
  }

  if (!connected) {
    return (
      <div className="page home-page">
        <header className="home-page-head">
          <div>
            <h1>{copy.heading}</h1>
          </div>
        </header>
        <SessionRequired contextKey="session.context.dashboard" />
      </div>
    );
  }

  const recentRuns = data.recentRuns;
  const pendingApprovals = data.pendingApprovalCount;
  const unknownCountLabel = !hasLoaded ? "…" : copy.unavailable;

  return (
    <div className="page home-page">
      <header className="home-page-head">
        <h1>{copy.heading}</h1>
      </header>

      <div className="home-workspace-layout">
        <div className="home-work-column">
          <section className="home-launch" aria-labelledby="home-launch-title">
            <h2 id="home-launch-title">{copy.launchTitle}</h2>
            <form className="home-task-form" onSubmit={startTask}>
              <label className="home-task-field">
                <span className="sr-only">{copy.taskLabel}</span>
                <textarea rows={3} maxLength={32000}
                  value={draft}
                  onChange={(event) => setDraft(event.target.value)}
                  placeholder={copy.taskPlaceholder}
                  onKeyDown={(event) => {
                    if (!event.nativeEvent.isComposing && event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
                      event.preventDefault();
                      event.currentTarget.form?.requestSubmit();
                    }
                  }}
                />
              </label>
              <fieldset className="home-workflow-options">
                <legend className="sr-only">{copy.destination}</legend>
                {APPLICATIONS.map((item) => (
                  <label key={item.href}>
                    <input type="radio" name="workflow" value={item.href}
                      checked={appHref === item.href} onChange={() => setAppHref(item.href)} />
                    <span><Icon name={item.icon} size={16} />{t(item.titleKey)}</span>
                  </label>
                ))}
              </fieldset>
              <div className="home-task-controls">
                <label className="home-task-destination">
                  <span>{copy.destination}</span>
                  <select value={appHref} onChange={(event) => setAppHref(event.target.value)}>
                    {APPLICATIONS.map((item) => (
                      <option value={item.href} key={item.href}>{t(item.titleKey)}</option>
                    ))}
                  </select>
                </label>
                <button type="submit" className="button button-primary">
                  {copy.start}
                  <Icon name="arrowRight" size={16} />
                </button>
              </div>
            </form>
          </section>

          <section className="home-recent" aria-labelledby="home-recent-title">
            <div className="home-section-heading">
              <div>
                <h2 id="home-recent-title">{copy.recentTitle}</h2>
              </div>
              <Link className="home-text-link" href="/runs">{copy.viewRuns}</Link>
            </div>
            {recentRuns === null ? (
              <div className="home-inline-state">
                <span>{!hasLoaded || loading ? copy.loading : copy.runsUnavailable}</span>
              </div>
            ) : recentRuns.length === 0 ? (
              <div className="home-inline-state">
                <span className="state-title">{copy.noRuns}</span>
              </div>
            ) : (
              <ol className="home-run-list">
                {recentRuns.map((run) => (
                  <li key={run.id}>
                    <Link className="home-run-row" href={`/runs/${encodeURIComponent(run.id)}`}>
                      <span className="home-run-main">
                        <code>{run.id.slice(0, 8)}…</code>
                        <span>v{run.agent_version_number}</span>
                      </span>
                      <StatusBadge status={run.status} />
                      <span className="home-run-time" title={run.started_at || run.created_at}>
                        {formatRelativeTime(run.started_at || run.created_at)}
                      </span>
                      {run.failure_code && (
                        <span className="home-run-result" title={run.failure_code}>
                          {errorText(run.failure_code, statusLabel(run.status))}
                        </span>
                      )}
                    </Link>
                  </li>
                ))}
              </ol>
            )}
          </section>
        </div>
        <div className="home-directory-column">
          <aside className="home-pulse" aria-labelledby="home-pulse-title">
            <div className="home-section-heading home-section-heading-compact">
              <div>
                <h2 id="home-pulse-title">{copy.pulseTitle}</h2>
              </div>
              <button
                type="button"
                className="home-icon-button"
                onClick={() => setReloadToken((value) => value + 1)}
                disabled={loading}
                title={copy.refresh}
                aria-label={copy.refresh}
              >
                <Icon name="runs" size={16} />
              </button>
            </div>
            <div className="home-pulse-grid">
              <Link href="/agents" className="home-pulse-item">
                <span>{copy.agents}</span>
                <strong>{displayCount(data.agentCount, unknownCountLabel, formatCount)}</strong>
              </Link>
              <Link href="/knowledge" className="home-pulse-item">
                <span>{copy.knowledge}</span>
                <strong>{displayCount(data.knowledgeCount, unknownCountLabel, formatCount)}</strong>
              </Link>
              <Link href="/tools" className="home-pulse-item">
                <span>{copy.tools}</span>
                <strong>{displayCount(data.toolCount, unknownCountLabel, formatCount)}</strong>
              </Link>
              <Link
                href="/approvals"
                className={`home-pulse-item${pendingApprovals && pendingApprovals > 0 ? " is-attention" : ""}`}
              >
                <span>{copy.approvals}</span>
                <strong>{displayCount(pendingApprovals, unknownCountLabel, formatCount)}</strong>
              </Link>
            </div>
            {partialError && <p className="home-data-note is-warning">{copy.partialError}</p>}
            {loading && <p className="home-data-note" role="status">{copy.loading}</p>}
            <div className="home-directory-links">
              <Link className="home-text-link" href="/dashboard">{copy.dashboard}</Link>
              <Link className="home-text-link" href="/knowledge/playground">{copy.openPlayground}</Link>
            </div>
          </aside>

          <section className="home-applications" aria-labelledby="home-applications-title">
            <div className="home-section-heading">
              <div>
                <h2 id="home-applications-title">{copy.applicationsTitle}</h2>
              </div>
            </div>
            <div className="home-application-grid">
              {APPLICATIONS.map((item) => (
                <Link className="home-application-card" href={item.href} key={item.href}>
                  <span className="home-application-icon" aria-hidden="true"><Icon name={item.icon} size={21} /></span>
                  <span className="home-application-title">{t(item.titleKey)}</span>
                  <span className="home-application-policy">{t(item.actKey)}</span>
                </Link>
              ))}
            </div>
          </section>
        </div>
      </div>
    </div>
  );
}
