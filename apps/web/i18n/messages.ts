import type { FlattenMessageKeys, Locale } from "@/i18n/types";
import { enUS, type MessageSchema } from "@/i18n/locales/en-US";
import { zhCN } from "@/i18n/locales/zh-CN";

export { enUS, zhCN };

export type { MessageSchema };

export type MessageKey = FlattenMessageKeys<MessageSchema>;

const DICTIONARIES: Record<Locale, MessageSchema> = {
  "en-US": enUS,
  "zh-CN": zhCN,
};

function lookup(dict: unknown, key: string): string | undefined {
  let node: unknown = dict;
  for (const part of key.split(".")) {
    if (typeof node !== "object" || node === null) return undefined;
    node = (node as Record<string, unknown>)[part];
  }
  return typeof node === "string" ? node : undefined;
}

/** Resolve a message key; falls back to en-US, then to the raw key. */
export function resolveMessage(locale: Locale, key: string, values?: Record<string, string | number>): string {
  const template = lookup(DICTIONARIES[locale], key) ?? lookup(enUS, key);
  if (template === undefined) return key;
  if (!values) return template;
  return template.replace(/\{(\w+)\}/g, (match, name: string) =>
    name in values ? String(values[name]) : match,
  );
}

/**
 * Plain-language text for a backend error code.
 *
 * The backend sends English `message` text that is written for operators,
 * not for the person in front of the screen. When we recognise the code we
 * say it in the reader's language; when we do not, we keep the server's own
 * message rather than inventing one, because a vague localized sentence is
 * worse for support than a precise foreign one.
 */
export function apiErrorMessage(code: string, locale: Locale, fallback: string): string {
  return lookup(DICTIONARIES[locale], `errors.code.${code}`) ?? fallback;
}

/**
 * Human label for a backend status enum value. The raw value stays the
 * contract; this is presentation only. Unknown statuses fall back to the
 * raw value and never throw.
 */
export function statusLabel(status: string, locale: Locale): string {
  return lookup(DICTIONARIES[locale], `status.${status.toUpperCase()}`) ?? status;
}

/**
 * Human label for a backend failure category. Unknown categories fall back
 * to the raw value.
 */
export function failureCategoryLabel(category: string, locale: Locale): string {
  return lookup(DICTIONARIES[locale], `failureCategory.${category.toUpperCase()}`) ?? category;
}

/** Human label for an experiment purpose value. Unknown values fall back to the raw value. */
export function evaluationPurposeLabel(purpose: string, locale: Locale): string {
  return lookup(DICTIONARIES[locale], `evaluation.experiments.purposeLabels.${purpose}`) ?? purpose;
}

/** Human label for a timeline entry kind. Unknown kinds fall back to the raw value. */
export function timelineKindLabel(kind: string, locale: Locale): string {
  return lookup(DICTIONARIES[locale], `timeline.kind.${kind}`) ?? kind;
}
