"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Fragment, useLayoutEffect, useMemo, useState, useRef } from "react";

import { useI18n } from "@/i18n/provider";
import type { MessageKey } from "@/i18n/messages";

export type Crumb = {
  label: string;
  /** Omitted on the last crumb: the page you are already on is not a link. */
  href?: string;
};

/**
 * Where the current page sits in the product.
 *
 * This replaces the assorted "Back to …" ghost buttons that each detail page
 * grew independently. A back button answers one question — how do I leave —
 * and answers it differently on every page. A trail answers where am I, makes
 * every ancestor reachable in one click rather than one hop at a time, and
 * looks the same everywhere, so a reader learns it once.
 */
export default function Breadcrumbs({ items }: { items: Crumb[] }) {
  const { t } = useI18n();
  if (items.length === 0) return null;
  return (
    <nav className="breadcrumbs" aria-label={t("shell.breadcrumbs")}>
      <ol>
        {items.map((item, index) => {
          const last = index === items.length - 1;
          return (
            <Fragment key={`${item.href ?? "current"}-${index}`}>
              <li>
                {item.href && !last ? (
                  <Link href={item.href}>{item.label}</Link>
                ) : (
                  <span aria-current="page">{item.label}</span>
                )}
              </li>
              {!last && (
                <li className="breadcrumb-separator" aria-hidden="true">
                  /
                </li>
              )}
            </Fragment>
          );
        })}
      </ol>
    </nav>
  );
}
/** Static route segments → their nav label. Detail segments (resource ids)
 * collapse to one generic crumb; page titles stay the page's own job. */
const SEGMENT_LABELS: Record<string, MessageKey> = {
  dashboard: "nav.dashboard",
  runs: "nav.runs",
  compare: "nav.runCompare",
  approvals: "nav.approvals",
  research: "nav.research",
  incidents: "nav.incidents",
  analytics: "nav.analytics",
  support: "nav.support",
  agents: "nav.agents",
  knowledge: "nav.knowledge",
  playground: "nav.retrievalPlayground",
  tools: "nav.tools",
  evaluations: "nav.evaluations",
  datasets: "nav.datasets",
  experiments: "nav.experiments",
  "release-gates": "nav.releaseGates",
  pricing: "nav.pricing",
  settings: "nav.settings",
  models: "nav.models",
  versions: "nav.versions",
};

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * Trail a route generates on its own: static segments carry their nav label,
 * resource ids become a single "detail" crumb. Returns null for top-level
 * routes, where a one-item trail would just echo the page heading.
 */
export function autoBreadcrumbs(pathname: string, t: (key: MessageKey) => string): Crumb[] | null {
  const segments = pathname.split("/").filter(Boolean);
  if (segments.length < 2) return null;
  const items: Crumb[] = [];
  let href = "";
  for (const segment of segments) {
    href += `/${segment}`;
    const labelKey = SEGMENT_LABELS[segment];
    const isLast = segment === segments[segments.length - 1];
    if (labelKey) {
      items.push({ label: t(labelKey), href: isLast ? undefined : href });
    } else {
      const label = UUID_PATTERN.test(segment) ? t("agents.detail") : decodeURIComponent(segment);
      items.push({ label, href: isLast ? undefined : href });
    }
  }
  return items.length >= 2 ? items : null;
}

/** Shell-level fallback: renders the generated trail unless the page brought
 * its own. The first paint stays empty so a page-managed trail never flashes
 * twice. */
export function AutoBreadcrumbs() {
  const pathname = usePathname() ?? "";
  const { t } = useI18n();
  const items = useMemo(() => autoBreadcrumbs(pathname, t), [pathname, t]);
  const [pageOwnsTrail, setPageOwnsTrail] = useState(true);
  const fallbackRef = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const update = () => setPageOwnsTrail(
      [...document.querySelectorAll("main .breadcrumbs")].some((trail) => !fallbackRef.current?.contains(trail)),
    );
    update();
    const main = document.querySelector("main");
    const observer = new MutationObserver(update);
    if (main) observer.observe(main, { childList: true, subtree: true });
    return () => observer.disconnect();
  }, [pathname]);
  if (items === null || pageOwnsTrail) return null;
  return <div ref={fallbackRef}><Breadcrumbs items={items} /></div>;
}
