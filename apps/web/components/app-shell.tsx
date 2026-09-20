"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";

import { useI18n } from "../i18n/provider";
import type { MessageKey } from "../i18n/messages";
import { LocaleProvider } from "../i18n/provider";
import { FrontendSessionProvider } from "./session-provider";
import LanguageSwitcher from "./language-switcher";
import SessionControl from "./session-control";

type NavItem = { href: string; labelKey: MessageKey; match: (pathname: string) => boolean };

function prefixMatch(prefix: string): (pathname: string) => boolean {
  return (pathname) => pathname === prefix || pathname.startsWith(`${prefix}/`);
}

const NAV_GROUPS: Array<{ headingKey: MessageKey; items: NavItem[] }> = [
  {
    headingKey: "nav.overview",
    items: [
      { href: "/dashboard", labelKey: "nav.dashboard", match: prefixMatch("/dashboard") },
      { href: "/runs", labelKey: "nav.runs", match: prefixMatch("/runs") },
      { href: "/approvals", labelKey: "nav.approvals", match: prefixMatch("/approvals") },
    ],
  },
  {
    headingKey: "nav.knowledge",
    items: [
      {
        href: "/knowledge/playground",
        labelKey: "nav.retrievalPlayground",
        match: prefixMatch("/knowledge"),
      },
    ],
  },
];

function NavLink({ item, onNavigate }: { item: NavItem; onNavigate: () => void }) {
  const { t } = useI18n();
  const pathname = usePathname();
  const active = item.match(pathname);
  return (
    <li>
      <Link
        href={item.href}
        className={`nav-link${active ? " nav-link-active" : ""}`}
        aria-current={active ? "page" : undefined}
        onClick={onNavigate}
      >
        {t(item.labelKey)}
      </Link>
    </li>
  );
}

function SidebarContent({ onNavigate }: { onNavigate: () => void }) {
  const { t } = useI18n();
  const pathname = usePathname() ?? "";
  return (
    <>
      <div className="sidebar-brand">
        <Link href="/" className="brand-link" onClick={onNavigate}>
          <span className="brand-name">AgentHub</span>
          <span className="brand-subtitle">{t("brand.subtitle")}</span>
        </Link>
      </div>
      <nav className="sidebar-nav" aria-label={t("shell.primaryNav")}>
        {NAV_GROUPS.map((group) => (
          <div className="nav-group" key={group.headingKey}>
            <p className="nav-group-heading">{t(group.headingKey)}</p>
            <ul>
              {group.items.map((item) => (
                <NavLink item={item} onNavigate={onNavigate} key={item.href} />
              ))}
            </ul>
          </div>
        ))}
        <div className="nav-group">
          <p className="nav-group-heading">{t("evaluation.eyebrow")}</p>
          <ul>
            <li>
              <Link
                href="/evaluations"
                className={`nav-link${pathname.startsWith("/evaluations") ? " nav-link-active" : ""}`}
                aria-current={pathname.startsWith("/evaluations") ? "page" : undefined}
                onClick={onNavigate}
              >
                {t("nav.evaluations")}
              </Link>
            </li>
          </ul>
        </div>
      </nav>
      <p className="sidebar-footnote">{t("shell.footnote")}</p>
    </>
  );
}

export default function AppShell({ children }: { children: ReactNode }) {
  return (
    <LocaleProvider>
      <FrontendSessionProvider>
        <ShellChrome>{children}</ShellChrome>
      </FrontendSessionProvider>
    </LocaleProvider>
  );
}

function ShellChrome({ children }: { children: ReactNode }) {
  const { t } = useI18n();
  const [navOpen, setNavOpen] = useState(false);

  useEffect(() => {
    document.body.classList.toggle("nav-open", navOpen);
    return () => document.body.classList.remove("nav-open");
  }, [navOpen]);

  useEffect(() => {
    if (!navOpen) return;
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") setNavOpen(false);
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [navOpen]);

  return (
    <div className="app-shell">
      <a className="skip-link" href="#app-content">
        {t("shell.skipToContent")}
      </a>
      <aside className="sidebar" id="app-sidebar">
        <SidebarContent onNavigate={() => setNavOpen(false)} />
      </aside>
      <div className="app-frame">
        <header className="topbar">
          <button
            type="button"
            className="button button-ghost nav-toggle"
            aria-expanded={navOpen}
            aria-controls="app-sidebar"
            onClick={() => setNavOpen((open) => !open)}
          >
            <span aria-hidden="true">☰</span> <span className="nav-toggle-label">{t("shell.menu")}</span>
          </button>
          <div className="topbar-spacer" />
          <LanguageSwitcher />
          <SessionControl />
        </header>
        <main className="app-content" id="app-content">
          {children}
        </main>
      </div>
    </div>
  );
}
