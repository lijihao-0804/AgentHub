"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";

import SessionControl from "./session-control";
import { FrontendSessionProvider } from "./session-provider";

type NavItem = { href: string; label: string; match: (pathname: string) => boolean };

function prefixMatch(prefix: string): (pathname: string) => boolean {
  return (pathname) => pathname === prefix || pathname.startsWith(`${prefix}/`);
}

const NAV_GROUPS: Array<{ heading: string; items: NavItem[] }> = [
  {
    heading: "Overview",
    items: [
      { href: "/dashboard", label: "Dashboard", match: prefixMatch("/dashboard") },
      { href: "/runs", label: "Runs", match: prefixMatch("/runs") },
      { href: "/approvals", label: "Approvals", match: prefixMatch("/approvals") },
    ],
  },
  {
    heading: "Knowledge",
    items: [
      {
        href: "/knowledge/playground",
        label: "Retrieval Playground",
        match: prefixMatch("/knowledge"),
      },
    ],
  },
];

function NavLink({ item, onNavigate }: { item: NavItem; onNavigate: () => void }) {
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
        {item.label}
      </Link>
    </li>
  );
}

function SidebarContent({ onNavigate }: { onNavigate: () => void }) {
  return (
    <>
      <div className="sidebar-brand">
        <Link href="/" className="brand-link" onClick={onNavigate}>
          <span className="brand-name">AgentHub</span>
          <span className="brand-subtitle">Enterprise Agent Runtime &amp; Control Plane</span>
        </Link>
      </div>
      <nav className="sidebar-nav" aria-label="Primary">
        {NAV_GROUPS.map((group) => (
          <div className="nav-group" key={group.heading}>
            <p className="nav-group-heading">{group.heading}</p>
            <ul>
              {group.items.map((item) => (
                <NavLink item={item} onNavigate={onNavigate} key={item.href} />
              ))}
            </ul>
          </div>
        ))}
        <div className="nav-group">
          <p className="nav-group-heading">Coming later</p>
          <ul>
            <li>
              <span className="nav-link nav-link-disabled" aria-disabled="true">
                Evaluations
                <span className="nav-soon">Planned</span>
              </span>
            </li>
          </ul>
        </div>
      </nav>
      <p className="sidebar-footnote">Session lives in memory only · no token storage</p>
    </>
  );
}

export default function AppShell({ children }: { children: ReactNode }) {
  const [navOpen, setNavOpen] = useState(false);

  useEffect(() => {
    document.body.classList.toggle("nav-open", navOpen);
    return () => document.body.classList.remove("nav-open");
  }, [navOpen]);

  return (
    <FrontendSessionProvider>
      <div className="app-shell">
        <a className="skip-link" href="#app-content">
          Skip to content
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
              <span aria-hidden="true">☰</span> Menu
            </button>
            <SessionControl />
          </header>
          <main className="app-content" id="app-content">
            {children}
          </main>
        </div>
      </div>
    </FrontendSessionProvider>
  );
}
