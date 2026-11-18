"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import {
  ACCENT_STORAGE_KEY,
  DARK_SCHEME_QUERY,
  DEFAULT_APPEARANCE,
  THEME_STORAGE_KEY,
  applyAppearance,
  parseAccent,
  readAppearance,
  type Appearance,
  type ThemeMode,
} from "@/lib/appearance";

type ThemeContextValue = Appearance & {
  canPersist: boolean;
  setMode: (mode: ThemeMode) => void;
  setAccent: (accent: string) => void;
  resetAppearance: () => void;
};

const ThemeContext = createContext<ThemeContextValue | null>(null);
const useBrowserLayoutEffect = typeof window === "undefined" ? useEffect : useLayoutEffect;

function savePreference(key: string, value: string) {
  try {
    window.localStorage.setItem(key, value);
    return true;
  } catch {
    return false;
  }
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [appearance, setAppearance] = useState(DEFAULT_APPEARANCE);
  const [canPersist, setCanPersist] = useState(true);
  const current = useRef(appearance);

  const apply = useCallback((next: Appearance) => {
    current.current = next;
    applyAppearance(next, window.matchMedia(DARK_SCHEME_QUERY).matches);
    setAppearance(next);
  }, []);

  useBrowserLayoutEffect(() => {
    apply(readAppearance());
    const media = window.matchMedia(DARK_SCHEME_QUERY);
    const onSystemChange = () => {
      if (current.current.mode === "system") applyAppearance(current.current, media.matches);
    };
    const onStorage = (event: StorageEvent) => {
      if (event.key === null || event.key === THEME_STORAGE_KEY || event.key === ACCENT_STORAGE_KEY) {
        apply(readAppearance());
      }
    };
    media.addEventListener("change", onSystemChange);
    window.addEventListener("storage", onStorage);
    return () => {
      media.removeEventListener("change", onSystemChange);
      window.removeEventListener("storage", onStorage);
    };
  }, [apply]);

  const setMode = useCallback((mode: ThemeMode) => {
    apply({ ...current.current, mode });
    setCanPersist(savePreference(THEME_STORAGE_KEY, mode));
  }, [apply]);

  const setAccent = useCallback((value: string) => {
    const accent = parseAccent(value);
    if (!accent) return;
    apply({ ...current.current, accent });
    setCanPersist(savePreference(ACCENT_STORAGE_KEY, accent));
  }, [apply]);

  const resetAppearance = useCallback(() => {
    apply(DEFAULT_APPEARANCE);
    const savedMode = savePreference(THEME_STORAGE_KEY, DEFAULT_APPEARANCE.mode);
    const savedAccent = savePreference(ACCENT_STORAGE_KEY, DEFAULT_APPEARANCE.accent);
    setCanPersist(savedMode && savedAccent);
  }, [apply]);

  const value = useMemo(() => ({ ...appearance, canPersist, setMode, setAccent, resetAppearance }),
    [appearance, canPersist, setMode, setAccent, resetAppearance]);
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme() {
  const value = useContext(ThemeContext);
  if (!value) throw new Error("useTheme must be used inside ThemeProvider");
  return value;
}
