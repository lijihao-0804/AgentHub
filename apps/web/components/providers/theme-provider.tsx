"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

/**
 * Application theme.
 *
 * This is the product's own theme, applied to every surface as a
 * `data-theme` attribute on <html> that swaps the colour tokens in
 * globals.css. It is unrelated to the Next.js dev-tools overlay, whose
 * own Theme control only repaints that overlay and does not exist in a
 * production build.
 *
 * Theme state is deliberately separate from FrontendSessionProvider:
 * switching theme repaints UI only and never touches the in-memory
 * workspace session or its credentials.
 */

export type Theme = "dark" | "light";

export const THEME_STORAGE_KEY = "agenthub.theme";

const DEFAULT_THEME: Theme = "light";

/**
 * The provider is rendered on the server, so use a browser-only layout effect
 * after hydration. It runs before the next paint and avoids the visible theme
 * swap that an ordinary effect can produce.
 */
const useBrowserLayoutEffect =
  typeof window === "undefined" ? useEffect : useLayoutEffect;

type ThemeContextValue = {
  theme: Theme;
  setTheme: (theme: Theme) => void;
};

const ThemeContext = createContext<ThemeContextValue | null>(null);

function isTheme(value: string | null): value is Theme {
  return value === "dark" || value === "light";
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  // The server and first client render agree on the product default. A saved
  // preference is applied before the next browser paint.
  const [theme, setThemeState] = useState<Theme>(DEFAULT_THEME);

  useBrowserLayoutEffect(() => {
    let preferred: Theme | null = null;
    try {
      const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
      if (isTheme(stored)) preferred = stored;
    } catch {
      // Storage unavailable (e.g. privacy mode) — fall through.
    }
    const resolved = preferred ?? DEFAULT_THEME;
    document.documentElement.dataset.theme = resolved;
    setThemeState(resolved);
  }, []);

  useBrowserLayoutEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);

  const setTheme = useCallback((next: Theme) => {
    document.documentElement.dataset.theme = next;
    setThemeState(next);
    try {
      window.localStorage.setItem(THEME_STORAGE_KEY, next);
    } catch {
      // Preference persistence is best-effort only.
    }
  }, []);

  const value = useMemo<ThemeContextValue>(() => ({ theme, setTheme }), [theme, setTheme]);
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeContextValue {
  const value = useContext(ThemeContext);
  if (!value) throw new Error("useTheme must be used inside ThemeProvider");
  return value;
}
