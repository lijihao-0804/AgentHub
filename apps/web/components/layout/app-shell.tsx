"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { Fragment, useEffect, useRef, useState, type ReactNode } from "react";

import { LocaleProvider, useI18n } from "@/i18n/provider";
import type { MessageKey } from "@/i18n/messages";
import {
  FrontendSessionProvider,
  useFrontendSession,
} from "@/components/providers/session-provider";
import LanguageSwitcher from "@/components/layout/language-switcher";
import ThemeSwitcher from "@/components/layout/theme-switcher";
import { ThemeProvider } from "@/components/providers/theme-provider";
import WorkspaceSelector from "@/components/layout/workspace-selector";
import PageFinder from "@/components/layout/page-finder";
import UserMenu from "@/components/layout/user-menu";
import { RouteTitle } from "@/components/layout/route-title";
import { AutoBreadcrumbs } from "@/components/layout/breadcrumbs";
import Icon, { type IconName } from "@/components/ui/icon";
import BrandMark from "@/components/ui/brand-mark";
import { getObservabilitySummary } from "@/lib/api/observability";

type NavItem = {
  href: string;
  labelKey: MessageKey;
  icon: IconName;
  match: (pathname: string) => boolean;
  /** Live counter source; hidden while zero or unknown. */
  badge?: keyof NavCounts;
};

type NavGroup = {
  headingKey: MessageKey;
  items: NavItem[];
};

type NavCounts = { approvalsPending: number | null; running: number | null };

/**
 * One lightweight summary poll keeps the two operational counters useful
 * without making the shell own any page data. Workspace credentials are read
 * from the existing in-memory session provider.
 */
function useNavCounts(): NavCounts {
  const { connected, workspaceId, accessToken } = useFrontendSession();
  const [counts, setCounts] = useState<NavCounts>({
    approvalsPending: null,
    running: null,
  });

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
          if (cancelled) return;
          setCounts({
            approvalsPending: summary.approvals.pending,
            running: summary.current.running_count,
          });
        })
        .catch(() => {
          if (!cancelled) {
            setCounts({ approvalsPending: null, running: null });
          }
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
  }, [accessToken, connected, workspaceId]);

  return counts;
}

function prefixMatch(prefix: string) {
  return (pathname: string) => {
    return pathname === prefix || pathname.startsWith(`${prefix}/`);
  };
}

/**
 * Product navigation follows the work an operator is doing. Build resources,
 * run governance and settings are first-class destinations; nothing important
 * hides behind a "More" drawer.
 */
const PRIMARY_GROUPS: NavGroup[] = [
  {
    headingKey: "nav.workspaceGroup",
    items: [
      {
        href: "/",
        labelKey: "nav.home",
        icon: "workspace",
        match: (pathname) => pathname === "/",
      },
      {
        href: "/dashboard",
        labelKey: "nav.dashboard",
        icon: "dashboard",
        match: prefixMatch("/dashboard"),
      },
    ],
  },
  {
    headingKey: "nav.applications",
    items: [
      {
        href: "/research",
        labelKey: "nav.research",
        icon: "research",
        match: prefixMatch("/research"),
      },
      {
        href: "/incidents",
        labelKey: "nav.incidents",
        icon: "incidents",
        match: prefixMatch("/incidents"),
      },
      {
        href: "/analytics",
        labelKey: "nav.analytics",
        icon: "analytics",
        match: prefixMatch("/analytics"),
      },
      {
        href: "/support",
        labelKey: "nav.support",
        icon: "support",
        match: prefixMatch("/support"),
      },
    ],
  },
  {
    headingKey: "nav.buildGroup",
    items: [
      {
        href: "/agents",
        labelKey: "nav.agents",
        icon: "agents",
        match: prefixMatch("/agents"),
      },
      {
        href: "/knowledge",
        labelKey: "nav.knowledge",
        icon: "knowledge",
        match: prefixMatch("/knowledge"),
      },
      {
        href: "/tools",
        labelKey: "nav.tools",
        icon: "tools",
        match: prefixMatch("/tools"),
      },
    ],
  },
  {
    headingKey: "nav.operateGroup",
    items: [
      {
        href: "/runs",
        labelKey: "nav.runs",
        icon: "runs",
        match: prefixMatch("/runs"),
        badge: "running",
      },
      {
        href: "/approvals",
        labelKey: "nav.approvals",
        icon: "approvals",
        match: prefixMatch("/approvals"),
        badge: "approvalsPending",
      },
      {
        href: "/evaluations",
        labelKey: "nav.evaluations",
        icon: "evaluations",
        match: prefixMatch("/evaluations"),
      },
    ],
  },
  {
    headingKey: "nav.configureGroup",
    items: [
      {
        href: "/settings",
        labelKey: "nav.settings",
        icon: "settings",
        match: prefixMatch("/settings"),
      },
    ],
  },
];

/** Routes rendered without the control-plane chrome. */
const AUTH_ROUTES = ["/login", "/register"];

function isAuthRoute(pathname: string): boolean {
  return AUTH_ROUTES.some(
    (route) => pathname === route || pathname.startsWith(`${route}/`),
  );
}

const SidebarToggleGlyph = (
  <svg aria-hidden="true" fill="none" viewBox="0 0 24 24">
    <rect
      height="15.5"
      rx="2.2"
      stroke="currentColor"
      strokeWidth="1.7"
      width="17"
      x="3.5"
      y="4.25"
    />
    <path d="M9 4.8v14.4" stroke="currentColor" strokeWidth="1.7" />
  </svg>
);

const MenuGlyph = (
  <svg aria-hidden="true" fill="none" viewBox="0 0 24 24">
    <path
      d="M5 7h14M5 12h14M5 17h14"
      stroke="currentColor"
      strokeLinecap="round"
      strokeWidth="1.8"
    />
  </svg>
);

function NavLink({
  item,
  label,
  badge,
  badgeTitle,
  active,
  onNavigate,
}: {
  item: NavItem;
  label: string;
  badge: string | null;
  badgeTitle: string | undefined;
  active: boolean;
  onNavigate: () => void;
}) {
  return (
    <li>
      <Link
        href={item.href}
        className={`nav-link${active ? " nav-link-active" : ""}`}
        aria-current={active ? "page" : undefined}
        aria-label={label}
        title={label}
        onClick={onNavigate}
      >
        <span className="nav-link-label">
          <Icon name={item.icon} size={18} />
          <span className="nav-link-text">{label}</span>
        </span>
        {badge !== null && (
          <span
            className={`nav-badge${item.badge === "approvalsPending" ? " nav-badge-urgent" : ""}`}
            title={badgeTitle}
          >
            {badge}
          </span>
        )}
      </Link>
    </li>
  );
}

function SidebarContent({ onNavigate }: { onNavigate: () => void }) {
  const { t, formatNumber } = useI18n();
  const counts = useNavCounts();
  const pathname = usePathname() ?? "";

  return (
    <>
      <div className="sidebar-brand">
        <Link
          href="/"
          className="brand-link"
          aria-label={`AgentHub, ${t("brand.subtitle")}`}
          title={t("brand.subtitle")}
          onClick={onNavigate}
        >
          <span className="brand-mark"><BrandMark className="brand-mark-glyph" /></span>
          <span className="brand-copy">
            <span className="brand-name">AgentHub</span>
            <span className="brand-subtitle">{t("brand.subtitle")}</span>
          </span>
        </Link>
      </div>

      <nav className="sidebar-nav" aria-label={t("shell.primaryNav")}>
        {PRIMARY_GROUPS.map((group) => (
          <section className="nav-group" key={group.headingKey}>
            <h2 className="nav-group-heading">{t(group.headingKey)}</h2>
            <ul>
              {group.items.map((item) => {
                const rawCount = item.badge ? counts[item.badge] : null;
                const showBadge = rawCount !== null && rawCount > 0;
                const badgeTitle = !showBadge
                  ? undefined
                  : item.badge === "approvalsPending"
                    ? t("nav.badgePending", { count: rawCount })
                    : t("nav.badgeRunning", { count: rawCount });
                return (
                  <NavLink
                    item={item}
                    label={t(item.labelKey)}
                    badge={showBadge ? formatNumber(rawCount) : null}
                    badgeTitle={badgeTitle}
                    active={item.match(pathname)}
                    onNavigate={onNavigate}
                    key={item.href}
                  />
                );
              })}
            </ul>
          </section>
        ))}
      </nav>

      <div className="sidebar-footer">
        <span className="sidebar-security-dot" aria-hidden="true" />
        <p className="sidebar-footnote">{t("shell.footnote")}</p>
      </div>
    </>
  );
}

export default function AppShell({ children }: { children: ReactNode }) {
  return (
    <ThemeProvider>
      <LocaleProvider>
        <FrontendSessionProvider>
          <RouteTitle />
          <ShellGate>{children}</ShellGate>
        </FrontendSessionProvider>
      </LocaleProvider>
    </ThemeProvider>
  );
}

/**
 * Routes traffic between the auth pages and the control-plane shell. The
 * session provider remains the sole authentication boundary.
 */
function ShellGate({ children }: { children: ReactNode }) {
  const { status } = useFrontendSession();
  const pathname = usePathname() ?? "";
  const router = useRouter();
  const onAuthRoute = isAuthRoute(pathname);

  useEffect(() => {
    if (status === "UNAUTHENTICATED" && !onAuthRoute) router.replace("/login");
    if (status === "AUTHENTICATED" && onAuthRoute) router.replace("/");
  }, [onAuthRoute, router, status]);

  if (status === "BOOTSTRAPPING") return <BootstrapScreen />;
  if (onAuthRoute) {
    return status === "AUTHENTICATED" ? (
      <BootstrapScreen />
    ) : (
      <div className="auth-frame">{children}</div>
    );
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
  const { workspaceId, sessionId } = useFrontendSession();
  const pathname = usePathname() ?? "";
  const [navOpen, setNavOpen] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const menuButtonRef = useRef<HTMLButtonElement>(null);
  const sidebarRef = useRef<HTMLElement>(null);

  useEffect(() => {
    setNavOpen(false);
  }, [pathname]);

  useEffect(() => {
    document.body.classList.toggle("nav-open", navOpen);
    return () => document.body.classList.remove("nav-open");
  }, [navOpen]);

  useEffect(() => {
    const desktop = window.matchMedia("(min-width: 901px)");
    const closeOnDesktop = (event: MediaQueryListEvent) => {
      if (event.matches) setNavOpen(false);
    };
    desktop.addEventListener("change", closeOnDesktop);
    return () => desktop.removeEventListener("change", closeOnDesktop);
  }, []);

  useEffect(() => {
    if (!navOpen) return;
    const previousFocus =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const firstLink = sidebarRef.current?.querySelector<HTMLElement>(
      ".brand-link, .nav-link, button",
    );
    firstLink?.focus();

    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setNavOpen(false);
        menuButtonRef.current?.focus();
        return;
      }
      if (event.key !== "Tab" || !sidebarRef.current) return;
      const focusable = [
        ...sidebarRef.current.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ),
      ];
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      if (previousFocus?.isConnected) previousFocus.focus();
    };
  }, [navOpen]);

  return (
    <div
      className="app-shell"
      data-sidebar={sidebarCollapsed ? "collapsed" : "open"}
    >
      <a className="skip-link" href="#app-content">
        {t("shell.skipToContent")}
      </a>

      <aside
        className="sidebar"
        id="app-sidebar"
        ref={sidebarRef}
      >
        <SidebarContent key={`${sessionId}:${workspaceId}`} onNavigate={() => setNavOpen(false)} />
      </aside>

      {navOpen && (
        <button
          type="button"
          className="sidebar-scrim"
          aria-label={t("shell.closeMenu")}
          onClick={() => setNavOpen(false)}
        />
      )}

      <div className="app-frame">
        <header className="topbar">
          <button
            type="button"
            className="shell-control sidebar-toggle"
            aria-label={t(
              sidebarCollapsed ? "shell.expandSidebar" : "shell.collapseSidebar",
            )}
            aria-controls="app-sidebar"
            aria-expanded={!sidebarCollapsed}
            title={t(
              sidebarCollapsed ? "shell.expandSidebar" : "shell.collapseSidebar",
            )}
            onClick={() => setSidebarCollapsed((value) => !value)}
          >
            {SidebarToggleGlyph}
          </button>

          <button
            type="button"
            className="shell-control nav-toggle"
            ref={menuButtonRef}
            aria-label={t("shell.menu")}
            title={t("shell.menu")}
            aria-expanded={navOpen}
            aria-controls="app-sidebar"
            onClick={() => setNavOpen((open) => !open)}
          >
            {MenuGlyph}
          </button>

          <WorkspaceSelector />
          <span className="topbar-spacer" aria-hidden="true" />
          <div className="topbar-actions">
            <PageFinder groups={PRIMARY_GROUPS} />
            <ThemeSwitcher />
            <LanguageSwitcher />
            <UserMenu />
          </div>
        </header>

        <main className="app-content" id="app-content">
          <AutoBreadcrumbs />
          <Fragment key={`${sessionId}:${workspaceId}`}>{children}</Fragment>
        </main>
      </div>
    </div>
  );
}
