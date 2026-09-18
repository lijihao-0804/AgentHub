export function formatLocator(locator: Record<string, unknown> | null): string {
  if (!locator) return "—";
  if (locator.type === "page") return `Page ${String(locator.page)}`;
  if (locator.type === "page_range") {
    return `Pages ${String(locator.start_page)}–${String(locator.end_page)}`;
  }
  if (locator.type === "text_range") {
    return `Characters ${String(locator.char_start)}–${String(locator.char_end)}`;
  }
  return JSON.stringify(locator);
}
