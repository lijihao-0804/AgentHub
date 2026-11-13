"use client";

import { useTheme } from "@/components/providers/theme-provider";
import { useI18n } from "@/i18n/provider";

const SunIcon = (
  <svg aria-hidden="true" fill="none" viewBox="0 0 20 20">
    <circle cx="10" cy="10" r="3.2" stroke="currentColor" strokeWidth="1.6" />
    <path
      d="M10 2.2v1.5M10 16.3v1.5M2.2 10h1.5M16.3 10h1.5M4.5 4.5l1.1 1.1M14.4 14.4l1.1 1.1M15.5 4.5l-1.1 1.1M5.6 14.4l-1.1 1.1"
      stroke="currentColor"
      strokeLinecap="round"
      strokeWidth="1.6"
    />
  </svg>
);

const MoonIcon = (
  <svg aria-hidden="true" fill="none" viewBox="0 0 20 20">
    <path
      d="M16.4 12.7A6.8 6.8 0 0 1 7.3 3.6 6.8 6.8 0 1 0 16.4 12.7Z"
      stroke="currentColor"
      strokeLinejoin="round"
      strokeWidth="1.6"
    />
  </svg>
);

/**
 * Compact topbar theme switcher, built like the language switcher: real
 * buttons, keyboard operable, and the active theme marked with
 * aria-pressed rather than colour alone.
 */
export default function ThemeSwitcher() {
  const { theme, setTheme } = useTheme();
  const { t } = useI18n();
  return (
    // Shares the pill styling with the language switch; the second class is
    // what lets a narrow topbar drop this one and keep that one.
    <div className="lang-switch theme-switch" role="group" aria-label={t("shell.theme")}>
      <button
        type="button"
        className="lang-option theme-option"
        aria-pressed={theme === "light"}
        aria-label={t("shell.themeLight")}
        title={t("shell.themeLight")}
        onClick={() => setTheme("light")}
      >
        {SunIcon}
      </button>
      <button
        type="button"
        className="lang-option theme-option"
        aria-pressed={theme === "dark"}
        aria-label={t("shell.themeDark")}
        title={t("shell.themeDark")}
        onClick={() => setTheme("dark")}
      >
        {MoonIcon}
      </button>
    </div>
  );
}
