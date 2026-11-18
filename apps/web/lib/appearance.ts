export const THEME_STORAGE_KEY = "agenthub.theme";
export const ACCENT_STORAGE_KEY = "agenthub.accent.v1";
export const DARK_SCHEME_QUERY = "(prefers-color-scheme: dark)";
export const THEME_MODES = ["light", "dark", "system"] as const;
export type ThemeMode = (typeof THEME_MODES)[number];

export const ACCENT_PRESETS = [
  { name: "cobalt", color: "#3458aa" },
  { name: "lagoon", color: "#087b76" },
  { name: "iris", color: "#8556a8" },
  { name: "copper", color: "#ad552f" },
  { name: "graphite", color: "#536779" },
] as const;

export type Appearance = { mode: ThemeMode; accent: string };
export const DEFAULT_APPEARANCE: Appearance = { mode: "light", accent: ACCENT_PRESETS[0].color };

export function parseMode(value: string | null): ThemeMode {
  return value === "dark" || value === "system" ? value : "light";
}

/** A complete sRGB value is the only user input allowed into a CSS property. */
export function parseAccent(value: string | null): string | null {
  const hex = value?.trim().replace(/^#/, "");
  return hex && /^[\da-f]{6}$/i.test(hex) ? `#${hex.toLowerCase()}` : null;
}

export function readAppearance(): Appearance {
  try {
    return {
      mode: parseMode(window.localStorage.getItem(THEME_STORAGE_KEY)),
      accent: parseAccent(window.localStorage.getItem(ACCENT_STORAGE_KEY)) ?? DEFAULT_APPEARANCE.accent,
    };
  } catch {
    return DEFAULT_APPEARANCE;
  }
}

export function applyAppearance({ mode, accent }: Appearance, systemDark: boolean) {
  const root = document.documentElement;
  root.dataset.theme = mode === "system" ? (systemDark ? "dark" : "light") : mode;
  root.dataset.accent = accent;
  root.style.setProperty("--user-accent", accent);
}

// Runs in the document head, before CSS paints the signed-out or signed-in shell.
// Only known modes and a six-digit sRGB value can reach the document.
export const appearanceBootstrap = `(() => {
  const root = document.documentElement;
  let mode = "light", accent = "${DEFAULT_APPEARANCE.accent}";
  try {
    const savedMode = localStorage.getItem("${THEME_STORAGE_KEY}");
    if (savedMode === "dark" || savedMode === "system") mode = savedMode;
    const hex = (localStorage.getItem("${ACCENT_STORAGE_KEY}") || "").trim().replace(/^#/, "");
    if (/^[0-9a-f]{6}$/i.test(hex)) accent = "#" + hex.toLowerCase();
  } catch {}
  root.dataset.theme = mode === "system" ? (matchMedia("${DARK_SCHEME_QUERY}").matches ? "dark" : "light") : mode;
  root.dataset.accent = accent;
  root.style.setProperty("--user-accent", accent);
})();`;
