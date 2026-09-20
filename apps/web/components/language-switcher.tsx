"use client";

import { useI18n } from "../i18n/provider";

/**
 * Compact topbar language switcher. Real buttons keep it keyboard
 * operable; the active language is marked with aria-pressed (not color
 * alone) and localized accessible names.
 */
export default function LanguageSwitcher() {
  const { locale, setLocale, t } = useI18n();
  return (
    <div className="lang-switch" role="group" aria-label={t("shell.language")}>
      <button
        type="button"
        className="lang-option"
        aria-pressed={locale === "en-US"}
        aria-label={t("shell.languageEn")}
        onClick={() => setLocale("en-US")}
      >
        EN
      </button>
      <button
        type="button"
        className="lang-option"
        aria-pressed={locale === "zh-CN"}
        aria-label={t("shell.languageZh")}
        onClick={() => setLocale("zh-CN")}
      >
        中文
      </button>
    </div>
  );
}
