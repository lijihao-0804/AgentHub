"use client";

import Link from "next/link";

import Icon, { type IconName } from "@/components/ui/icon";
import type { MessageKey } from "@/i18n/messages";
import { useI18n } from "@/i18n/provider";
import { useFrontendSession } from "@/components/providers/session-provider";

const QUICK_LINKS: Array<{ href: string; icon: IconName; titleKey: MessageKey; descriptionKey: MessageKey }> = [
  {
    href: "/dashboard",
    icon: "dashboard",
    titleKey: "home.cards.dashboard.title",
    descriptionKey: "home.cards.dashboard.description",
  },
  {
    href: "/runs",
    icon: "runs",
    titleKey: "home.cards.runs.title",
    descriptionKey: "home.cards.runs.description",
  },
  {
    href: "/approvals",
    icon: "approvals",
    titleKey: "home.cards.approvals.title",
    descriptionKey: "home.cards.approvals.description",
  },
  {
    href: "/knowledge/playground",
    icon: "search",
    titleKey: "home.cards.playground.title",
    descriptionKey: "home.cards.playground.description",
  },
];

/**
 * The four applications, in the order they were built.
 *
 * Each one is an `AgentVersion` with its own prompt and its own MCP tools,
 * writing its own artifact types into the same threads. The `act` line is the
 * one that carries the argument: the four differ in what they are allowed to
 * do, and that difference lives in tool governance, not in four runtimes.
 */
const APPLICATIONS: Array<{
  href: string;
  icon: IconName;
  titleKey: MessageKey;
  descriptionKey: MessageKey;
  actKey: MessageKey;
}> = [
  {
    href: "/research",
    icon: "research",
    titleKey: "home.applications.research.title",
    descriptionKey: "home.applications.research.description",
    actKey: "home.applications.research.act",
  },
  {
    href: "/incidents",
    icon: "incidents",
    titleKey: "home.applications.incidents.title",
    descriptionKey: "home.applications.incidents.description",
    actKey: "home.applications.incidents.act",
  },
  {
    href: "/analytics",
    icon: "analytics",
    titleKey: "home.applications.analytics.title",
    descriptionKey: "home.applications.analytics.description",
    actKey: "home.applications.analytics.act",
  },
  {
    href: "/support",
    icon: "support",
    titleKey: "home.applications.support.title",
    descriptionKey: "home.applications.support.description",
    actKey: "home.applications.support.act",
  },
];

export default function Home() {
  const { t } = useI18n();
  const { connected, workspaceId, workspaces, organizations, openPanel } = useFrontendSession();

  const activeWorkspace = workspaces.find((workspace) => workspace.id === workspaceId) ?? null;
  const activeOrganization = activeWorkspace
    ? organizations.find((organization) => organization.id === activeWorkspace.organization_id) ?? null
    : null;

  /*
   * The stage rail is authored as one translated string ("Understand →
   * Acquire → …") so a translator sees the sequence as a sentence rather
   * than five disconnected words. Splitting it here is what turns it into
   * a rail without asking the translator to maintain five keys in order.
   */
  const stages = t("home.applications.stages")
    .split("→")
    .map((stage) => stage.trim())
    .filter(Boolean);

  return (
    <div className="page">
      <header className="page-header">
        <p className="eyebrow">{t("home.eyebrow")}</p>
        <h1>{t("home.title")}</h1>
        <p className="page-lede">{t("home.lede")}</p>
        {/*
         * The workspace used to own a full panel above the fold, showing a
         * truncated raw id. Connected is the normal state and needs no panel:
         * it is one quiet line naming the workspace, with the id kept on the
         * tooltip for the moments someone genuinely needs to copy it.
         */}
        {connected && activeWorkspace && (
          <p className="page-context">
            <Icon name="workspace" size={14} />
            <span className="page-context-label">{t("session.workspaceLabel")}</span>
            <strong title={workspaceId}>
              {activeOrganization ? `${activeOrganization.name} / ` : ""}
              {activeWorkspace.name}
            </strong>
            <button type="button" className="link-button" onClick={openPanel}>
              {t("session.change")}
            </button>
          </p>
        )}
      </header>

      {/* Disconnected is the one state that does deserve the space. */}
      {!connected && (
        <section className="panel" aria-labelledby="session-overview-title">
          <div className="panel-heading">
            <div>
              <p className="eyebrow">{t("home.sessionEyebrow")}</p>
              <h2 id="session-overview-title">{t("home.sessionTitle")}</h2>
            </div>
          </div>
          <div className="state-block state-inline">
            <p className="state-title">{t("workspace.noWorkspaces")}</p>
            <p className="state-hint">{t("workspace.noWorkspacesHint")}</p>
            <button type="button" className="button button-primary" onClick={openPanel}>
              {t("workspace.selectWorkspace")}
            </button>
          </div>
        </section>
      )}

      <section className="panel home-hero" aria-labelledby="applications-title">
        <div className="home-hero-intro">
          <p className="eyebrow">{t("home.applications.eyebrow")}</p>
          <h2 id="applications-title">{t("home.applications.title")}</h2>
          <p className="home-hero-lede">{t("home.applications.lede")}</p>
        </div>
        <ol className="stage-rail" aria-label={t("home.applications.stages")}>
          {stages.map((stage) => (
            <li className="stage-chip" key={stage}>
              {stage}
            </li>
          ))}
        </ol>
        <div className="app-grid">
          {APPLICATIONS.map((item) => (
            <Link className="app-card" href={item.href} key={item.href}>
              <span className="app-card-icon" aria-hidden="true">
                <Icon name={item.icon} size={22} />
              </span>
              <span className="app-card-title">{t(item.titleKey)}</span>
              <span className="app-card-description">{t(item.descriptionKey)}</span>
              <span className="app-card-act">{t(item.actKey)}</span>
              <span className="app-card-cta" aria-hidden="true">
                {t("home.cardCta")}
                <Icon name="arrowRight" size={15} />
              </span>
            </Link>
          ))}
        </div>
      </section>

      <section className="quick-links" aria-labelledby="quick-links-title">
        <p className="eyebrow" id="quick-links-title">
          {t("home.quickLinks")}
        </p>
        <div className="quick-grid">
          {QUICK_LINKS.map((item) => (
            <Link className="quick-card" href={item.href} key={item.href}>
              <Icon name={item.icon} size={18} className="quick-card-icon" />
              <span className="quick-card-body">
                <span className="quick-card-title">{t(item.titleKey)}</span>
                <span className="quick-card-description">{t(item.descriptionKey)}</span>
              </span>
              <Icon name="arrowRight" size={15} className="quick-card-arrow" />
            </Link>
          ))}
        </div>
      </section>
    </div>
  );
}
