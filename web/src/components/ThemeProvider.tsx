import { createContext, useContext, useEffect, useState, type ReactNode } from 'react';
import {
  DEFAULT_THEME,
  DEFAULT_THEME_STORAGE_KEY,
  readStoredTheme,
  resolveTheme,
  saveStoredTheme,
  type ResolvedTheme,
  type Theme,
} from './theme';

export type { ResolvedTheme, Theme } from './theme';
export { DEFAULT_THEME, DEFAULT_THEME_STORAGE_KEY } from './theme';

type ThemeProviderProps = {
  children: ReactNode;
  defaultTheme?: Theme;
  storageKey?: string;
};

type ThemeProviderState = {
  theme: Theme;
  /** The actually-rendered theme (resolves `system` against the OS preference). */
  resolvedTheme: ResolvedTheme;
  setTheme: (t: Theme) => void;
};

const ThemeProviderContext = createContext<ThemeProviderState>({
  theme: 'system',
  resolvedTheme: 'dark',
  setTheme: () => null,
});

function readSystemTheme(): ResolvedTheme {
  try {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return 'dark';
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  } catch {
    return 'dark';
  }
}

export function ThemeProvider({
  children,
  defaultTheme = DEFAULT_THEME,
  storageKey = DEFAULT_THEME_STORAGE_KEY,
}: ThemeProviderProps) {
  const [theme, setThemeState] = useState<Theme>(() => readStoredTheme(storageKey) ?? defaultTheme);
  const [systemTheme, setSystemTheme] = useState<ResolvedTheme>(() => readSystemTheme());

  // Track OS preference changes so that toggle reflects what the user actually sees.
  useEffect(() => {
    try {
      if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return;
      const mql = window.matchMedia('(prefers-color-scheme: dark)');
      const handler = (e: MediaQueryListEvent) => setSystemTheme(e.matches ? 'dark' : 'light');
      if (typeof mql.addEventListener === 'function' && typeof mql.removeEventListener === 'function') {
        mql.addEventListener('change', handler);
        return () => mql.removeEventListener('change', handler);
      }
      // Older Safari fallback.
      if (typeof mql.addListener === 'function' && typeof mql.removeListener === 'function') {
        mql.addListener(handler);
        return () => mql.removeListener(handler);
      }
    } catch {
      // Media queries are optional; retain the initial safe system fallback.
    }
  }, []);

  const resolvedTheme = resolveTheme(theme, systemTheme);

  useEffect(() => {
    try {
      const root = window.document.documentElement;
      root.classList.remove('light', 'dark');
      root.classList.add(resolvedTheme);
    } catch {
      // The head bootstrap and provider are best effort; rendering must continue without them.
    }
  }, [resolvedTheme]);

  const setTheme = (t: Theme) => {
    saveStoredTheme(storageKey, t);
    setThemeState(t);
  };

  return (
    <ThemeProviderContext.Provider value={{ theme, resolvedTheme, setTheme }}>
      {children}
    </ThemeProviderContext.Provider>
  );
}

export const useTheme = () => useContext(ThemeProviderContext);
