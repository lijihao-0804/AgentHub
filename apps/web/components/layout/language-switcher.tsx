"use client";

import { useI18n } from "@/i18n/provider";

export default function LanguageSwitcher() {
  const { locale, setLocale, t } = useI18n();
  const next = locale === "zh-CN" ? "en-US" : "zh-CN";
  const label = t(next === "en-US" ? "shell.languageEn" : "shell.languageZh");
  return (
    <button type="button" className="shell-control language-toggle" lang={next}
      aria-label={label} title={label} onClick={() => setLocale(next)}>
      {next === "en-US" ? "EN" : "中文"}
    </button>
  );
}
