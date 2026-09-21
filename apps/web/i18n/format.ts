import { LOCALE_TAGS, type Locale } from "@/i18n/types";

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

/**
 * Counts that the backend computes as averages (tokens per run, calls per run)
 * arrive as floats. A token is not divisible, so the UI rounds: "5,680", never
 * "5679.52". Use this for anything that counts discrete things.
 */
export function formatCount(value: number | null | undefined, locale: Locale): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "";
  return formatNumber(Math.round(value), locale);
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
 *
 * Rounding is presentation only. Per-run LLM cost is genuinely sub-cent, so a
 * fixed 2-decimal format would collapse every row to "0.00 USD"; instead the
 * scale picks the precision: >= 1 gets 2 decimals, cents get up to 4, and
 * anything smaller keeps 2 significant digits ("0.00055 USD"). Callers that
 * need the exact backend value should surface it in a title/tooltip.
 */
export function formatCurrencyAmount(
  value: number | string | null | undefined,
  currency: string | null | undefined,
  locale: Locale,
): string {
  if (value === null || value === undefined) return "";
  const amount = typeof value === "string" ? Number(value) : value;
  const suffix = currency ? ` ${currency}` : "";
  // Non-numeric amounts are contract data we must not mangle.
  if (!Number.isFinite(amount)) return `${value}${suffix}`.trim();

  const magnitude = Math.abs(amount);
  let options: Intl.NumberFormatOptions;
  if (magnitude === 0 || magnitude >= 1) {
    options = { minimumFractionDigits: 2, maximumFractionDigits: 2 };
  } else if (magnitude >= 0.01) {
    options = { minimumFractionDigits: 2, maximumFractionDigits: 4 };
  } else {
    options = { maximumSignificantDigits: 2 };
  }
  return `${new Intl.NumberFormat(LOCALE_TAGS[locale], options).format(amount)}${suffix}`;
}

/**
 * Durations are stored in milliseconds but are not read in milliseconds once
 * they pass a second. "26,636.568 ms" is a number a human has to decode;
 * "26.6 s" is one they can read. The unit is promoted, never the value's
 * meaning — ms below a second, seconds below a minute, minutes above that.
 */
export function formatDurationMs(value: number | null | undefined, locale: Locale): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "";
  const magnitude = Math.abs(value);
  if (magnitude < 1000) return `${formatNumber(Math.round(value), locale)} ms`;
  if (magnitude < 60_000) {
    const seconds = value / 1000;
    // Keep three significant digits so 4.72 s and 26.6 s read at the same weight.
    return `${new Intl.NumberFormat(LOCALE_TAGS[locale], {
      maximumFractionDigits: magnitude < 10_000 ? 2 : 1,
    }).format(seconds)} s`;
  }
  const minutes = value / 60_000;
  return `${new Intl.NumberFormat(LOCALE_TAGS[locale], {
    maximumFractionDigits: 1,
  }).format(minutes)} min`;
}
