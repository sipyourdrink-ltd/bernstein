import { useEffect, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { Eye, EyeOff, Trash2 } from 'lucide-react';
import { useTheme, type Theme } from '@/components/ThemeProvider';
import { clearAuthToken, getAuthToken, setAuthToken } from '@/lib/api';
import { cn } from '@/lib/utils';

const FLEET_STORAGE_KEY = 'bernstein-fleet-mode';

const THEMES: { value: Theme; label: string; hint: string }[] = [
  { value: 'system', label: 'System', hint: 'Follow OS preference' },
  { value: 'light', label: 'Light', hint: 'Force light theme' },
  { value: 'dark', label: 'Dark', hint: 'Force dark theme' },
];

function readToken(): string {
  return getAuthToken() ?? '';
}

function readFleetMode(): boolean {
  if (typeof window === 'undefined') return false;
  return window.localStorage.getItem(FLEET_STORAGE_KEY) === '1';
}

export default function Settings() {
  const { theme, setTheme } = useTheme();
  const queryClient = useQueryClient();
  const [tokenInput, setTokenInput] = useState<string>(() => readToken());
  const [savedToken, setSavedToken] = useState<string>(() => readToken());
  const [reveal, setReveal] = useState<boolean>(false);
  const [fleetMode, setFleetMode] = useState<boolean>(() => readFleetMode());
  const [savedAt, setSavedAt] = useState<number | null>(null);

  // Auto-clear "Saved" flash after 2s.
  useEffect(() => {
    if (savedAt === null) return;
    const t = setTimeout(() => setSavedAt(null), 2000);
    return () => clearTimeout(t);
  }, [savedAt]);

  const handleSaveToken = () => {
    const trimmed = tokenInput.trim();
    if (trimmed) {
      setAuthToken(trimmed);
    } else {
      clearAuthToken();
    }
    setSavedToken(trimmed);
    setSavedAt(Date.now());
    // Retry everything that failed under the previous token.
    void queryClient.invalidateQueries();
  };

  const handleClearToken = () => {
    clearAuthToken();
    setTokenInput('');
    setSavedToken('');
    setSavedAt(Date.now());
  };

  const handleToggleFleet = () => {
    if (typeof window === 'undefined') return;
    const next = !fleetMode;
    window.localStorage.setItem(FLEET_STORAGE_KEY, next ? '1' : '0');
    setFleetMode(next);
    setSavedAt(Date.now());
  };

  const dirty = tokenInput.trim() !== savedToken;

  return (
    <div className="max-w-3xl p-6">
      <header className="mb-6">
        <h1 className="text-2xl font-semibold">Settings</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Per-user preferences stored in localStorage. Server-side settings
          (telemetry, fleet roster) live behind the operator config endpoints
          and are not yet wired into this screen.
        </p>
      </header>

      {/* Theme ------------------------------------------------------------ */}
      <section className="mb-6 rounded-md border border-border bg-card p-5">
        <h2 className="text-base font-medium text-foreground">Theme</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          Pick a colour scheme. <span className="font-mono">System</span>{' '}
          tracks <code className="font-mono text-xs">prefers-color-scheme</code>.
        </p>
        <div className="mt-4 grid grid-cols-3 gap-2">
          {THEMES.map((opt) => {
            const active = theme === opt.value;
            return (
              <button
                key={opt.value}
                type="button"
                onClick={() => setTheme(opt.value)}
                aria-pressed={active}
                className={cn(
                  'rounded-md border px-3 py-2.5 text-left transition-colors',
                  active
                    ? 'border-foreground bg-foreground text-background'
                    : 'border-border bg-secondary text-foreground hover:bg-card',
                )}
              >
                <div className="text-sm font-medium">{opt.label}</div>
                <div
                  className={cn(
                    'mt-0.5 text-[11px]',
                    active ? 'opacity-80' : 'text-muted-foreground',
                  )}
                >
                  {opt.hint}
                </div>
              </button>
            );
          })}
        </div>
      </section>

      {/* Auth token ------------------------------------------------------- */}
      <section className="mb-6 rounded-md border border-border bg-card p-5">
        <h2 className="text-base font-medium text-foreground">Auth token</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          Bearer token for <code className="font-mono text-xs">/api/v1</code>.
          Stored only in this browser's localStorage. Cleared automatically on
          a 401 response.
        </p>
        <div className="mt-4 flex items-center gap-2">
          <div className="relative flex-1">
            <input
              type={reveal ? 'text' : 'password'}
              value={tokenInput}
              onChange={(e) => setTokenInput(e.target.value)}
              placeholder="paste token…"
              spellCheck={false}
              autoComplete="off"
              className="w-full rounded-md border border-border bg-background px-3 py-2 pr-10 font-mono text-[12.5px] text-foreground outline-none focus:border-foreground"
            />
            <button
              type="button"
              onClick={() => setReveal((v) => !v)}
              className="absolute right-1.5 top-1/2 grid size-7 -translate-y-1/2 place-items-center rounded-sm text-muted-foreground hover:bg-secondary hover:text-foreground"
              aria-label={reveal ? 'Hide token' : 'Show token'}
            >
              {reveal ? (
                <EyeOff className="size-3.5" strokeWidth={1.5} />
              ) : (
                <Eye className="size-3.5" strokeWidth={1.5} />
              )}
            </button>
          </div>
          <button
            type="button"
            onClick={handleSaveToken}
            disabled={!dirty}
            className="rounded-md border border-foreground bg-foreground px-3 py-2 text-sm font-medium text-background transition-colors hover:bg-foreground/90 disabled:opacity-50"
          >
            Save
          </button>
          <button
            type="button"
            onClick={handleClearToken}
            disabled={!savedToken && !tokenInput}
            className="grid size-9 place-items-center rounded-md border border-border bg-secondary text-muted-foreground hover:text-destructive disabled:opacity-50"
            aria-label="Clear token"
            title="Clear stored token"
          >
            <Trash2 className="size-3.5" strokeWidth={1.5} />
          </button>
        </div>
        <div className="mt-2 text-[11px] text-muted-foreground">
          {savedToken
            ? `Stored token: ${savedToken.slice(0, 4)}…${savedToken.slice(-4)} (${savedToken.length} chars)`
            : 'No token stored - requests will be sent unauthenticated.'}
        </div>
      </section>

      {/* Fleet mode ------------------------------------------------------- */}
      <section className="mb-6 rounded-md border border-border bg-card p-5">
        <h2 className="text-base font-medium text-foreground">Fleet mode</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          Mirror of the topbar toggle. Switches between a single installation
          view and the multi-project fleet supervisor.
        </p>
        <label className="mt-4 inline-flex cursor-pointer items-center gap-3">
          <input
            type="checkbox"
            checked={fleetMode}
            onChange={handleToggleFleet}
            className="size-4"
          />
          <span className="text-sm text-foreground">
            Fleet mode is{' '}
            <span className="font-medium">
              {fleetMode ? 'on' : 'off'}
            </span>
          </span>
        </label>
        <p className="mt-2 text-[11px] text-muted-foreground">
          Note: AppShell reads this flag once at mount. Reload the page after
          toggling for the topbar control to reflect the new value.
        </p>
      </section>

      {/* Saved flash ------------------------------------------------------ */}
      <div
        aria-live="polite"
        className="pointer-events-none fixed bottom-10 right-6 z-40 transition-opacity"
        style={{ opacity: savedAt ? 1 : 0 }}
      >
        <div className="pointer-events-auto rounded-md border border-border bg-card px-3 py-2 text-sm shadow-md">
          Saved
        </div>
      </div>
    </div>
  );
}
