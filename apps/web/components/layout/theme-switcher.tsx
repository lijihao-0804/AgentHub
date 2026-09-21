"use client";

import { useTheme } from "@/components/providers/theme-provider";
import { useI18n } from "@/i18n/provider";

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
        className="lang-option"
        aria-pressed={theme === "dark"}
        aria-label={t("shell.themeDark")}
        title={t("shell.themeDark")}
        onClick={() => setTheme("dark")}
      >
        <span aria-hidden="true">☾</span>
      </button>
      <button
        type="button"
        className="lang-option"
        aria-pressed={theme === "light"}
        aria-label={t("shell.themeLight")}
        title={t("shell.themeLight")}
        onClick={() => setTheme("light")}
      >
        <span aria-hidden="true">☀</span>
      </button>
    </div>
  );
}
