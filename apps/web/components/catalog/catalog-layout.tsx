"use client";

import Link from "next/link";
import type { ReactNode } from "react";

import Icon from "@/components/ui/icon";

export type CatalogStat = {
  label: string;
  value: string;
  href?: string;
  tone?: "attention" | "success";
};

export function CatalogPageHeader({
  eyebrow,
  title,
  description,
  actions,
  context,
}: {
  eyebrow?: string;
  title: string;
  description: string;
  actions?: ReactNode;
  context?: ReactNode;
}) {
  return (
    <header className="catalog-page-header">
      <div className="catalog-page-heading">
        {eyebrow && <p className="catalog-eyebrow">{eyebrow}</p>}
        <h1>{title}</h1>
        <p className="catalog-page-description">{description}</p>
        {context && <div className="catalog-page-context">{context}</div>}
      </div>
      {actions && <div className="catalog-page-actions">{actions}</div>}
    </header>
  );
}

export function CatalogToolbar({
  searchLabel,
  searchPlaceholder,
  searchValue,
  onSearchChange,
  resultLabel,
  clearLabel,
  children,
}: {
  searchLabel: string;
  searchPlaceholder: string;
  searchValue: string;
  onSearchChange: (value: string) => void;
  resultLabel: string;
  clearLabel: string;
  children?: ReactNode;
}) {
  return (
    <div className="catalog-toolbar">
      <div className="catalog-search-control">
        <Icon name="search" size={17} />
        <input
          type="search"
          value={searchValue}
          onChange={(event) => onSearchChange(event.target.value)}
          placeholder={searchPlaceholder}
          aria-label={searchLabel}
          autoComplete="off"
        />
        {searchValue && (
          <button
            type="button"
            className="catalog-search-clear"
            onClick={() => onSearchChange("")}
            aria-label={clearLabel}
          >
            ×
          </button>
        )}
      </div>
      <div className="catalog-toolbar-meta">
        <span className="catalog-result-count" aria-live="polite">{resultLabel}</span>
        {children}
      </div>
    </div>
  );
}

export function CatalogStatStrip({ items }: { items: CatalogStat[] }) {
  return (
    <section className="catalog-stat-strip" aria-label={items.map((item) => item.label).join(", ")}>
      {items.map((item) => {
        const content = (
          <>
            <span className="catalog-stat-label">{item.label}</span>
            <strong className="catalog-stat-value">{item.value}</strong>
          </>
        );
        return item.href ? (
          <Link
            className={`catalog-stat${item.tone ? ` catalog-stat-${item.tone}` : ""}`}
            href={item.href}
            key={item.label}
          >
            {content}
          </Link>
        ) : (
          <div
            className={`catalog-stat${item.tone ? ` catalog-stat-${item.tone}` : ""}`}
            key={item.label}
          >
            {content}
          </div>
        );
      })}
    </section>
  );
}

export function CatalogEmptyNotice({
  title,
  hint,
}: {
  title: string;
  hint?: string;
}) {
  return (
    <div className="catalog-empty-notice">
      <span className="catalog-empty-mark" aria-hidden="true"><Icon name="search" size={18} /></span>
      <div>
        <p className="state-title">{title}</p>
        {hint && <p className="state-hint">{hint}</p>}
      </div>
    </div>
  );
}
