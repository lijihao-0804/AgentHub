import type { MessageKey } from "@/i18n/messages";

/** Minimal shape of the translator so this stays a pure formatting helper. */
type Translate = (key: MessageKey, values?: Record<string, string | number>) => string;

/**
 * Human-readable position of a citation inside its source document.
 *
 * The backend describes positions structurally (`page`, `page_range`,
 * `text_range`) and deliberately does not send display text, so the wording is
 * ours to choose and therefore ours to localize. An unrecognised shape falls
 * through to raw JSON: it is not pretty, but it is honest, and it keeps a new
 * locator type visible instead of silently rendering as an em dash.
 */
export function formatLocator(locator: Record<string, unknown> | null, t: Translate): string {
  if (!locator) return "—";
  if (locator.type === "page") {
    return t("playground.locator.page", { page: String(locator.page) });
  }
  if (locator.type === "page_range") {
    return t("playground.locator.pageRange", {
      start: String(locator.start_page),
      end: String(locator.end_page),
    });
  }
  if (locator.type === "text_range") {
    return t("playground.locator.textRange", {
      start: String(locator.char_start),
      end: String(locator.char_end),
    });
  }
  return JSON.stringify(locator);
}
