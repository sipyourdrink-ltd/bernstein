// Bernstein chrome - Variant A "Decision-Grade Quiet Command".
// Source of truth: design_handoff_bernstein_phase1/design-source/chrome.jsx.

import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { Link, useLocation, useNavigate, useSearchParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import {
  Activity,
  ChevronRight,
  Command,
  DollarSign,
  ListChecks,
  Moon,
  Network,
  ScrollText,
  Search,
  Settings as SettingsIcon,
  ShieldCheck,
  Sun,
  Target,
  User,
} from 'lucide-react';
import { cn } from '@/lib/utils';
import { apiGet } from '@/lib/api';
import { formatUSD } from '@/lib/format';
import { useTheme } from './ThemeProvider';
import { CommandPalette } from './CommandPalette';

type GuiMeta = { version: string; commit: string; build_time: string };

const NAV = [
  { to: '/tasks', label: 'Tasks', icon: ListChecks, key: 'tasks' as const },
  { to: '/agents', label: 'Agents', icon: Activity, key: 'agents' as const },
  { to: '/approvals', label: 'Approvals', icon: ShieldCheck, key: 'approvals' as const },
  { to: '/audit', label: 'Audit', icon: ScrollText, key: 'audit' as const },
  { to: '/costs', label: 'Costs', icon: DollarSign, key: 'costs' as const },
  { to: '/missions', label: 'Missions', icon: Target, key: 'missions' as const },
  { to: '/fleet', label: 'Fleet', icon: Network, key: 'fleet' as const },
  { to: '/settings', label: 'Settings', icon: SettingsIcon, key: 'settings' as const },
] as const;

// Routes reachable via topbar / user-menu but not in the sidebar - used to
// label the topbar when the user is on these screens.  Fleet and Settings
// both have sidebar entries now (smoke-test follow-up), but keep the map
// so any future "topbar-only" routes have a consistent home.
const TOPBAR_LABELS: Record<string, string> = {};

const FLEET_STORAGE_KEY = 'bernstein-fleet-mode';
const FLEET_URL_PARAM = 'fleet';

/** Hoisted helper so it can be reused by tests and the deep-link card. */
function readInitialFleetMode(): boolean {
  if (typeof window === 'undefined') return false;
  // URL wins for deep-linking - a teammate sharing `…?fleet=1` should land in
  // fleet mode even if their local storage still says single.
  const params = new URLSearchParams(window.location.search);
  if (params.has(FLEET_URL_PARAM)) {
    return params.get(FLEET_URL_PARAM) === '1';
  }
  return window.localStorage.getItem(FLEET_STORAGE_KEY) === '1';
}

interface FooterStats {
  agentsTotal: number;
  agentsRunning: number;
  todayUsd: number | null;
  budgetUsd: number | null;
  queueDepth: number | null;
  apiHealthy: boolean;
}

// Backends sometimes return a bare list, sometimes an envelope `{items: [...]}`.
function coerceList<T>(raw: unknown): T[] {
  if (Array.isArray(raw)) return raw as T[];
  if (raw && typeof raw === 'object' && Array.isArray((raw as { items?: unknown }).items)) {
    return (raw as { items: T[] }).items;
  }
  return [];
}

function useFooterStats(): FooterStats {
  type AgentLite = { status?: string };
  type CostsLite = { today_usd?: number; budget_usd?: number };
  type TasksLite = { items?: unknown[]; total?: number };

  const agentsQ = useQuery({
    queryKey: ['agents', 'list'],
    queryFn: () => apiGet<unknown>('/agents').catch(() => [] as unknown),
    refetchInterval: 15_000,
  });
  const costsQ = useQuery({
    queryKey: ['costs', 'current'],
    queryFn: () =>
      apiGet<CostsLite>('/costs/current').catch((): CostsLite => ({})),
    refetchInterval: 30_000,
  });
  const tasksQ = useQuery({
    queryKey: ['tasks', 'pending'],
    queryFn: () =>
      apiGet<TasksLite>('/tasks?status=pending&page_size=1').catch((): TasksLite => ({})),
    refetchInterval: 30_000,
  });
  // Health: only count as healthy when we get a JSON object back. A 200 OK
  // from a misconfigured proxy that returns text/html would otherwise resolve
  // to `undefined` from apiGet and be reported as healthy.
  const healthQ = useQuery({
    queryKey: ['health', 'deps'],
    queryFn: () =>
      apiGet<unknown>('/health/deps')
        .then((data) => data != null && typeof data === 'object')
        .catch(() => false),
    refetchInterval: 60_000,
  });

  const agents = coerceList<AgentLite>(agentsQ.data);
  return {
    agentsTotal: agents.length,
    agentsRunning: agents.filter((a) => a.status === 'running').length,
    todayUsd: costsQ.data?.today_usd ?? null,
    budgetUsd: costsQ.data?.budget_usd ?? null,
    queueDepth: tasksQ.data?.total ?? tasksQ.data?.items?.length ?? null,
    apiHealthy: healthQ.data ?? false,
  };
}

function useApprovalsBadge(): number {
  type ApprovalsLite = { pending?: unknown[]; items?: unknown[] };
  // NOTE: do not swallow errors in the queryFn - let React Query mark the
  // query as errored so its retry/backoff machinery can recover. Otherwise a
  // single failed first fetch would leave the badge stuck at 0 forever even
  // when the API recovers (because returning {} looks like a successful empty
  // response and there's nothing to retry).
  const q = useQuery({
    queryKey: ['approvals', 'queue'],
    queryFn: () => apiGet<ApprovalsLite>('/approvals/queue'),
    refetchInterval: 10_000,
    retry: 3,
  });
  return q.data?.pending?.length ?? q.data?.items?.length ?? 0;
}

function useGuiMetaLabel(): string {
  // Was a one-shot `fetch` with no retry + no auth header - a single 401/5xx
  // would lock the build chip on "connecting…" forever. React Query gives us
  // exponential backoff for free, and apiGet attaches the bearer token.
  const q = useQuery({
    queryKey: ['gui-meta'],
    queryFn: () => apiGet<GuiMeta>('/gui-meta'),
    retry: 3,
    retryDelay: (attempt) => Math.min(1000 * 2 ** attempt, 30_000),
    staleTime: 5 * 60_000,
  });
  if (q.data) {
    const commit = q.data.commit ? ` · ${q.data.commit.slice(0, 7)}` : '';
    return `v${q.data.version}${commit}`;
  }
  // While the very first fetch is still in flight show "connecting…"; once
  // every retry is exhausted, fall back to "version unknown" so the chip
  // doesn't pretend to still be working.
  if (q.isFetching && !q.isFetched) return 'connecting…';
  if (q.isError) return 'version unknown';
  return 'connecting…';
}

export default function AppShell({ children }: { children: ReactNode }) {
  const { resolvedTheme, setTheme } = useTheme();
  const location = useLocation();
  const navigate = useNavigate();
  const metaText = useGuiMetaLabel();
  const [searchParams, setSearchParams] = useSearchParams();
  const [fleetMode, setFleetMode] = useState<boolean>(readInitialFleetMode);
  const [menuOpen, setMenuOpen] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);
  const approvalsCount = useApprovalsBadge();
  const stats = useFooterStats();

  useEffect(() => {
    if (!menuOpen) return;
    const onClick = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setMenuOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setMenuOpen(false);
    };
    window.addEventListener('mousedown', onClick);
    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('mousedown', onClick);
      window.removeEventListener('keydown', onKey);
    };
  }, [menuOpen]);

  // ⌘K opens the command palette.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.key === 'k' || e.key === 'K') && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        setPaletteOpen((v) => !v);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  // Close the palette on any route change so it doesn't stay overlaying the
  // new screen if navigation happened from outside the palette callback.
  useEffect(() => {
    setPaletteOpen(false);
    setMenuOpen(false);
  }, [location.pathname]);

  // Active-route lookup for sidebar / topbar - match either exact or nested.
  const matchNavItem = (path: string) =>
    NAV.find((n) => path === n.to || path.startsWith(`${n.to}/`));
  const current = matchNavItem(location.pathname);
  const breadcrumb = useMemo<[string, string] | null>(() => {
    const segs = location.pathname.split('/').filter(Boolean);
    if (segs.length < 2) return null;
    const root = NAV.find((n) => n.to === `/${segs[0]}`);
    if (!root) return null;
    // Use just the deepest segment so deeply-nested paths don't render an
    // unreadable "verify / abc / xyz" string.
    const last = segs[segs.length - 1] ?? '';
    return [root.label, last];
  }, [location.pathname]);

  // Topbar title: prefer breadcrumb on nested routes, otherwise fall back to
  // the matched NAV label, the well-known TOPBAR_LABELS mapping, or the
  // capitalised first segment so screens like /fleet and /settings aren't
  // rendered with a blank header.
  const topbarLabel = useMemo(() => {
    if (current) return current.label;
    const seg = location.pathname.split('/').filter(Boolean)[0] ?? '';
    if (!seg) return '';
    const known = TOPBAR_LABELS[`/${seg}`];
    if (known) return known;
    return seg.charAt(0).toUpperCase() + seg.slice(1);
  }, [current, location.pathname]);

  // Keep ?fleet=1 in lockstep with browser back/forward navigation. When the
  // URL changes (e.g. the operator clicks a back-link that drops the flag),
  // mirror the new value into local state + storage so the toggle button and
  // future page-loads agree.
  useEffect(() => {
    const param = searchParams.get(FLEET_URL_PARAM);
    if (param == null) return;
    const next = param === '1';
    if (next === fleetMode) return;
    setFleetMode(next);
    window.localStorage.setItem(FLEET_STORAGE_KEY, next ? '1' : '0');
  }, [searchParams, fleetMode]);

  const toggleFleet = () => {
    const next = !fleetMode;
    setFleetMode(next);
    window.localStorage.setItem(FLEET_STORAGE_KEY, next ? '1' : '0');
    // Mirror the new state into the URL so deep-links + back/forward both
    // round-trip. `replace: true` avoids cluttering the history stack with
    // every toggle click.
    setSearchParams(
      (prev) => {
        const params = new URLSearchParams(prev);
        if (next) params.set(FLEET_URL_PARAM, '1');
        else params.delete(FLEET_URL_PARAM);
        return params;
      },
      { replace: true },
    );
    // Actually re-route: enabling Fleet should jump to the fleet view; turning
    // it off from the fleet screen returns the operator to the default Tasks
    // view. From any other screen we leave the user where they are.
    if (next) {
      if (location.pathname !== '/fleet') navigate(`/fleet?${FLEET_URL_PARAM}=1`);
    } else if (location.pathname === '/fleet') {
      navigate('/tasks');
    }
  };

  return (
    <>
      <div className="min-h-screen flex bg-background text-foreground">
        {/* Sidebar - 220 px, secondary background */}
        <aside className="relative w-[220px] shrink-0 border-r border-border bg-secondary flex flex-col">
          <div className="px-[18px] pt-[18px] pb-[16px] border-b border-border-subtle">
            <Link to="/tasks" className="flex items-center gap-[10px]">
              <span className="size-[26px] grid place-items-center rounded-[6px] bg-primary text-primary-foreground font-mono text-[13px] font-semibold">
                B
              </span>
              <span className="text-[15px] font-semibold tracking-[-0.01em]">Bernstein</span>
            </Link>
            <div className="mt-1.5 font-mono text-[10px] uppercase tracking-[0.14em] text-meta-foreground">
              Conducting Podium
            </div>
          </div>

          <nav className="flex-1 px-2 py-2.5 flex flex-col gap-px">
            {NAV.map((item) => {
              const Icon = item.icon;
              const active = location.pathname === item.to || location.pathname.startsWith(`${item.to}/`);
              const showBadge = item.key === 'approvals' && approvalsCount > 0;
              return (
                <Link
                  key={item.to}
                  to={item.to}
                  className={cn(
                    'flex items-center gap-[10px] px-3 py-[9px] rounded-md text-[13px] -tracking-[0.005em]',
                    active
                      ? 'bg-card text-foreground font-medium'
                      : 'text-muted-foreground hover:text-foreground hover:bg-card/60',
                  )}
                  aria-current={active ? 'page' : undefined}
                >
                  <Icon className="size-3.5 shrink-0" strokeWidth={1.5} />
                  <span className="flex-1">{item.label}</span>
                  {showBadge && (
                    <span className="font-mono tabular-nums text-[10px] min-w-[18px] text-center px-1.5 py-px rounded-full bg-accent text-accent-foreground">
                      {approvalsCount}
                    </span>
                  )}
                </Link>
              );
            })}
          </nav>

          {/* Variant A signature: thin score-line stub */}
          <div className="absolute right-2 top-[92px] bottom-[60px] w-px bg-border-subtle" />

          {/* Build chip with health status dot */}
          <div className="flex items-center gap-1.5 px-3.5 py-2.5 border-t border-border-subtle font-mono text-[10.5px] text-meta-foreground">
            <span
              className={cn(
                'inline-block size-1.5 rounded-full',
                stats.apiHealthy ? 'bg-success' : 'bg-warning',
              )}
              title={stats.apiHealthy ? 'API healthy' : 'API degraded'}
            />
            {metaText}
          </div>
        </aside>

        {/* Main column */}
        <main className="flex-1 min-w-0 flex flex-col">
          {/* Topbar - 52 px */}
          <header className="h-[52px] shrink-0 flex items-center justify-between px-5 bg-background border-b border-border">
            <div className="flex items-center gap-3 min-w-0">
              {breadcrumb ? (
                <div className="flex items-center gap-2 text-[12.5px] text-muted-foreground">
                  <span>{breadcrumb[0]}</span>
                  <ChevronRight className="size-2.5 text-meta-foreground" strokeWidth={1.5} />
                  <span className="text-foreground font-medium">{breadcrumb[1]}</span>
                </div>
              ) : (
                <div className="text-[15px] font-semibold -tracking-[0.005em]">{topbarLabel}</div>
              )}
            </div>

            <div className="flex items-center gap-2">
              {/* Jump-to / ⌘K trigger */}
              <button
                type="button"
                onClick={() => setPaletteOpen(true)}
                className="flex items-center justify-between gap-2 min-w-[220px] px-2.5 py-1.5 rounded-md bg-card border border-border text-[12px] text-muted-foreground hover:text-foreground transition-colors"
              >
                <span className="flex items-center gap-2">
                  <Search className="size-3 text-meta-foreground" strokeWidth={1.5} />
                  <span>Jump to…</span>
                </span>
                <span className="font-mono text-[10px] text-meta-foreground border border-border-subtle rounded px-1.5 py-px">
                  ⌘K
                </span>
              </button>

              {/* Single / Fleet segmented control (no inner border) */}
              <div className="flex border border-border rounded-md overflow-hidden text-[11.5px]">
                <button
                  type="button"
                  onClick={() => fleetMode && toggleFleet()}
                  className={cn(
                    'px-2.5 py-1.5 font-medium transition-colors',
                    !fleetMode
                      ? 'bg-foreground text-background'
                      : 'bg-transparent text-muted-foreground hover:text-foreground',
                  )}
                  aria-pressed={!fleetMode}
                >
                  Single
                </button>
                <button
                  type="button"
                  onClick={() => !fleetMode && toggleFleet()}
                  className={cn(
                    'px-2.5 py-1.5 font-medium flex items-center gap-1.5 transition-colors',
                    fleetMode
                      ? 'bg-foreground text-background'
                      : 'bg-transparent text-muted-foreground hover:text-foreground',
                  )}
                  aria-pressed={fleetMode}
                >
                  <Network className="size-2.5" strokeWidth={1.5} />
                  Fleet
                </button>
              </div>

              {/* User menu */}
              <div className="relative" ref={menuRef}>
                <button
                  type="button"
                  onClick={() => setMenuOpen((v) => !v)}
                  className="size-8 grid place-items-center rounded-md bg-card border border-border text-muted-foreground hover:text-foreground"
                  aria-label="User menu"
                  aria-expanded={menuOpen}
                  aria-haspopup="menu"
                >
                  <User className="size-3.5" strokeWidth={1.5} />
                </button>
                {menuOpen && (
                  <div
                    role="menu"
                    className="absolute right-0 top-10 w-56 bg-popover border border-border rounded-md shadow-md py-1 z-50"
                  >
                    <button
                      type="button"
                      role="menuitem"
                      onClick={() => setTheme(resolvedTheme === 'dark' ? 'light' : 'dark')}
                      className="w-full flex items-center gap-3 px-3 py-2 text-[13px] text-popover-foreground hover:bg-secondary text-left"
                    >
                      {resolvedTheme === 'dark' ? (
                        <Sun className="size-3.5" strokeWidth={1.5} />
                      ) : (
                        <Moon className="size-3.5" strokeWidth={1.5} />
                      )}
                      {resolvedTheme === 'dark' ? 'Light theme' : 'Dark theme'}
                    </button>
                    <Link
                      to="/settings"
                      role="menuitem"
                      onClick={() => setMenuOpen(false)}
                      className="flex items-center gap-3 px-3 py-2 text-[13px] text-popover-foreground hover:bg-secondary"
                    >
                      <SettingsIcon className="size-3.5" strokeWidth={1.5} />
                      Settings
                    </Link>
                    <button
                      type="button"
                      role="menuitem"
                      onClick={() => {
                        setMenuOpen(false);
                        setPaletteOpen(true);
                      }}
                      className="w-full flex items-center gap-3 px-3 py-2 text-[13px] text-popover-foreground hover:bg-secondary text-left"
                    >
                      <Command className="size-3.5" strokeWidth={1.5} />
                      Command palette
                      <span className="ml-auto font-mono text-[10px] text-meta-foreground border border-border-subtle rounded px-1.5 py-px">
                        ⌘K
                      </span>
                    </button>
                    <div className="border-t border-border-subtle my-1" />
                    <div className="px-3 py-2 text-[10.5px] text-meta-foreground font-mono">
                      {metaText}
                    </div>
                  </div>
                )}
              </div>
            </div>
          </header>

          {/* Page body */}
          <div className="flex-1 min-h-0 overflow-auto">{children}</div>

          {/* Footer bar - 26 px */}
          <FooterBar stats={stats} />
        </main>
      </div>

      <CommandPalette
        open={paletteOpen}
        onClose={() => setPaletteOpen(false)}
        onNavigate={(path) => {
          setPaletteOpen(false);
          navigate(path);
        }}
        nav={NAV.map((n) => ({ label: n.label, to: n.to }))}
      />
    </>
  );
}

function FooterBar({ stats }: { stats: FooterStats }) {
  return (
    <footer className="h-[26px] shrink-0 flex items-center justify-between px-4 bg-secondary border-t border-border font-mono text-[10.5px] text-meta-foreground uppercase tracking-[0.08em]">
      <div className="flex items-center gap-3.5">
        <span className="flex items-center gap-1.5">
          <span
            className={cn(
              'inline-block size-1.5 rounded-full',
              stats.apiHealthy ? 'bg-success' : 'bg-warning',
            )}
          />
          API · {stats.apiHealthy ? 'OK' : 'degraded'}
        </span>
        <span>
          <span className="tabular-nums">{stats.agentsTotal}</span>{' '}
          {stats.agentsTotal === 1 ? 'agent' : 'agents'} · {' '}
          <span className="tabular-nums">{stats.agentsRunning}</span> running
        </span>
      </div>
      <div className="flex items-center gap-3.5">
        <span>
          today{' '}
          <span className="text-foreground tabular-nums">
            {stats.todayUsd != null ? formatUSD(stats.todayUsd) : '-'}
          </span>
          {stats.budgetUsd != null && (
            <>
              {' · '}budget <span className="tabular-nums">{formatUSD(stats.budgetUsd)}</span>
            </>
          )}
        </span>
        <span>
          queue · <span className="tabular-nums">{stats.queueDepth ?? '-'}</span> pending
        </span>
      </div>
    </footer>
  );
}
