import { LOCALE_TAGS, type Locale } from "./types";

/**
 * Centralized locale-aware formatting. Raw contract values (hashes, UUIDs,
 * token counts, currency codes) are formatted by callers only where it is
 * safe; these helpers never change business semantics.
 */

export function formatDateTime(value: string | Date, locale: Locale): string {
  const date = typeof value === "string" ? new Date(value) : value;
  return new Intl.DateTimeFormat(LOCALE_TAGS[locale], {
    dateStyle: "short",
    timeStyle: "medium",
  }).format(date);
}

/**
 * Bucket labels for the trend chart. The product presents these buckets in
 * UTC; only the language changes, never the timezone semantics.
 */
export function formatUTCBucketDate(value: string | Date, locale: Locale): string {
  const date = typeof value === "string" ? new Date(value) : value;
  return new Intl.DateTimeFormat(LOCALE_TAGS[locale], {
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  }).format(date);
}

export function formatNumber(value: number, locale: Locale): string {
  return new Intl.NumberFormat(LOCALE_TAGS[locale]).format(value);
}

export function formatPercent(rate: number, locale: Locale): string {
  return new Intl.NumberFormat(LOCALE_TAGS[locale], {
    style: "percent",
    maximumFractionDigits: 1,
  }).format(rate);
}

/**
 * Currency amounts stay amount-string + currency-code from the backend.
 * Locale switching is never a conversion: "12.30 USD" stays "12.30 USD".
 */
export function formatCurrencyAmount(
  value: number | string | null | undefined,
  currency: string | null | undefined,
): string {
  if (value === null || value === undefined) return "";
  return `${value} ${currency ?? ""}`.trim();
}

export function formatDurationMs(value: number | null, locale: Locale): string {
  if (value === null || !Number.isFinite(value)) return "";
  return `${formatNumber(Math.round(value), locale)} ms`;
}
