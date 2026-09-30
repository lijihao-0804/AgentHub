"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";

import { useI18n } from "@/i18n/provider";
import type { MessageKey } from "@/i18n/messages";
import { LocaleProvider } from "@/i18n/provider";
import { FrontendSessionProvider, useFrontendSession } from "@/components/providers/session-provider";
import LanguageSwitcher from "@/components/layout/language-switcher";
import ThemeSwitcher from "@/components/layout/theme-switcher";
import { ThemeProvider } from "@/components/providers/theme-provider";
import WorkspaceSelector from "@/components/layout/workspace-selector";
import UserMenu from "@/components/layout/user-menu";
import Icon, { type IconName } from "@/components/ui/icon";
import { getObservabilitySummary } from "@/lib/api/observability";

type NavItem = {
  href: string;
  labelKey: MessageKey;
  icon: IconName;
  match: (pathname: string) => boolean;
  /** Rendered indented under the entry above it, for a tool that belongs to it. */
  nested?: boolean;
  /** Live counter source; hidden while zero or unknown. */
  badge?: keyof NavCounts;
};

type NavCounts = { approvalsPending: number | null; running: number | null };

/**
 * Sidebar counters: pending approvals and in-flight runs. One lightweight
 * summary poll every 30 seconds while the tab is visible; a failed poll
 * simply hides the badges instead of shouting.
 */
function useNavCounts(): NavCounts {
  const { connected, workspaceId, accessToken } = useFrontendSession();
  const [counts, setCounts] = useState<NavCounts>({ approvalsPending: null, running: null });
  useEffect(() => {
    if (!connected) {
      setCounts({ approvalsPending: null, running: null });
      return;
    }
    let cancelled = false;
    const load = () => {
      if (document.hidden) return;
      getObservabilitySummary({ workspaceId, accessToken })
        .then((summary) => {
          if (!cancelled) {
            setCounts({
              approvalsPending: summary.approvals.pending,
              running: summary.current.running_count,
            });
          }
        })
        .catch(() => {
          if (!cancelled) setCounts({ approvalsPending: null, running: null });
        });
    };
    load();
    const interval = window.setInterval(load, 30_000);
    document.addEventListener("visibilitychange", load);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
      document.removeEventListener("visibilitychange", load);
    };
  }, [connected, workspaceId, accessToken]);
  return counts;
}

function prefixMatch(prefix: string, ...except: string[]): (pathname: string) => boolean {
  return (pathname) => {
    // A nested tool owns its own entry, so the parent must not light up for it.
    if (except.some((route) => pathname === route || pathname.startsWith(`${route}/`))) return false;
    return pathname === prefix || pathname.startsWith(`${prefix}/`);
  };
}

/**
 * The sidebar is the whole map of the product. Anything reachable only by
 * typing a URL or by finding one contextual button is, in practice, not
 * shipped — so the standalone tools (run comparison, the retrieval
 * playground, provider configuration) get their own entries, indented under
 * the section they belong to rather than hidden inside it.
 */
const NAV_GROUPS: Array<{ headingKey: MessageKey; items: NavItem[] }> = [
  {
    headingKey: "nav.overview",
    items: [
      { href: "/dashboard", labelKey: "nav.dashboard", icon: "dashboard", match: prefixMatch("/dashboard") },
      { href: "/runs", labelKey: "nav.runs", icon: "runs", match: prefixMatch("/runs", "/runs/compare"), badge: "running" },
      { href: "/runs/compare", labelKey: "nav.runCompare", icon: "compare", match: prefixMatch("/runs/compare"), nested: true },
      { href: "/approvals", labelKey: "nav.approvals", icon: "approvals", match: prefixMatch("/approvals"), badge: "approvalsPending" },
    ],
  },
  {
    headingKey: "nav.applications",
    items: [
      { href: "/research", labelKey: "nav.research", icon: "research", match: prefixMatch("/research") },
      { href: "/incidents", labelKey: "nav.incidents", icon: "incidents", match: prefixMatch("/incidents") },
      { href: "/analytics", labelKey: "nav.analytics", icon: "analytics", match: prefixMatch("/analytics") },
      { href: "/support", labelKey: "nav.support", icon: "support", match: prefixMatch("/support") },
    ],
  },
  {
    headingKey: "nav.build",
    items: [
      { href: "/agents", labelKey: "nav.agents", icon: "agents", match: prefixMatch("/agents") },
      { href: "/knowledge", labelKey: "nav.knowledge", icon: "knowledge", match: prefixMatch("/knowledge", "/knowledge/playground") },
      {
        href: "/knowledge/playground",
        labelKey: "nav.retrievalPlayground",
        icon: "search",
        match: prefixMatch("/knowledge/playground"),
        nested: true,
      },
      { href: "/tools", labelKey: "nav.tools", icon: "tools", match: prefixMatch("/tools") },
    ],
  },
  {
    headingKey: "nav.evaluate",
    items: [
      { href: "/evaluations", labelKey: "nav.evaluations", icon: "evaluations", match: prefixMatch("/evaluations") },
      { href: "/evaluations/datasets", labelKey: "nav.datasets", icon: "evaluations", nested: true, match: prefixMatch("/evaluations/datasets") },
      { href: "/evaluations/experiments", labelKey: "nav.experiments", icon: "evaluations", nested: true, match: prefixMatch("/evaluations/experiments") },
      { href: "/evaluations/release-gates", labelKey: "nav.releaseGates", icon: "evaluations", nested: true, match: prefixMatch("/evaluations/release-gates") },
      { href: "/evaluations/pricing", labelKey: "nav.pricing", icon: "evaluations", nested: true, match: prefixMatch("/evaluations/pricing") },
    ],
  },
  {
    headingKey: "nav.workspace",
    items: [
      { href: "/settings", labelKey: "nav.settings", icon: "settings", match: prefixMatch("/settings", "/settings/models") },
      { href: "/settings/models", labelKey: "nav.models", icon: "models", match: prefixMatch("/settings/models"), nested: true },
    ],
  },
];

/** Routes rendered without the control-plane chrome. */
const AUTH_ROUTES = ["/login", "/register"];

function isAuthRoute(pathname: string): boolean {
  return AUTH_ROUTES.some((route) => pathname === route || pathname.startsWith(`${route}/`));
}

function NavLink({
  item,
  onNavigate,
  counts,
}: {
  item: NavItem;
  onNavigate: () => void;
  counts: NavCounts;
}) {
  const { t, formatNumber } = useI18n();
  const pathname = usePathname() ?? "";
  const active = item.match(pathname);
  const badgeCount = item.badge ? counts[item.badge] : null;
  const showBadge = badgeCount !== null && badgeCount > 0;
  return (
    <li>
      <Link
        href={item.href}
        className={`nav-link${item.nested ? " nav-link-nested" : ""}${active ? " nav-link-active" : ""}`}
        aria-current={active ? "page" : undefined}
        onClick={onNavigate}
      >
        <span className="nav-link-label">
          <Icon name={item.icon} size={16} />
          <span>{t(item.labelKey)}</span>
        </span>
        {showBadge && (
          <span
            className={`nav-badge${item.badge === "approvalsPending" ? " nav-badge-urgent" : ""}`}
            title={
              item.badge === "approvalsPending"
                ? t("nav.badgePending", { count: badgeCount })
                : t("nav.badgeRunning", { count: badgeCount })
            }
          >
            {formatNumber(badgeCount)}
          </span>
        )}
      </Link>
    </li>
  );
}

function SidebarContent({ onNavigate }: { onNavigate: () => void }) {
  const { t, formatNumber } = useI18n();
  const counts = useNavCounts();
  return (
    <>
      <div className="sidebar-brand">
        <Link href="/dashboard" className="brand-link" onClick={onNavigate}>
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
                <NavLink item={item} onNavigate={onNavigate} counts={counts} key={item.href} />
              ))}
            </ul>
          </div>
        ))}
      </nav>
      <p className="sidebar-footnote">{t("shell.footnote")}</p>
    </>
  );
}

export default function AppShell({ children }: { children: ReactNode }) {
  return (
    <ThemeProvider>
      <LocaleProvider>
        <FrontendSessionProvider>
          <ShellGate>{children}</ShellGate>
        </FrontendSessionProvider>
      </LocaleProvider>
    </ThemeProvider>
  );
}

/**
 * Routes traffic between the auth pages and the control-plane shell.
 * A missing session is a normal signed-out state, never an error.
 */
function ShellGate({ children }: { children: ReactNode }) {
  const { status } = useFrontendSession();
  const pathname = usePathname() ?? "";
  const router = useRouter();
  const onAuthRoute = isAuthRoute(pathname);

  useEffect(() => {
    if (status === "UNAUTHENTICATED" && !onAuthRoute) router.replace("/login");
    if (status === "AUTHENTICATED" && onAuthRoute) router.replace("/dashboard");
  }, [status, onAuthRoute, router]);

  if (status === "BOOTSTRAPPING") return <BootstrapScreen />;
  if (onAuthRoute) {
    return status === "AUTHENTICATED" ? <BootstrapScreen /> : <div className="auth-frame">{children}</div>;
  }
  if (status === "UNAUTHENTICATED") return <BootstrapScreen />;
  return <ShellChrome>{children}</ShellChrome>;
}

function BootstrapScreen() {
  const { t } = useI18n();
  return (
    <div className="auth-frame">
      <div className="state-block state-loading" role="status" aria-live="polite">
        <span className="loading-bar" aria-hidden="true" />
        <span>{t("common.loading")}</span>
      </div>
    </div>
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
          <ThemeSwitcher />
          <LanguageSwitcher />
          <WorkspaceSelector />
          <UserMenu />
        </header>
        <main className="app-content" id="app-content">
          {children}
        </main>
      </div>
    </div>
  );
}
