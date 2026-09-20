"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import {
  formatCurrencyAmount,
  formatDateTime,
  formatDurationMs,
  formatNumber,
  formatPercent,
  formatUTCBucketDate,
} from "@/i18n/format";
import {
  apiErrorMessage,
  evaluationPurposeLabel,
  failureCategoryLabel,
  resolveMessage,
  statusLabel,
  timelineKindLabel,
} from "@/i18n/messages";
import { DEFAULT_LOCALE, LOCALE_STORAGE_KEY, type Locale } from "@/i18n/types";
import type { MessageKey } from "@/i18n/messages";

type I18nContextValue = {
  locale: Locale;
  setLocale: (locale: Locale) => void;
  t: (key: MessageKey, values?: Record<string, string | number>) => string;
  /** Reader-facing text for a backend error code, with the server message as fallback. */
  errorText: (code: string, fallback: string) => string;
  statusLabel: (status: string) => string;
  failureCategoryLabel: (category: string) => string;
  purposeLabel: (purpose: string) => string;
  timelineKindLabel: (kind: string) => string;
  formatDateTime: (value: string | Date) => string;
  formatUTCBucketDate: (value: string | Date) => string;
  formatNumber: (value: number) => string;
  formatPercent: (rate: number) => string;
  formatCurrencyAmount: (value: number | string | null | undefined, currency: string | null | undefined) => string;
  formatDurationMs: (value: number | null) => string;
};

const I18nContext = createContext<I18nContextValue | null>(null);

function isLocale(value: string | null): value is Locale {
  return value === "en-US" || value === "zh-CN";
}

export function resolveBrowserLocale(language: string | null | undefined): Locale {
  const normalized = (language ?? "").trim().toLowerCase();
  if (
    normalized === "zh" ||
    normalized === "zh-cn" ||
    normalized === "zh-sg" ||
    normalized === "zh-hans" ||
    normalized.startsWith("zh-hans-")
  ) {
    return "zh-CN";
  }
  return "en-US";
}

/**
 * Locale state is intentionally separate from FrontendSessionProvider:
 * switching language re-renders UI text only and never touches the
 * in-memory workspace session or its credentials.
 *
 * The initial render always uses the stable default (en-US) to avoid
 * hydration mismatches; the persisted or browser-derived preference is
 * applied after hydration. A brief language flash is accepted by design.
 */
export function LocaleProvider({ children }: { children: ReactNode }) {
  const [locale, setLocaleState] = useState<Locale>(DEFAULT_LOCALE);

  useEffect(() => {
    let preferred: Locale | null = null;
    try {
      const stored = window.localStorage.getItem(LOCALE_STORAGE_KEY);
      if (isLocale(stored)) preferred = stored;
    } catch {
      // Storage unavailable (e.g. privacy mode) — fall through.
    }
    if (!preferred) {
      const language = typeof navigator !== "undefined" ? navigator.language : "";
      preferred = resolveBrowserLocale(language);
    }
    if (preferred) setLocaleState(preferred);
  }, []);

  useEffect(() => {
    document.documentElement.lang = locale;
  }, [locale]);

  const setLocale = useCallback((next: Locale) => {
    setLocaleState(next);
    try {
      window.localStorage.setItem(LOCALE_STORAGE_KEY, next);
    } catch {
      // Preference persistence is best-effort only.
    }
  }, []);

  const value = useMemo<I18nContextValue>(
    () => ({
      locale,
      setLocale,
      t: (key, values) => resolveMessage(locale, key, values),
      errorText: (code, fallback) => apiErrorMessage(code, locale, fallback),
      statusLabel: (status) => statusLabel(status, locale),
      failureCategoryLabel: (category) => failureCategoryLabel(category, locale),
      purposeLabel: (purpose) => evaluationPurposeLabel(purpose, locale),
      timelineKindLabel: (kind) => timelineKindLabel(kind, locale),
      formatDateTime: (value) => formatDateTime(value, locale),
      formatUTCBucketDate: (value) => formatUTCBucketDate(value, locale),
      formatNumber: (value) => formatNumber(value, locale),
      formatPercent: (rate) => formatPercent(rate, locale),
      formatCurrencyAmount: (value, currency) => formatCurrencyAmount(value, currency),
      formatDurationMs: (value) => formatDurationMs(value, locale),
    }),
    [locale, setLocale],
  );

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18nContextValue {
  const value = useContext(I18nContext);
  if (!value) {
    throw new Error("useI18n must be used inside LocaleProvider.");
  }
  return value;
}
