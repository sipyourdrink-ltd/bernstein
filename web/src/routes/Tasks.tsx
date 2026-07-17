// Tasks screen - Variant A "Decision-Grade Quiet Command".
// Source of truth: design_handoff_bernstein_phase1/design-source/screens/screen-tasks.jsx
// + README §6.01 (Tasks specs) + §8 (states contract).

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type ReactNode,
} from 'react';
import { createPortal } from 'react-dom';
import { useQuery, useQueryClient, useMutation } from '@tanstack/react-query';
import { MoreHorizontal, Play, Command as CommandIcon, Search, X } from 'lucide-react';
import { apiGet, apiPost, ApiError } from '@/lib/api';
import { useEventStream } from '@/lib/sse';
import { TaskLogsPanel } from '@/components/logs';
import { TaskDiffPanel } from '@/components/diff/TaskDiffPanel';
import { TaskGatesPanel } from '@/components/gates/TaskGatesPanel';
import { TaskDepsPanel } from '@/components/deps/TaskDepsPanel';
import { TaskTracePanel } from '@/components/trace/TaskTracePanel';
import { TaskArtifactsPanel } from '@/components/artifacts';
import {
  formatUSD,
  formatDuration,
  formatTokens,
  formatRelative,
  formatCount,
} from '@/lib/format';
import { duration, ease, prefersReducedMotion } from '@/lib/motion';
import {
  EmptyState,
  LoadingState,
  ErrorState,
  StatusDot,
  Pill,
  SectionLabel,
} from '@/lib/states';
import { cn } from '@/lib/utils';

// ── Domain types ────────────────────────────────────────────────────────────
// UI status vocabulary - the visual states the table/drawer render.
type TaskStatus = 'running' | 'queued' | 'stalled' | 'failed' | 'done';

// Backend status vocabulary (see core/tasks/models.py::TaskStatus).
type BackendStatus =
  | 'planned'
  | 'open'
  | 'claimed'
  | 'in_progress'
  | 'done'
  | 'closed'
  | 'failed'
  | 'blocked'
  | 'waiting_for_subtasks'
  | 'cancelled'
  | 'orphaned'
  | 'pending_approval'
  | 'refused';

interface TaskRow {
  id: string;
  title: string;
  /** Server-side raw status string. */
  status: BackendStatus | string;
  role: string;
  /** Backend uses ``assigned_agent``; older shape used ``agent``. Either may be present. */
  assigned_agent?: string | null;
  agent?: string | null;
  /** Duration in milliseconds. May be omitted; we derive from claimed_at when needed. */
  duration_ms?: number | null;
  /** 0–100 progress percent. May be missing; show "-" when so. */
  progress?: number | null;
  /** Total tokens consumed so far. */
  tokens?: number | null;
  /** Working git branch. May live on the row or inside ``metadata``. */
  branch?: string | null;
  /** Cost in USD. May live on the row or inside ``metadata.cost_usd``. */
  cost_usd?: number | null;
  /** Free-form metadata bag from the orchestrator. */
  metadata?: Record<string, unknown> | null;
  updated_at?: string | null;
  /** Unix epoch seconds when the task was claimed. */
  claimed_at?: number | null;
  created_at?: number | null;
}

interface TasksListResponse {
  items: TaskRow[];
  total?: number;
  page?: number;
  page_size?: number;
  counts?: Partial<Record<TaskStatus | BackendStatus | 'all' | 'done_24h', number>>;
}

interface PlanStep {
  status: TaskStatus;
  text: string;
}

interface TaskDetail extends TaskRow {
  tokens_in?: number | null;
  tokens_out?: number | null;
  cost_cap_usd?: number | null;
  diff_added?: number | null;
  diff_removed?: number | null;
  approvals_total?: number | null;
  approvals_done?: number | null;
  approvals_pending?: number | null;
  plan?: PlanStep[];
  /** Server-side ``progress_log`` entries - used as a Plan fallback. */
  progress_log?: Array<{ timestamp?: number; message?: string; percent?: number }>;
}

// Map every backend status onto a UI bucket. Unknown strings fall to 'queued'
// (the safest neutral state).
function toUiStatus(s: string | null | undefined): TaskStatus {
  switch (s) {
    case 'running':
    case 'in_progress':
    case 'claimed':
      return 'running';
    case 'planned':
    case 'open':
    case 'queued':
    case 'waiting_for_subtasks':
    case 'pending_approval':
      return 'queued';
    case 'stalled':
    case 'blocked':
    case 'orphaned':
      return 'stalled';
    case 'failed':
    case 'cancelled':
    // Terminal typed refusal (worker completion contract). Rendered in the
    // attention bucket; server-side counts keep refused separate from failed.
    case 'refused':
      return 'failed';
    case 'done':
    case 'closed':
      return 'done';
    default:
      return 'queued';
  }
}

// Cost may live on the row or inside ``metadata.cost_usd`` depending on
// orchestrator version. Coerce strings/numbers, ignore garbage.
function readCostUsd(row: TaskRow): number | null {
  if (typeof row.cost_usd === 'number' && Number.isFinite(row.cost_usd)) return row.cost_usd;
  const md = row.metadata;
  if (md && typeof md === 'object') {
    const v = (md as Record<string, unknown>).cost_usd;
    if (typeof v === 'number' && Number.isFinite(v)) return v;
    if (typeof v === 'string') {
      const n = Number.parseFloat(v);
      if (Number.isFinite(n)) return n;
    }
  }
  return null;
}

function readBranch(row: TaskRow): string | null {
  if (typeof row.branch === 'string' && row.branch) return row.branch;
  const md = row.metadata;
  if (md && typeof md === 'object') {
    const v = (md as Record<string, unknown>).branch;
    if (typeof v === 'string' && v) return v;
  }
  return null;
}

function readAgent(row: TaskRow): string | null {
  if (typeof row.agent === 'string' && row.agent) return row.agent;
  if (typeof row.assigned_agent === 'string' && row.assigned_agent) return row.assigned_agent;
  return null;
}

function readTokens(row: TaskRow): number | null {
  if (typeof row.tokens === 'number' && Number.isFinite(row.tokens)) return row.tokens;
  const md = row.metadata;
  if (md && typeof md === 'object') {
    const v = (md as Record<string, unknown>).tokens;
    if (typeof v === 'number' && Number.isFinite(v)) return v;
  }
  return null;
}

// Tokens in/out may live as top-level fields on the detail response or
// inside ``metadata`` as ``tokens_in``/``tokens_out`` (orchestrator-version
// dependent). Read either shape.
function readTokensIn(detail: TaskDetail): number | null {
  if (typeof detail.tokens_in === 'number' && Number.isFinite(detail.tokens_in)) return detail.tokens_in;
  const md = detail.metadata;
  if (md && typeof md === 'object') {
    const v = (md as Record<string, unknown>).tokens_in;
    if (typeof v === 'number' && Number.isFinite(v)) return v;
    const vp = (md as Record<string, unknown>).tokens_prompt;
    if (typeof vp === 'number' && Number.isFinite(vp)) return vp;
  }
  return null;
}

function readTokensOut(detail: TaskDetail): number | null {
  if (typeof detail.tokens_out === 'number' && Number.isFinite(detail.tokens_out)) return detail.tokens_out;
  const md = detail.metadata;
  if (md && typeof md === 'object') {
    const v = (md as Record<string, unknown>).tokens_out;
    if (typeof v === 'number' && Number.isFinite(v)) return v;
    const vc = (md as Record<string, unknown>).tokens_completion;
    if (typeof vc === 'number' && Number.isFinite(vc)) return vc;
  }
  return null;
}

// Approval counters live in ``metadata`` (no top-level Task field exists yet).
function readApprovalsTriple(detail: TaskDetail): {
  total: number | null;
  done: number | null;
  pending: number | null;
} {
  const md = (detail.metadata ?? null) as Record<string, unknown> | null;
  const readNum = (key: string, top: number | null | undefined): number | null => {
    if (typeof top === 'number' && Number.isFinite(top)) return top;
    if (md && typeof md === 'object') {
      const v = md[key];
      if (typeof v === 'number' && Number.isFinite(v)) return v;
    }
    return null;
  };
  return {
    total: readNum('approvals_total', detail.approvals_total),
    done: readNum('approvals_done', detail.approvals_done),
    pending: readNum('approvals_pending', detail.approvals_pending),
  };
}

// Derive the working branch when the server didn't pin it on the row.
// Backend convention (see ``core/git/`` worktree branch naming): one branch per
// agent session, formatted ``agent/{role}-{session-prefix}``.
function deriveBranch(row: TaskRow): string | null {
  const direct = readBranch(row);
  if (direct) return direct;
  const agent = readAgent(row);
  if (!agent) return null;
  const prefix = agent.slice(0, 8);
  const role = (row.role || 'agent').toLowerCase();
  return `agent/${role}-${prefix}`;
}

// Last-resort plan: walk ``progress_log`` and synthesize one step per
// progress entry. The most recent entry that isn't 100 % is "running"; all
// earlier entries are "done". A 100 % entry collapses everything to "done".
function planFromProgress(detail: TaskDetail): PlanStep[] {
  const log = detail.progress_log;
  if (!Array.isArray(log) || log.length === 0) return [];
  // Sort by timestamp ascending so the rendered order matches execution order.
  const sorted = [...log].sort((a, b) => (a.timestamp ?? 0) - (b.timestamp ?? 0));
  const last = sorted[sorted.length - 1];
  const finished = typeof last?.percent === 'number' && last.percent >= 100;
  return sorted.map((entry, idx) => {
    const isLast = idx === sorted.length - 1;
    const text = String(entry.message ?? '').trim() || `step ${idx + 1}`;
    const status: TaskStatus = finished
      ? 'done'
      : isLast
        ? 'running'
        : 'done';
    return { text, status };
  });
}

function readDurationMs(row: TaskRow): number | null {
  if (typeof row.duration_ms === 'number' && Number.isFinite(row.duration_ms)) return row.duration_ms;
  const claimed = row.claimed_at;
  if (typeof claimed === 'number' && Number.isFinite(claimed)) {
    const nowMs = Date.now();
    const claimedMs = claimed * 1000;
    const delta = nowMs - claimedMs;
    return delta >= 0 ? delta : null;
  }
  return null;
}

// ── Filter chips ────────────────────────────────────────────────────────────

type ChipKey = 'all' | 'running' | 'queued' | 'stalled' | 'done_24h' | 'failed';

// statusParam goes straight to the backend ``?status=`` filter. The closest
// backend status to "running" is ``in_progress`` (Tasks the orchestrator has
// actively claimed); ``open`` is the queue.
//
// NB: the original ``Done · 24h`` label promised a 24h time-window filter
// that the server doesn't expose. Dropping the suffix to keep the chip
// honest until the time-window endpoint lands.
const CHIPS: { key: ChipKey; label: string; statusParam?: string }[] = [
  { key: 'all', label: 'All' },
  { key: 'running', label: 'Running', statusParam: 'in_progress' },
  { key: 'queued', label: 'Queued', statusParam: 'open' },
  { key: 'stalled', label: 'Stalled', statusParam: 'blocked' },
  { key: 'done_24h', label: 'Done', statusParam: 'done' },
  { key: 'failed', label: 'Failed', statusParam: 'failed' },
];

// ── Detail tabs ─────────────────────────────────────────────────────────────

const DETAIL_TABS = ['Summary', 'Diff', 'Gates', 'Artifacts', 'Logs', 'Deps', 'Trace'] as const;
type DetailTab = (typeof DETAIL_TABS)[number];

// ── Operator-syntax token highlighter ───────────────────────────────────────
// Highlights `agent:`, `status:`, `role:` keys in the `accent` colour.
// Pure presentation - does NOT mutate the input.

const TOKEN_RE = /(agent:|status:|role:)/gi;

function HighlightedQuery({ value }: { value: string }) {
  if (!value) {
    // Subtle ghost hint of the syntax - purely visual, the input itself is empty.
    // Parent already renders the literal ``filter:`` label, so do not duplicate it here.
    return (
      <span className="text-meta-foreground/60">
        agent:claude status:running role:backend
      </span>
    );
  }
  const parts: ReactNode[] = [];
  let last = 0;
  let m: RegExpExecArray | null;
  TOKEN_RE.lastIndex = 0;
  while ((m = TOKEN_RE.exec(value)) !== null) {
    if (m.index > last) {
      parts.push(
        <span key={`t-${last}`} className="text-foreground">
          {value.slice(last, m.index)}
        </span>,
      );
    }
    parts.push(
      <span key={`k-${m.index}`} className="text-accent">
        {m[0]}
      </span>,
    );
    last = m.index + m[0].length;
  }
  if (last < value.length) {
    parts.push(
      <span key={`t-${last}`} className="text-foreground">
        {value.slice(last)}
      </span>,
    );
  }
  return <>{parts}</>;
}

// ── Search-string parser → filter params ────────────────────────────────────
// Pulls out `agent:` and `role:` tokens; the rest is a free-text contains-match.

interface ParsedQuery {
  agent: string | null;
  role: string | null;
  text: string;
}

function parseQuery(q: string): ParsedQuery {
  let agent: string | null = null;
  let role: string | null = null;
  const free: string[] = [];
  for (const tok of q.split(/\s+/).filter(Boolean)) {
    const lower = tok.toLowerCase();
    if (lower.startsWith('agent:')) {
      // Trailing-colon (`agent:`) is a partial token while typing - keep the
      // existing free-text behaviour off, but do not assign agent='' either.
      const rest = tok.slice('agent:'.length);
      if (rest) agent = rest;
    } else if (lower.startsWith('role:')) {
      const rest = tok.slice('role:'.length);
      if (rest) role = rest;
    } else if (lower.startsWith('status:')) {
      // status is driven by chip selection, not the query bar
      continue;
    } else {
      free.push(tok);
    }
  }
  return { agent, role, text: free.join(' ').trim() };
}

// ── Endpoint helpers ────────────────────────────────────────────────────────

function buildListPath(opts: {
  status?: string;
  agent?: string | null;
  role?: string | null;
  text?: string;
  page: number;
}): string {
  const p = new URLSearchParams();
  if (opts.status) p.set('status', opts.status);
  if (opts.agent) p.set('agent', opts.agent);
  if (opts.role) p.set('role', opts.role);
  if (opts.text) p.set('q', opts.text);
  p.set('page', String(opts.page));
  return `/tasks?${p.toString()}`;
}

// ── Main component ─────────────────────────────────────────────────────────

export default function Tasks() {
  const qc = useQueryClient();

  const [queryStr, setQueryStr] = useState<string>('');
  const [activeChip, setActiveChip] = useState<ChipKey>('all');
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [page] = useState<number>(1);
  const [activeTab, setActiveTab] = useState<DetailTab>('Summary');

  const parsed = useMemo(() => parseQuery(queryStr), [queryStr]);
  const chip = CHIPS.find((c) => c.key === activeChip) ?? CHIPS[0];

  const listPath = buildListPath({
    status: chip.statusParam,
    agent: parsed.agent,
    role: parsed.role,
    text: parsed.text,
    page,
  });

  const listQ = useQuery({
    queryKey: ['tasks', 'list', listPath],
    queryFn: async (): Promise<TasksListResponse> => {
      // Backend returns either a wrapped {items, total, ...} OR a raw list
      // (legacy /tasks endpoint shape). Normalize here so the rest of the
      // component can assume the wrapped shape.
      const raw = await apiGet<TasksListResponse | TaskRow[]>(listPath);
      if (Array.isArray(raw)) {
        return { items: raw, total: raw.length, page: 1, page_size: raw.length };
      }
      return raw;
    },
  });

  const detailQ = useQuery({
    queryKey: ['tasks', 'detail', selectedId],
    queryFn: () => apiGet<TaskDetail>(`/tasks/${encodeURIComponent(selectedId ?? '')}`),
    enabled: !!selectedId,
  });

  // Live updates → invalidate the list (and detail if relevant).
  useEventStream('/api/v1/events', {
    on: {
      task_update: (raw) => {
        qc.invalidateQueries({ queryKey: ['tasks'] });
        const id = (raw as { id?: string } | null)?.id;
        if (id && id === selectedId) {
          qc.invalidateQueries({ queryKey: ['tasks', 'detail', id] });
        }
      },
      task_progress: (raw) => {
        qc.invalidateQueries({ queryKey: ['tasks', 'list', listPath] });
        const id = (raw as { id?: string } | null)?.id;
        if (id && id === selectedId) {
          qc.invalidateQueries({ queryKey: ['tasks', 'detail', id] });
        }
      },
    },
  });

  // Mutations (per-task).
  // NB: /cancel requires a JSON body (TaskCancelRequest{reason}); the legacy
  // empty-POST returned 422. Keep ``reason`` short and honest.
  // NB: there is no `/tasks/{id}/retry` or `/tasks/{id}/kill` endpoint -
  // ``force-claim`` is the closest "re-run" semantic (resets the row back to
  // open with priority 0); kill maps to ``/tasks/{id}/cancel`` until a
  // session-level kill lands.
  const cancelMut = useMutation({
    mutationFn: (id: string) =>
      apiPost<unknown>(`/tasks/${encodeURIComponent(id)}/cancel`, { reason: 'cancelled from gui' }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['tasks'] }),
  });
  const rerunMut = useMutation({
    mutationFn: (id: string) =>
      apiPost<unknown>(`/tasks/${encodeURIComponent(id)}/force-claim`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['tasks'] }),
  });
  const prioritizeMut = useMutation({
    mutationFn: (id: string) =>
      apiPost<unknown>(`/tasks/${encodeURIComponent(id)}/prioritize`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['tasks'] }),
  });
  const killMut = useMutation({
    mutationFn: (id: string) =>
      apiPost<unknown>(`/tasks/${encodeURIComponent(id)}/cancel`, { reason: 'killed from gui' }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['tasks'] }),
  });

  // Derived
  const items = listQ.data?.items ?? [];
  const counts = listQ.data?.counts ?? {};
  // Counts may be reported under either UI keys (running/stalled) or backend
  // keys (in_progress/blocked). Read both, prefer the explicit one.
  const totalCount = counts.all ?? listQ.data?.total ?? items.length;
  const runningCount =
    counts.running ??
    counts.in_progress ??
    items.filter((t) => toUiStatus(t.status) === 'running').length;
  const stalledCount =
    counts.stalled ??
    counts.blocked ??
    items.filter((t) => toUiStatus(t.status) === 'stalled').length;
  const lastSync = listQ.dataUpdatedAt ? new Date(listQ.dataUpdatedAt).toISOString() : null;

  const selected = items.find((t) => t.id === selectedId) ?? null;

  // Selection lifecycle: clear selection if the previously-selected row no
  // longer exists (e.g. SSE invalidation removed it). Otherwise the detail
  // query keeps thrashing on a 404 and the drawer renders stale fallback data.
  // The drawer only opens on explicit user action - never auto-select.
  useEffect(() => {
    if (!listQ.data) return;
    if (selectedId !== null && !items.some((t) => t.id === selectedId)) {
      setSelectedId(null);
    }
  }, [selectedId, items, listQ.data]);

  const refetchList = () => {
    listQ.refetch();
  };

  const closeDrawer = useCallback(() => setSelectedId(null), []);

  return (
    <div className="grid h-full min-h-0 grid-cols-1">
      {/* ── LEFT: query + table ─────────────────────────────────────────── */}
      <section className="flex min-w-0 flex-col overflow-hidden px-[22px] py-[18px]">
        <Header
          totalCount={totalCount}
          runningCount={runningCount}
          stalledCount={stalledCount}
          lastSync={lastSync}
          loading={listQ.isLoading}
        />

        <SearchBar value={queryStr} onChange={setQueryStr} />

        <ChipsRow active={activeChip} counts={counts} onSelect={setActiveChip} />

        <div className="mt-[14px] min-h-0 flex-1 overflow-hidden rounded-md border border-border bg-card">
          {listQ.isLoading && !listQ.data ? (
            <div className="p-4">
              <LoadingState rows={8} />
            </div>
          ) : listQ.isError ? (
            <div className="p-4">
              <ErrorState
                message={
                  listQ.error instanceof ApiError
                    ? listQ.error.message
                    : 'Could not load tasks.'
                }
                retry={refetchList}
              />
            </div>
          ) : items.length === 0 ? (
            <div className="p-4">
              <EmptyState
                title="No tasks yet"
                description="Spin up the first run to populate this list."
                action={{
                  label: 'Run new task',
                  onClick: () => {
                    /* CTA handled by parent shell command palette */
                  },
                }}
              />
            </div>
          ) : (
            <TasksTable
              items={items}
              selectedId={selectedId}
              onSelect={(id) => setSelectedId((cur) => (cur === id ? null : id))}
            />
          )}
        </div>
      </section>

      {/* ── Detail drawer (overlay) ─────────────────────────────────────── */}
      <DrawerShell open={selectedId !== null} onClose={closeDrawer}>
        {selectedId === null ? null : detailQ.isLoading && !detailQ.data ? (
          <DrawerLoading id={selectedId} fallback={selected} onClose={closeDrawer} />
        ) : detailQ.isError && !detailQ.data ? (
          <DrawerError
            id={selectedId}
            message={
              detailQ.error instanceof ApiError
                ? detailQ.error.message
                : 'Could not load task detail.'
            }
            retry={() => detailQ.refetch()}
            onClose={closeDrawer}
          />
        ) : (
          <DetailDrawer
            task={(detailQ.data ?? selected) as TaskDetail | TaskRow}
            activeTab={activeTab}
            onTabChange={setActiveTab}
            onClose={closeDrawer}
            onCancel={() => selectedId && cancelMut.mutate(selectedId)}
            onRerun={() => selectedId && rerunMut.mutate(selectedId)}
            onPrioritize={() => selectedId && prioritizeMut.mutate(selectedId)}
            onKill={() => selectedId && killMut.mutate(selectedId)}
            isCancelling={cancelMut.isPending}
            isRerunning={rerunMut.isPending}
            isPrioritizing={prioritizeMut.isPending}
            isKilling={killMut.isPending}
          />
        )}
      </DrawerShell>
    </div>
  );
}

// ── Drawer shell ───────────────────────────────────────────────────────────
// Overlay container that owns the cross-cutting drawer behaviours:
//   • mounts via portal so click-outside (backdrop) really intercepts the
//     task list underneath
//   • ESC + backdrop click → close
//   • focus trap while open; restore focus to the trigger element on close
//   • left-edge drag handle to resize; width persists in localStorage
//   • respects prefers-reduced-motion (the slide-in animation collapses)
//   • role="dialog" + aria-modal + aria-labelledby for assistive tech

const DRAWER_WIDTH_KEY = 'bernstein.tasks.drawerWidth.v1';
const DRAWER_MIN_W = 360;
const DRAWER_MAX_W = 880;
const DRAWER_DEFAULT_W = 460;

function loadDrawerWidth(): number {
  if (typeof window === 'undefined') return DRAWER_DEFAULT_W;
  try {
    const raw = window.localStorage.getItem(DRAWER_WIDTH_KEY);
    if (!raw) return DRAWER_DEFAULT_W;
    const n = Number.parseFloat(raw);
    if (!Number.isFinite(n)) return DRAWER_DEFAULT_W;
    return Math.max(DRAWER_MIN_W, Math.min(DRAWER_MAX_W, n));
  } catch {
    return DRAWER_DEFAULT_W;
  }
}

function saveDrawerWidth(w: number): void {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.setItem(DRAWER_WIDTH_KEY, String(Math.round(w)));
  } catch {
    /* private mode etc. - ignore */
  }
}

function DrawerShell({
  open,
  onClose,
  children,
}: {
  open: boolean;
  onClose: () => void;
  children: ReactNode;
}) {
  const panelRef = useRef<HTMLDivElement | null>(null);
  const triggerRef = useRef<Element | null>(null);
  const [width, setWidth] = useState<number>(() => loadDrawerWidth());
  const dragStateRef = useRef<{ startX: number; startW: number } | null>(null);

  // Capture the element that had focus when the drawer opens so we can
  // restore focus to it on close (modal-best-practice).
  useLayoutEffect(() => {
    if (!open) return;
    triggerRef.current = document.activeElement;
    // Move focus into the panel - the first focusable element wins.
    const node = panelRef.current;
    if (!node) return;
    const firstFocusable = node.querySelector<HTMLElement>(
      'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    );
    if (firstFocusable) {
      firstFocusable.focus();
    } else {
      node.focus();
    }
  }, [open]);

  // ESC closes; Tab is trapped within the panel.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        e.stopPropagation();
        onClose();
        return;
      }
      if (e.key !== 'Tab') return;
      const node = panelRef.current;
      if (!node) return;
      const focusables = Array.from(
        node.querySelectorAll<HTMLElement>(
          'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ),
      ).filter((el) => !el.hasAttribute('aria-hidden') && el.offsetParent !== null);
      if (focusables.length === 0) {
        e.preventDefault();
        node.focus();
        return;
      }
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      const active = document.activeElement as HTMLElement | null;
      if (e.shiftKey) {
        if (active === first || !node.contains(active)) {
          e.preventDefault();
          last.focus();
        }
      } else if (active === last) {
        e.preventDefault();
        first.focus();
      }
    };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [open, onClose]);

  // Restore focus to the trigger on close (best-effort).
  useEffect(() => {
    if (open) return;
    const trigger = triggerRef.current as HTMLElement | null;
    triggerRef.current = null;
    if (trigger && typeof trigger.focus === 'function') {
      trigger.focus();
    }
  }, [open]);

  // Drag-to-resize handle. Width is the distance from the right edge of the
  // viewport to the drawer's left edge, so dragging the handle leftward
  // grows the drawer.
  const onResizePointerDown = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      e.preventDefault();
      const startX = e.clientX;
      dragStateRef.current = { startX, startW: width };
      const onMove = (ev: PointerEvent) => {
        const drag = dragStateRef.current;
        if (!drag) return;
        const next = drag.startW + (drag.startX - ev.clientX);
        const clamped = Math.max(DRAWER_MIN_W, Math.min(DRAWER_MAX_W, next));
        setWidth(clamped);
      };
      const onUp = () => {
        dragStateRef.current = null;
        window.removeEventListener('pointermove', onMove);
        window.removeEventListener('pointerup', onUp);
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
        setWidth((w) => {
          saveDrawerWidth(w);
          return w;
        });
      };
      window.addEventListener('pointermove', onMove);
      window.addEventListener('pointerup', onUp);
      document.body.style.cursor = 'col-resize';
      document.body.style.userSelect = 'none';
    },
    [width],
  );

  // Keyboard resize (operators who can't drag): ←/→ on the handle adjusts
  // width in 24 px increments; Home/End jump to extremes.
  const onResizeKeyDown = useCallback((e: React.KeyboardEvent<HTMLDivElement>) => {
    const STEP = 24;
    let delta = 0;
    if (e.key === 'ArrowLeft') delta = STEP;
    else if (e.key === 'ArrowRight') delta = -STEP;
    else if (e.key === 'Home') {
      e.preventDefault();
      setWidth(DRAWER_MAX_W);
      saveDrawerWidth(DRAWER_MAX_W);
      return;
    } else if (e.key === 'End') {
      e.preventDefault();
      setWidth(DRAWER_MIN_W);
      saveDrawerWidth(DRAWER_MIN_W);
      return;
    } else {
      return;
    }
    e.preventDefault();
    setWidth((w) => {
      const next = Math.max(DRAWER_MIN_W, Math.min(DRAWER_MAX_W, w + delta));
      saveDrawerWidth(next);
      return next;
    });
  }, []);

  if (!open) return null;
  if (typeof document === 'undefined') return null;

  const reduceMotion = prefersReducedMotion();
  const animation = reduceMotion
    ? undefined
    : `drawer-in ${duration.panel * 1000}ms cubic-bezier(${ease.out.join(',')})`;

  return createPortal(
    <div
      className="fixed inset-0 z-40"
      role="presentation"
      // Backdrop click anywhere outside the panel closes the drawer.
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      {/* Backdrop: semi-transparent, lets the task list show through but
          provides clear visual separation. Click anywhere on it closes. */}
      <div
        aria-hidden="true"
        className="absolute inset-0 bg-foreground/20 backdrop-blur-[1px] animate-fade-in"
        onMouseDown={(e) => {
          e.stopPropagation();
          onClose();
        }}
      />

      <aside
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="task-drawer-title"
        tabIndex={-1}
        className="absolute right-0 top-0 flex h-full flex-col overflow-hidden border-l border-border bg-secondary shadow-2xl outline-none"
        style={
          {
            width,
            animation,
          } as CSSProperties
        }
        onMouseDown={(e) => e.stopPropagation()}
      >
        {/* Drag handle - sits on the panel's left edge */}
        <div
          role="separator"
          aria-orientation="vertical"
          aria-label="Resize task detail drawer"
          aria-valuemin={DRAWER_MIN_W}
          aria-valuemax={DRAWER_MAX_W}
          aria-valuenow={Math.round(width)}
          tabIndex={0}
          onPointerDown={onResizePointerDown}
          onKeyDown={onResizeKeyDown}
          className="group absolute left-0 top-0 z-10 h-full w-1.5 -translate-x-1/2 cursor-col-resize select-none"
        >
          <div className="absolute left-1/2 top-1/2 h-12 w-0.5 -translate-x-1/2 -translate-y-1/2 rounded-full bg-border transition-colors group-hover:bg-accent group-focus-visible:bg-accent" />
        </div>

        {children}
      </aside>
    </div>,
    document.body,
  );
}

// ── Header ─────────────────────────────────────────────────────────────────

function Header({
  totalCount,
  runningCount,
  stalledCount,
  lastSync,
  loading,
}: {
  totalCount: number;
  runningCount: number;
  stalledCount: number;
  lastSync: string | null;
  loading: boolean;
}) {
  return (
    <div className="mb-[14px] flex items-baseline justify-between gap-4">
      <div className="min-w-0">
        <h1 className="text-h2 text-foreground">Tasks</h1>
        <div className="mt-[3px] text-[12px] text-muted-foreground">
          <span className="font-mono tabular-nums">
            {loading ? '-' : formatCount(totalCount)}
          </span>{' '}
          tasks ·{' '}
          <span className="font-mono tabular-nums">
            {loading ? '-' : formatCount(runningCount)}
          </span>{' '}
          running ·{' '}
          <span className="font-mono tabular-nums">
            {loading ? '-' : formatCount(stalledCount)}
          </span>{' '}
          stalled · last sync{' '}
          <span className="font-mono">
            {lastSync ? formatRelative(lastSync) : 'just now'}
          </span>
        </div>
      </div>
      <div className="flex shrink-0 items-center gap-1.5">
        <button
          type="button"
          className="inline-flex items-center gap-1.5 rounded-md border border-border bg-card px-2.5 py-1.5 text-[12px] font-medium text-foreground transition-colors hover:bg-secondary"
        >
          <Play className="size-3" strokeWidth={1.5} />
          Run new task
        </button>
        <button
          type="button"
          className="inline-flex items-center gap-1.5 rounded-md border border-foreground bg-foreground px-2.5 py-1.5 text-[12px] font-medium text-background transition-colors hover:bg-foreground/90"
        >
          <CommandIcon className="size-3" strokeWidth={1.5} />
          New from prompt
        </button>
      </div>
    </div>
  );
}

// ── Search bar ─────────────────────────────────────────────────────────────

function SearchBar({
  value,
  onChange,
}: {
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <div className="relative flex items-center gap-2 rounded-md border border-border bg-card px-2.5 py-2 font-mono text-[12.5px]">
      <Search className="size-3 shrink-0 text-meta-foreground" strokeWidth={1.5} />
      <span className="shrink-0 text-meta-foreground">filter:</span>
      <div className="relative flex-1">
        {/* Highlight overlay (visual layer) */}
        <div
          aria-hidden
          className="pointer-events-none absolute inset-0 truncate whitespace-pre text-foreground"
        >
          <HighlightedQuery value={value} />
        </div>
        <input
          type="text"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className="relative w-full bg-transparent text-transparent caret-foreground outline-none [&::selection]:bg-accent/30 [&::selection]:text-foreground"
          spellCheck={false}
          aria-label="Operator-syntax filter"
        />
      </div>
      <span className="ml-auto shrink-0 rounded-sm border border-border-subtle px-1.5 py-px font-mono text-[10px] text-meta-foreground">
        ↵
      </span>
    </div>
  );
}

// ── Chips row ──────────────────────────────────────────────────────────────

// Map a ChipKey to the backend ``counts`` keys it would prefer to read, in
// fallback order. The backend may emit either UI-flavoured keys (running) or
// raw backend statuses (in_progress, blocked, …).
const CHIP_COUNT_KEYS: Record<ChipKey, readonly string[]> = {
  all: ['all'],
  running: ['running', 'in_progress'],
  queued: ['queued', 'open'],
  stalled: ['stalled', 'blocked'],
  done_24h: ['done_24h', 'done'],
  failed: ['failed'],
};

function ChipsRow({
  active,
  counts,
  onSelect,
}: {
  active: ChipKey;
  counts: Partial<Record<TaskStatus | BackendStatus | 'all' | 'done_24h', number>>;
  onSelect: (k: ChipKey) => void;
}) {
  return (
    <div className="mt-[10px] flex flex-wrap items-center gap-1.5">
      {CHIPS.map((c) => {
        const isActive = c.key === active;
        const lookups = CHIP_COUNT_KEYS[c.key];
        const n = lookups
          .map((k) => counts[k as keyof typeof counts])
          .find((v): v is number => typeof v === 'number');
        return (
          <button
            type="button"
            key={c.key}
            onClick={() => onSelect(c.key)}
            className={cn(
              'inline-flex items-center gap-1.5 rounded-full border px-2.5 py-[3px] text-[11.5px] transition-colors',
              isActive
                ? 'bg-foreground text-background border-foreground'
                : 'border-border bg-card text-muted-foreground hover:text-foreground',
            )}
            aria-pressed={isActive}
          >
            {c.label}
            <span
              className={cn(
                'font-mono tabular-nums text-[10.5px]',
                isActive ? 'opacity-80' : 'text-meta-foreground',
              )}
            >
              {n != null ? formatCount(n) : '-'}
            </span>
          </button>
        );
      })}
      <span className="flex-1" />
      <SectionLabel className="ml-auto !text-[10.5px]">sort: updated ↓</SectionLabel>
    </div>
  );
}

// ── Tasks table ────────────────────────────────────────────────────────────

function TasksTable({
  items,
  selectedId,
  onSelect,
}: {
  items: TaskRow[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  return (
    <div className="h-full overflow-auto">
      <table className="w-full table-fixed border-collapse">
        <colgroup>
          <col className="w-[16px]" />
          <col className="w-[86px]" />
          <col />
          <col className="w-[140px]" />
          <col className="w-[110px]" />
          <col className="w-[70px]" />
          <col className="w-[110px]" />
          <col className="w-[80px]" />
          <col className="w-[36px]" />
        </colgroup>
        <thead className="bg-muted/60">
          <tr className="text-left">
            <Th />
            <Th>ID</Th>
            <Th>Title</Th>
            <Th>Agent</Th>
            <Th>Role</Th>
            <Th align="right">Dur</Th>
            <Th>Progress</Th>
            <Th align="right">Cost</Th>
            <Th />
          </tr>
        </thead>
        <tbody>
          {items.map((tk) => {
            const sel = tk.id === selectedId;
            const ui = toUiStatus(tk.status);
            const agent = readAgent(tk);
            const branch = readBranch(tk);
            const cost = readCostUsd(tk);
            const dur = readDurationMs(tk);
            const progress = typeof tk.progress === 'number' && Number.isFinite(tk.progress)
              ? tk.progress
              : null;
            return (
              <tr
                key={tk.id}
                className={cn(
                  'group cursor-pointer border-b border-border-subtle transition-colors last:border-b-0',
                  sel
                    ? 'bg-secondary [box-shadow:inset_2px_0_0_hsl(var(--accent))]'
                    : 'hover:bg-muted/40',
                )}
                onClick={() => onSelect(tk.id)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    onSelect(tk.id);
                  }
                }}
                tabIndex={0}
                aria-selected={sel}
              >
                <Td className="pl-[14px]">
                  <StatusDot kind={ui} />
                </Td>
                <Td className="font-mono text-[11.5px] text-muted-foreground">
                  <span className="block truncate" title={tk.id}>{tk.id}</span>
                </Td>
                <Td className="min-w-0">
                  <div
                    className={cn(
                      'truncate text-[12.5px]',
                      sel ? 'font-medium text-foreground' : 'text-foreground',
                    )}
                    title={tk.title}
                  >
                    {tk.title}
                  </div>
                  {branch && (
                    <div className="mt-0.5 truncate font-mono text-[10.5px] text-meta-foreground" title={branch}>
                      ↳ {branch}
                    </div>
                  )}
                </Td>
                <Td className="font-mono text-[11.5px] text-muted-foreground">
                  <span className="block truncate" title={agent ?? undefined}>{agent ?? '-'}</span>
                </Td>
                <Td>
                  <Pill kind="ghost">{tk.role}</Pill>
                </Td>
                <Td
                  align="right"
                  className={cn(
                    'font-mono tabular-nums text-[11.5px]',
                    ui === 'stalled' ? 'text-warning' : 'text-foreground',
                  )}
                >
                  {ui === 'queued' ? '-' : formatDuration(dur)}
                </Td>
                <Td>
                  {ui === 'queued' || progress === null ? (
                    <span className="font-mono text-[11px] text-meta-foreground">-</span>
                  ) : (
                    <ProgressCell status={ui} value={progress} />
                  )}
                </Td>
                <Td align="right" className="font-mono tabular-nums text-[11.5px]">
                  {formatUSD(cost)}
                </Td>
                <Td align="right" className="pr-3 text-meta-foreground">
                  <button
                    type="button"
                    className="grid size-6 place-items-center rounded-sm text-meta-foreground transition-colors hover:bg-muted hover:text-foreground"
                    aria-label="Row actions"
                    onClick={(e) => {
                      e.stopPropagation();
                      // Selecting the row opens the drawer where every per-task
                      // action (cancel/rerun/kill) lives. Keep the kebab affordance
                      // visible but route to the same target until a popover lands.
                      onSelect(tk.id);
                    }}
                  >
                    <MoreHorizontal className="size-3.5" strokeWidth={1.5} />
                  </button>
                </Td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function ProgressCell({ status, value }: { status: TaskStatus; value: number }) {
  const barClass =
    status === 'failed'
      ? 'bg-destructive'
      : status === 'stalled'
        ? 'bg-warning'
        : status === 'done'
          ? 'bg-foreground'
          : 'bg-accent';
  const pct = Math.max(0, Math.min(100, value));
  return (
    <div className="flex items-center gap-2">
      <div className="h-1 flex-1 overflow-hidden rounded-sm bg-border-subtle">
        <div
          className={cn('h-full rounded-sm', barClass)}
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className="w-7 text-right font-mono tabular-nums text-[10.5px] text-meta-foreground">
        {pct}%
      </span>
    </div>
  );
}

// ── Drawer states ──────────────────────────────────────────────────────────

function DrawerLoading({
  id,
  fallback,
  onClose,
}: {
  id: string;
  fallback: TaskRow | null;
  onClose: () => void;
}) {
  return (
    <>
      <div className="border-b border-border px-[18px] pb-[10px] pt-[14px]">
        <div className="flex items-center justify-between gap-2">
          <span className="truncate font-mono text-[11px] tracking-[0.1em] text-meta-foreground">
            TASK · {id}
          </span>
          <button
            type="button"
            onClick={onClose}
            className="shrink-0 text-meta-foreground transition-colors hover:text-foreground"
            aria-label="Close detail"
          >
            <X className="size-3" strokeWidth={1.5} />
          </button>
        </div>
        <div
          id="task-drawer-title"
          className="mt-1.5 text-[14px] font-medium leading-snug text-foreground"
        >
          {fallback?.title ?? '-'}
        </div>
      </div>
      <div className="flex-1 overflow-auto px-[18px] py-[14px]">
        <LoadingState rows={6} />
      </div>
    </>
  );
}

function DrawerError({
  id,
  message,
  retry,
  onClose,
}: {
  id: string;
  message: string;
  retry: () => void;
  onClose: () => void;
}) {
  return (
    <>
      <div className="flex items-center justify-between border-b border-border px-[18px] pb-[10px] pt-[14px]">
        <span
          id="task-drawer-title"
          className="font-mono text-[11px] tracking-[0.1em] text-meta-foreground"
        >
          TASK · {id}
        </span>
        <button
          type="button"
          onClick={onClose}
          className="text-meta-foreground transition-colors hover:text-foreground"
          aria-label="Close detail"
        >
          <X className="size-3" strokeWidth={1.5} />
        </button>
      </div>
      <div className="flex-1 overflow-auto px-[18px] py-[14px]">
        <ErrorState message={message} retry={retry} />
      </div>
    </>
  );
}

// ── Detail drawer (Summary) ─────────────────────────────────────────────────

function DetailDrawer({
  task,
  activeTab,
  onTabChange,
  onClose,
  onCancel,
  onRerun,
  onPrioritize,
  onKill,
  isCancelling,
  isRerunning,
  isPrioritizing,
  isKilling,
}: {
  task: TaskDetail | TaskRow;
  activeTab: DetailTab;
  onTabChange: (t: DetailTab) => void;
  onClose: () => void;
  onCancel: () => void;
  onRerun: () => void;
  onPrioritize: () => void;
  onKill: () => void;
  isCancelling: boolean;
  isRerunning: boolean;
  isPrioritizing: boolean;
  isKilling: boolean;
}) {
  const detail = task as TaskDetail;
  const ui = toUiStatus(task.status);
  const durMs = readDurationMs(task);
  const durLabel = ui === 'queued' ? 'queued' : `${ui} · ${formatDuration(durMs)}`;
  const agent = readAgent(task);

  // KPI: tokens, cost, branch, approvals - all tolerate field-shape variance.
  // Tokens: prefer typed in/out (sum), fall back to the row's metadata.tokens.
  const tokensIn = readTokensIn(detail);
  const tokensOut = readTokensOut(detail);
  const tokensRow = readTokens(task);
  const tokensTotal =
    tokensIn != null && tokensOut != null
      ? tokensIn + tokensOut
      : tokensIn ?? tokensOut ?? tokensRow;
  const costUsd = readCostUsd(task);
  const costCap = detail.cost_cap_usd ?? null;
  // Branch: pin > metadata.branch > derived ``agent/{role}-{session-prefix}``.
  const branch = deriveBranch(task);
  const diffAdd = detail.diff_added;
  const diffDel = detail.diff_removed;
  const approvals = readApprovalsTriple(detail);
  const apTotal = approvals.total;
  const apDone = approvals.done;
  const apPending = approvals.pending;

  // Plan: server-supplied if present, else synthesized from ``progress_log``.
  const plan: PlanStep[] = detail.plan && detail.plan.length > 0 ? detail.plan : planFromProgress(detail);

  return (
    <>
      {/* Header */}
      <div className="border-b border-border px-[18px] pb-[10px] pt-[14px]">
        <div className="flex items-center justify-between gap-2">
          <span className="truncate font-mono text-[11px] tracking-[0.1em] text-meta-foreground">
            TASK · {task.id}
          </span>
          <button
            type="button"
            onClick={onClose}
            className="shrink-0 text-meta-foreground transition-colors hover:text-foreground"
            aria-label="Close detail"
          >
            <X className="size-3" strokeWidth={1.5} />
          </button>
        </div>
        <div
          id="task-drawer-title"
          className="mt-1.5 text-[14px] font-medium leading-snug text-foreground"
        >
          {task.title}
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          <Pill kind={statusToPillKind(ui)}>
            <StatusDot kind={ui} />
            {durLabel}
          </Pill>
          {agent && <Pill>{agent}</Pill>}
          <Pill kind="ghost">{task.role}</Pill>
        </div>
      </div>

      {/* Tabs */}
      <div className="flex gap-0 border-b border-border px-3 text-[12px]">
        {DETAIL_TABS.map((tab) => {
          const isActive = tab === activeTab;
          return (
            <button
              type="button"
              key={tab}
              onClick={() => onTabChange(tab)}
              className={cn(
                'border-b-2 px-2.5 py-2.5 transition-colors',
                isActive
                  ? 'border-accent font-medium text-foreground'
                  : 'border-transparent text-muted-foreground hover:text-foreground',
              )}
              aria-pressed={isActive}
            >
              {tab}
            </button>
          );
        })}
      </div>

      {/* Body */}
      <div className="flex-1 overflow-auto px-[18px] py-[14px] text-[12.5px]">
        {activeTab === 'Summary' && (
          <>
            {/* KPI 2×2 */}
            <div className="grid grid-cols-2 gap-2.5">
              <Kpi
                label="tokens"
                value={formatTokens(tokensTotal)}
                sub={
                  tokensIn != null && tokensOut != null
                    ? `${formatTokens(tokensIn)} in / ${formatTokens(tokensOut)} out`
                    : '-'
                }
              />
              <Kpi
                label="cost"
                value={formatUSD(costUsd)}
                sub={costCap != null ? `of ${formatUSD(costCap)} cap` : '-'}
              />
              <Kpi
                label="branch"
                value={branch ?? '-'}
                sub={
                  diffAdd != null && diffDel != null
                    ? `+${formatCount(diffAdd)} −${formatCount(diffDel)} lines`
                    : 'no diff yet'
                }
                valueMono
              />
              <Kpi
                label="approvals"
                value={
                  apTotal != null && apDone != null
                    ? `${formatCount(apDone)} / ${formatCount(apTotal)}`
                    : '-'
                }
                sub={
                  apPending != null && apPending > 0
                    ? `${formatCount(apPending)} pending`
                    : 'none pending'
                }
              />
            </div>

            {/* Plan */}
            <div className="mt-4">
              <SectionLabel className="mb-2">
                Plan{plan.length > 0 ? ` · ${plan.length} steps` : ''}
              </SectionLabel>
              {plan.length === 0 ? (
                <div className="rounded-sm border border-border-subtle bg-card/60 px-3 py-2 text-[12px] text-muted-foreground">
                  No plan steps reported.
                </div>
              ) : (
                <ol className="m-0 flex list-none flex-col gap-2 p-0">
                  {plan.map((step, i) => (
                    <li key={i} className="flex items-start gap-2.5 text-[12.5px]">
                      <span className="w-4 shrink-0 font-mono text-[11px] text-meta-foreground">
                        {i + 1}.
                      </span>
                      <span className="mt-1.5 shrink-0">
                        <StatusDot kind={step.status} />
                      </span>
                      <span
                        className={cn(
                          'flex-1',
                          step.status === 'queued'
                            ? 'text-muted-foreground'
                            : 'text-foreground',
                          step.status === 'done' && 'line-through',
                        )}
                      >
                        {step.text}
                      </span>
                    </li>
                  ))}
                </ol>
              )}
            </div>

            {/* Action stack. ``Change model`` / ``Change role`` are not yet wired
                to a backend mutation; do not silently fall through to the
                prioritize endpoint (that's a different action with side-effects). */}
            <div className="mt-5 grid grid-cols-2 gap-1.5">
              <ActionButton onClick={onCancel} pending={isCancelling}>
                Cancel run
              </ActionButton>
              <ActionButton onClick={onRerun} pending={isRerunning}>
                Re-run
              </ActionButton>
              <ActionButton onClick={onPrioritize} pending={isPrioritizing}>
                Prioritize
              </ActionButton>
              <ActionButton onClick={() => undefined} pending={false} disabledReason="Not wired in this build">
                Change model
              </ActionButton>
              <ActionButton
                className="col-span-2 border-destructive text-destructive hover:bg-destructive/10"
                onClick={onKill}
                pending={isKilling}
              >
                Kill session
              </ActionButton>
            </div>
          </>
        )}

        {activeTab !== 'Summary' && (
          <TaskTabContent task={task} activeTab={activeTab} />
        )}
      </div>
    </>
  );
}

function statusToPillKind(s: TaskStatus): 'success' | 'warning' | 'danger' | 'default' | 'ghost' {
  switch (s) {
    case 'running':
      return 'success';
    case 'stalled':
      return 'warning';
    case 'failed':
      return 'danger';
    case 'queued':
      return 'ghost';
    case 'done':
      return 'default';
  }
}

// ── Detail bits ────────────────────────────────────────────────────────────

function Kpi({
  label,
  value,
  sub,
  valueMono = false,
}: {
  label: string;
  value: string;
  sub: string;
  valueMono?: boolean;
}) {
  return (
    <div className="rounded-md border border-border-subtle bg-card p-[11px]">
      <div className="font-mono text-[10px] uppercase tracking-[0.1em] text-meta-foreground">
        {label}
      </div>
      <div
        className={cn(
          'mt-0.5 text-stat-md text-foreground tabular-nums',
          valueMono ? 'font-mono text-[14px]' : 'font-mono',
        )}
      >
        {value}
      </div>
      <div className="mt-0.5 text-[11px] text-meta-foreground">{sub}</div>
    </div>
  );
}

function ActionButton({
  children,
  onClick,
  pending,
  className,
  disabledReason,
}: {
  children: ReactNode;
  onClick: () => void;
  pending: boolean;
  className?: string;
  disabledReason?: string;
}) {
  const disabled = pending || disabledReason != null;
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={disabledReason}
      aria-disabled={disabled}
      className={cn(
        'rounded-md border border-border bg-card px-2.5 py-1.5 text-[12px] font-medium text-foreground transition-colors hover:bg-secondary disabled:cursor-not-allowed disabled:opacity-60',
        className,
      )}
    >
      {children}
    </button>
  );
}

// Tab content router. Each tab owns its own panel module under
// `web/src/components/{kind}/`. To wire a new tab, add a case here and
// implement the corresponding panel - no other changes to Tasks.tsx needed.
function TaskTabContent({
  task,
  activeTab,
}: {
  task: TaskDetail | TaskRow;
  activeTab: DetailTab;
}) {
  const taskId = String(task.id);
  switch (activeTab) {
    case 'Logs':
      return <TaskLogsPanel taskId={taskId} active />;
    case 'Diff':
      return <TaskDiffPanel taskId={taskId} active />;
    case 'Gates':
      return <TaskGatesPanel taskId={taskId} active />;
    case 'Artifacts':
      return <TaskArtifactsPanel taskId={taskId} active />;
    case 'Deps':
      return <TaskDepsPanel taskId={taskId} active />;
    case 'Trace':
      return <TaskTracePanel taskId={taskId} active />;
    case 'Summary':
      return null;
    default:
      return <TabPlaceholder tab={activeTab} />;
  }
}

function TabPlaceholder({ tab }: { tab: DetailTab }) {
  return (
    <div className="rounded-md border border-border-subtle bg-card/60 px-4 py-6 text-center text-[12.5px] text-muted-foreground">
      <div className="font-mono text-[10px] uppercase tracking-[0.12em] text-meta-foreground">
        {tab}
      </div>
      <div className="mt-1.5">
        Live {tab.toLowerCase()} feed for this task is not wired in this build.
      </div>
    </div>
  );
}

// ── Table cells ────────────────────────────────────────────────────────────

function Th({
  children,
  align = 'left',
}: {
  children?: ReactNode;
  align?: 'left' | 'right';
}) {
  return (
    <th
      className={cn(
        'border-b border-border-subtle px-2 py-2 font-mono text-[10px] font-normal uppercase tracking-[0.12em] text-meta-foreground',
        align === 'right' ? 'text-right' : 'text-left',
      )}
    >
      {children}
    </th>
  );
}

function Td({
  children,
  align = 'left',
  className,
}: {
  children?: ReactNode;
  align?: 'left' | 'right';
  className?: string;
}) {
  return (
    <td
      className={cn(
        'px-2 py-2.5 align-top text-[12.5px]',
        align === 'right' ? 'text-right' : 'text-left',
        className,
      )}
    >
      {children}
    </td>
  );
}
