/** Supported product locales. en-US is the canonical default. */
export const LOCALES = ["en-US", "zh-CN"] as const;

export type Locale = (typeof LOCALES)[number];

export const DEFAULT_LOCALE: Locale = "en-US";

export const LOCALE_STORAGE_KEY = "agenthub.locale";

export const LOCALE_TAGS: Record<Locale, string> = {
  "en-US": "en-US",
  "zh-CN": "zh-CN",
};

/** Dot path union of every message key in the canonical (en-US) schema. */
export type FlattenMessageKeys<T> = T extends string
  ? never
  : {
      [K in keyof T & string]: T[K] extends string ? K : `${K}.${FlattenMessageKeys<T[K]>}`;
    }[keyof T & string];
