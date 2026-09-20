"use client";

import Link from "next/link";

import { useI18n } from "@/i18n/provider";
import { useFrontendSession } from "@/components/providers/session-provider";

const QUICK_LINKS = [
  {
    href: "/dashboard",
    titleKey: "home.cards.dashboard.title",
    descriptionKey: "home.cards.dashboard.description",
  },
  {
    href: "/runs",
    titleKey: "home.cards.runs.title",
    descriptionKey: "home.cards.runs.description",
  },
  {
    href: "/approvals",
    titleKey: "home.cards.approvals.title",
    descriptionKey: "home.cards.approvals.description",
  },
  {
    href: "/knowledge/playground",
    titleKey: "home.cards.playground.title",
    descriptionKey: "home.cards.playground.description",
  },
] as const;

/**
 * The four applications, in the order they were built.
 *
 * Each one is an `AgentVersion` with its own prompt and its own MCP tools,
 * writing its own artifact types into the same threads. The `act` line is the
 * one that carries the argument: the four differ in what they are allowed to
 * do, and that difference lives in tool governance, not in four runtimes.
 */
const APPLICATIONS = [
  {
    href: "/research",
    titleKey: "home.applications.research.title",
    descriptionKey: "home.applications.research.description",
    actKey: "home.applications.research.act",
  },
  {
    href: "/incidents",
    titleKey: "home.applications.incidents.title",
    descriptionKey: "home.applications.incidents.description",
    actKey: "home.applications.incidents.act",
  },
  {
    href: "/analytics",
    titleKey: "home.applications.analytics.title",
    descriptionKey: "home.applications.analytics.description",
    actKey: "home.applications.analytics.act",
  },
  {
    href: "/support",
    titleKey: "home.applications.support.title",
    descriptionKey: "home.applications.support.description",
    actKey: "home.applications.support.act",
  },
] as const;

function shortWorkspaceId(value: string): string {
  if (value.length <= 8) return value;
  return `${value.slice(0, 4)}…${value.slice(-3)}`;
}

export default function Home() {
  const { t } = useI18n();
  const { connected, workspaceId, openPanel } = useFrontendSession();

  return (
    <div className="page">
      <header className="page-header">
        <p className="eyebrow">{t("home.eyebrow")}</p>
        <h1>{t("home.title")}</h1>
        <p className="page-lede">{t("home.lede")}</p>
      </header>

      <section className="panel" aria-labelledby="session-overview-title">
        <div className="panel-heading">
          <div>
            <p className="eyebrow">{t("home.sessionEyebrow")}</p>
            <h2 id="session-overview-title">{t("home.sessionTitle")}</h2>
          </div>
        </div>
        {connected ? (
          <p className="state-hint">
            {t("session.connectedNote", { id: shortWorkspaceId(workspaceId) })}
          </p>
        ) : (
          <div className="state-block state-inline">
            <p className="state-title">{t("workspace.noWorkspaces")}</p>
            <p className="state-hint">{t("workspace.noWorkspacesHint")}</p>
            <button type="button" className="button button-primary" onClick={openPanel}>
              {t("workspace.selectWorkspace")}
            </button>
          </div>
        )}
      </section>

      <section className="panel" aria-labelledby="applications-title">
        <div className="panel-heading">
          <div>
            <p className="eyebrow">{t("home.applications.eyebrow")}</p>
            <h2 id="applications-title">{t("home.applications.title")}</h2>
            <p className="state-hint">{t("home.applications.lede")}</p>
          </div>
        </div>
        <p className="application-stages">{t("home.applications.stages")}</p>
        <div className="overview-grid">
          {APPLICATIONS.map((item) => (
            <Link className="overview-card" href={item.href} key={item.href}>
              <span className="overview-card-title">{t(item.titleKey)}</span>
              <span className="overview-card-description">{t(item.descriptionKey)}</span>
              <span className="overview-card-description application-act">{t(item.actKey)}</span>
              <span className="overview-card-cta" aria-hidden="true">
                {t("home.cardCta")} →
              </span>
            </Link>
          ))}
        </div>
      </section>

      <section className="overview-grid" aria-label={t("home.quickLinks")}>
        {QUICK_LINKS.map((item) => (
          <Link className="overview-card" href={item.href} key={item.href}>
            <span className="overview-card-title">{t(item.titleKey)}</span>
            <span className="overview-card-description">{t(item.descriptionKey)}</span>
            <span className="overview-card-cta" aria-hidden="true">
              {t("home.cardCta")} →
            </span>
          </Link>
        ))}
      </section>
    </div>
  );
}
