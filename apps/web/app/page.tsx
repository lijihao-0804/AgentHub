"use client";

import Link from "next/link";

import { useI18n } from "../i18n/provider";
import { useFrontendSession } from "../components/session-provider";

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
            <p className="state-title">{t("session.notConnected")}</p>
            <p className="state-hint">{t("session.notConnectedNote")}</p>
            <button type="button" className="button button-primary" onClick={openPanel}>
              {t("session.connect")}
            </button>
          </div>
        )}
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
