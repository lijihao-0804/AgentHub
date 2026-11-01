"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";

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
  const router = useRouter();
  const [draft, setDraft] = useState("");
  const [appHref, setAppHref] = useState(APPLICATIONS[0].href);

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

  function startConversation(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const target = appHref || APPLICATIONS[0].href;
    const seed = draft.trim();
    router.push(seed ? `${target}?draft=${encodeURIComponent(seed)}` : target);
  }

  return (
    <div className="page">
      <header className="page-header">
        <h1>{t("home.title")}</h1>
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

      {connected && (
        <section className="home-composer" aria-label={t("home.composer.title")}>
          <form className="home-composer-form" onSubmit={startConversation}>
            <label className="home-composer-field">
              <span className="sr-only">{t("home.composer.placeholder")}</span>
              <input
                value={draft}
                onChange={(event) => setDraft(event.target.value)}
                placeholder={t("home.composer.placeholder")}
                autoComplete="off"
              />
            </label>
            <label className="home-composer-agent">
              <span className="sr-only">{t("home.composer.agent")}</span>
              <select
                value={appHref}
                onChange={(event) => setAppHref(event.target.value)}
                aria-label={t("home.composer.agent")}
              >
                {APPLICATIONS.map((item) => (
                  <option value={item.href} key={item.href}>
                    {t(item.titleKey)}
                  </option>
                ))}
              </select>
            </label>
            <button type="submit" className="button button-primary">
              {t("home.composer.start")}
            </button>
          </form>
          <p className="home-composer-hint">{t("home.composer.hint")}</p>
        </section>
      )}

      {connected && (
        <nav className="home-apps" aria-label={t("home.applications.title")}>
          {APPLICATIONS.map((item) => (
            <Link className="home-app-chip" href={item.href} key={item.href}>
              <span className="home-app-chip-icon" aria-hidden="true">
                <Icon name={item.icon} size={16} />
              </span>
              <span>{t(item.titleKey)}</span>
            </Link>
          ))}
        </nav>
      )}

      <details className="panel home-hero home-collapsible">
        <summary className="home-collapsible-summary">
          <span className="eyebrow">{t("home.applications.eyebrow")}</span>
          <span className="home-collapsible-title">{t("home.applications.title")}</span>
        </summary>
        <div className="home-hero-intro">
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
      </details>

      <details className="home-collapsible home-quick">
        <summary className="home-collapsible-summary">
          <span className="home-collapsible-title">{t("home.quickLinks")}</span>
        </summary>
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
      </details>
    </div>
  );
}
