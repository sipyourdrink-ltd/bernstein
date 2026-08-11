export const THEMES = ['dark', 'light', 'system'] as const;
export type Theme = (typeof THEMES)[number];
export type ResolvedTheme = 'dark' | 'light';

export const DEFAULT_THEME = 'system' satisfies Theme;
export const DEFAULT_THEME_STORAGE_KEY = 'bernstein-theme';

export function isTheme(theme: string | null): theme is Theme {
  return THEMES.some((candidate) => candidate === theme);
}

export function resolveTheme(theme: Theme | null | undefined, systemTheme: ResolvedTheme): ResolvedTheme {
  return theme === 'dark' || theme === 'light' ? theme : systemTheme;
}

export function readStoredTheme(storageKey: string): Theme | null {
  try {
    const theme = window.localStorage.getItem(storageKey);
    return isTheme(theme) ? theme : null;
  } catch {
    return null;
  }
}

export function saveStoredTheme(storageKey: string, theme: Theme): void {
  try {
    window.localStorage.setItem(storageKey, theme);
  } catch {
    // Storage can be blocked in private browsing; retain the in-memory preference.
  }
}
