import {
  Component,
  useEffect,
  type ErrorInfo,
  type ReactNode,
} from 'react';
import {
  BrowserRouter,
  Routes,
  Route,
  Navigate,
  useLocation,
} from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import AppShell from './components/AppShell';
import { ThemeProvider } from './components/ThemeProvider';
import { ApiError } from './lib/api';
import Tasks from './routes/Tasks';
import Agents from './routes/Agents';
import Approvals from './routes/Approvals';
import Audit from './routes/Audit';
import Costs from './routes/Costs';
import Fleet from './routes/Fleet';
import Missions from './routes/Missions';
import Settings from './routes/Settings';

// ── QueryClient ────────────────────────────────────────────────────────────
// - retry: skip 4xx (operator error / unauth - never recovers); only retry on
//   transport / 5xx, and cap at 2 attempts.
// - refetchOnWindowFocus: keep the React Query default of `true` so screens
//   that observe live state (Approvals, Tasks) refresh when the operator
//   tabs back in. Screens that DO NOT want this (e.g. expensive cost
//   summaries) opt out per-query via `refetchOnWindowFocus: false`.
// - gcTime: a real garbage-collect window so stale caches do not pile up
//   between rare-visited screens.
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 5_000,
      gcTime: 5 * 60_000,
      retry: (failureCount, error) =>
        failureCount < 2 &&
        !(error instanceof ApiError && error.status >= 400 && error.status < 500),
    },
  },
});

// ── Per-route document.title + scroll-to-top ───────────────────────────────
// `useLocation` must run inside <BrowserRouter>, so this lives as a child of
// the router. Both behaviours are pure side-effects; the component renders
// nothing.
const ROUTE_TITLES: Record<string, string> = {
  '/tasks': 'Tasks',
  '/agents': 'Agents',
  '/approvals': 'Approvals',
  '/audit': 'Audit',
  '/costs': 'Costs',
  '/missions': 'Missions',
  '/fleet': 'Fleet',
  '/settings': 'Settings',
};

function RouteEffects() {
  const { pathname } = useLocation();

  useEffect(() => {
    // Title - fall back to the bare app name on unknown routes.
    const root = `/${pathname.split('/').filter(Boolean)[0] ?? ''}`;
    const label = ROUTE_TITLES[root];
    document.title = label ? `${label} · Bernstein` : 'Bernstein';
  }, [pathname]);

  useEffect(() => {
    // Scroll the main scroll container (and the window, just in case the
    // shell layout changes) back to the top on navigation. Ignored when
    // navigation includes a hash (anchor jumping).
    if (typeof window === 'undefined') return;
    if (window.location.hash) return;
    window.scrollTo({ top: 0, behavior: 'auto' });
    document
      .querySelectorAll<HTMLElement>('[data-scroll-root]')
      .forEach((el) => {
        el.scrollTop = 0;
      });
  }, [pathname]);

  return null;
}

// ── Error boundary ─────────────────────────────────────────────────────────
// Wraps every route below <AppShell> so a crash in one screen does not
// destroy the chrome. Provides Retry (re-mount the subtree) and Reload (full
// page reload) escape hatches.

interface ErrorBoundaryState {
  error: Error | null;
}

class RouteErrorBoundary extends Component<
  { children: ReactNode },
  ErrorBoundaryState
> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Surface the crash in the dev console; production telemetry would hook
    // here too once wired.
    // eslint-disable-next-line no-console
    console.error('[RouteErrorBoundary]', error, info.componentStack);
  }

  reset = () => this.setState({ error: null });

  render(): ReactNode {
    if (!this.state.error) return this.props.children;
    return <RouteCrashFallback error={this.state.error} onRetry={this.reset} />;
  }
}

function RouteCrashFallback({
  error,
  onRetry,
}: {
  error: Error;
  onRetry: () => void;
}) {
  return (
    <div className="flex h-full min-h-[60vh] items-center justify-center p-8">
      <div
        role="alert"
        className="max-w-lg rounded-md border border-destructive/40 bg-destructive/5 p-6"
      >
        <h2 className="text-h3 text-foreground">Screen crashed</h2>
        <p className="mt-2 text-body text-muted-foreground">
          A runtime error broke this view. The rest of the app is still up -
          retry to remount the screen, or reload the page if it keeps failing.
        </p>
        <pre className="mt-3 max-h-40 overflow-auto rounded-sm border border-border-subtle bg-card p-2 font-mono text-[11px] text-muted-foreground">
          {error.message}
        </pre>
        <div className="mt-4 flex items-center gap-2">
          <button
            type="button"
            onClick={onRetry}
            className="rounded-md border border-primary bg-primary px-3 py-1.5 text-body-md text-primary-foreground hover:bg-primary/90"
          >
            Retry
          </button>
          <button
            type="button"
            onClick={() => window.location.reload()}
            className="rounded-md border border-border bg-card px-3 py-1.5 text-body-md text-foreground hover:bg-secondary"
          >
            Reload page
          </button>
        </div>
      </div>
    </div>
  );
}

// ── 404 ────────────────────────────────────────────────────────────────────

function NotFound() {
  return (
    <div className="flex h-full min-h-[60vh] items-center justify-center p-8">
      <div className="max-w-md rounded-md border border-border bg-card p-6">
        <div className="font-mono text-[10.5px] uppercase tracking-[0.12em] text-meta-foreground">
          404
        </div>
        <h2 className="mt-1 text-h3 text-foreground">Route not found</h2>
        <p className="mt-2 text-body text-muted-foreground">
          The path you opened is not part of the Bernstein operator UI. Pick a
          screen from the sidebar, or jump back to Tasks.
        </p>
        <div className="mt-4">
          <a
            href="/ui/tasks"
            className="rounded-md border border-border bg-secondary px-3 py-1.5 text-body-md text-foreground hover:bg-card"
          >
            Go to Tasks
          </a>
        </div>
      </div>
    </div>
  );
}

export default function App() {
  return (
    <ThemeProvider defaultTheme="system" storageKey="bernstein-theme">
      <QueryClientProvider client={queryClient}>
        {/* basename normalizes both `/ui` and `/ui/` - RR strips the prefix
            internally before matching, so the index route fires exactly once
            in either case. */}
        <BrowserRouter basename="/ui">
          <RouteEffects />
          <AppShell>
            <RouteErrorBoundary>
              <Routes>
                <Route path="/" element={<Navigate to="/tasks" replace />} />
                <Route path="/tasks" element={<Tasks />} />
                <Route path="/agents" element={<Agents />} />
                <Route path="/approvals" element={<Approvals />} />
                <Route path="/audit" element={<Audit />} />
                <Route path="/costs" element={<Costs />} />
                <Route path="/missions" element={<Missions />} />
                {/* Fleet + Settings live in topbar / user-menu but stay deep-linkable. */}
                <Route path="/fleet" element={<Fleet />} />
                <Route path="/settings" element={<Settings />} />
                <Route path="*" element={<NotFound />} />
              </Routes>
            </RouteErrorBoundary>
          </AppShell>
        </BrowserRouter>
      </QueryClientProvider>
    </ThemeProvider>
  );
}
