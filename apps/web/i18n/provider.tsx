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
} from "./format";
import { failureCategoryLabel, resolveMessage, statusLabel, timelineKindLabel } from "./messages";
import { DEFAULT_LOCALE, LOCALE_STORAGE_KEY, type Locale } from "./types";
import type { MessageKey } from "./messages";

type I18nContextValue = {
  locale: Locale;
  setLocale: (locale: Locale) => void;
  t: (key: MessageKey, values?: Record<string, string | number>) => string;
  statusLabel: (status: string) => string;
  failureCategoryLabel: (category: string) => string;
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
      // Only Simplified Chinese locales map to zh-CN; zh-TW/zh-HK stay en-US.
      if (language === "zh-CN" || language === "zh-SG" || language === "zh-Hans") {
        preferred = "zh-CN";
      }
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
      statusLabel: (status) => statusLabel(status, locale),
      failureCategoryLabel: (category) => failureCategoryLabel(category, locale),
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
