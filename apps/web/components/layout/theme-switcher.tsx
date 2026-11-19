"use client";

import { useSyncExternalStore } from "react";

import { DARK_SCHEME_QUERY } from "@/lib/appearance";
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

function subscribeToSystemTheme(update: () => void) {
  const media = window.matchMedia(DARK_SCHEME_QUERY);
  media.addEventListener("change", update);
  return () => media.removeEventListener("change", update);
}

const systemIsDark = () => window.matchMedia(DARK_SCHEME_QUERY).matches;
const defaultIsDark = () => false;

export default function ThemeSwitcher() {
  const { mode, setMode } = useTheme();
  const { t } = useI18n();
  const systemDark = useSyncExternalStore(subscribeToSystemTheme, systemIsDark, defaultIsDark);
  const dark = mode === "dark" || (mode === "system" && systemDark);
  const label = t(dark ? "shell.themeLight" : "shell.themeDark");
  return (
    <button type="button" className="shell-control theme-toggle" aria-label={label}
      title={label} onClick={() => setMode(dark ? "light" : "dark")}>
      {dark ? SunIcon : MoonIcon}
    </button>
  );
}
