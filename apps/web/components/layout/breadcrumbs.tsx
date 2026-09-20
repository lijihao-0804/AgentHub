"use client";

import Link from "next/link";
import { Fragment } from "react";

import { useI18n } from "@/i18n/provider";

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
