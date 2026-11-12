// Approvals screen - pending tool-call queue + Why? + Diff + Action bar.
// Source of truth: design_handoff_bernstein_phase1/design-source/screens/screen-approvals.jsx
// + README §6.03 (Approvals specs) and §8 (states contract).

import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ShieldCheck, Check, X } from 'lucide-react';
import { ApiError, apiGet, apiPost } from '@/lib/api';
import { useEventStream } from '@/lib/sse';
import { formatDuration } from '@/lib/format';
import {
  EmptyState,
  ErrorState,
  LoadingState,
  Pill,
  SectionLabel,
  StatusDot,
} from '@/lib/states';
import { cn } from '@/lib/utils';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface ApprovalReason {
  text: string;
  severity?: 'info' | 'warning' | 'danger';
}

/** Wire shape from `GET /approvals/queue`. */
interface QueuedApproval {
  id: string;
  session_id: string;
  agent_role: string;
  tool_name: string;
  tool_args: Record<string, unknown>;
  created_at: number;
  ttl_seconds: number;
  nonce: string;
}

interface QueuedApprovalsResponse {
  pending: QueuedApproval[];
}

type Decision = 'allow' | 'reject';

// ---------------------------------------------------------------------------
// Pure helpers - derive design-only fields from generic tool_args.
// ---------------------------------------------------------------------------

function pickString(args: Record<string, unknown>, ...keys: string[]): string {
  for (const k of keys) {
    const v = args[k];
    if (typeof v === 'string' && v.length > 0) return v;
  }
  return '';
}

function pickStringList(args: Record<string, unknown>, ...keys: string[]): string[] {
  for (const k of keys) {
    const v = args[k];
    if (Array.isArray(v) && v.every((x) => typeof x === 'string')) {
      return v as string[];
    }
  }
  return [];
}

const REASON_DOT_KIND: Record<NonNullable<ApprovalReason['severity']>, 'failed' | 'stalled' | 'idle'> = {
  danger: 'failed',
  warning: 'stalled',
  info: 'idle',
};

/** Wait time in seconds, derived from epoch-second `created_at`. */
function waitSeconds(createdAt: number, now: number): number {
  return Math.max(0, Math.floor(now / 1000 - createdAt));
}

/** Derive the human "target" line (path / command / URL). */
function approvalTarget(a: QueuedApproval): string {
  return (
    pickString(a.tool_args, 'target', 'path', 'file', 'command', 'url', 'cmd') ||
    Object.keys(a.tool_args).slice(0, 3).join(' · ') ||
    '-'
  );
}

function approvalReasons(a: QueuedApproval): ApprovalReason[] {
  const raw = pickStringList(a.tool_args, 'reasons', 'why');
  if (raw.length > 0) return raw.map((text) => ({ text, severity: 'warning' }));
  // Fall back to a single explanatory line so the Why? card never reads empty.
  return [
    {
      text: `Tool "${a.tool_name}" requested by ${a.agent_role} requires explicit approval.`,
      severity: 'info',
    },
  ];
}

/** Result of resolving the diff payload for an approval.
 *
 * `kind` distinguishes a real diff from a JSON-args fallback or an absent
 * payload entirely so the UI can render the correct affordance. The previous
 * implementation conflated all three into a single string and crashed when
 * `diff` was explicitly `null` or a non-string value.
 */
type DiffPayload =
  | { kind: 'diff'; text: string }
  | { kind: 'args'; text: string }
  | { kind: 'empty' };

function approvalDiff(a: QueuedApproval): DiffPayload {
  // Defensive: `diff` / `patch` may be null, a number, or a non-string object.
  const raw = a.tool_args?.diff ?? a.tool_args?.patch;
  if (typeof raw === 'string' && raw.length > 0) {
    return { kind: 'diff', text: raw };
  }
  // Fall back to a JSON view of the args so operators can still inspect,
  // but only when there are actually args worth showing.
  if (a.tool_args && Object.keys(a.tool_args).length > 0) {
    try {
      return { kind: 'args', text: JSON.stringify(a.tool_args, null, 2) };
    } catch {
      return { kind: 'empty' };
    }
  }
  return { kind: 'empty' };
}

function diffCounts(diff: string): { plus: number; minus: number } {
  let plus = 0;
  let minus = 0;
  for (const line of diff.split('\n')) {
    if (line.startsWith('+') && !line.startsWith('+++')) plus += 1;
    else if (line.startsWith('-') && !line.startsWith('---')) minus += 1;
  }
  return { plus, minus };
}

function diffFilename(a: QueuedApproval): string {
  return pickString(a.tool_args, 'file', 'path', 'target', 'filename') || a.tool_name;
}

// ---------------------------------------------------------------------------
// Screen
// ---------------------------------------------------------------------------

/** Inline toast severity - only `error` extends the default 2s window. */
type ToastKind = 'info' | 'error';
interface ToastState {
  message: string;
  kind: ToastKind;
}

const QUEUE_KEY = ['approvals', 'queue'] as const;

export default function Approvals() {
  const queryClient = useQueryClient();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [toast, setToast] = useState<ToastState | null>(null);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const decisionInFlight = useRef(false);
  const queueHeading = useRef<HTMLHeadingElement>(null);
  const queueList = useRef<HTMLUListElement>(null);
  const decisionFocus = useRef<{ row: HTMLButtonElement; restore: boolean } | null>(null);
  // Re-render once a second so wait timers tick without refetching.
  const [now, setNow] = useState<number>(() => Date.now());

  const queueQ = useQuery<QueuedApprovalsResponse>({
    queryKey: QUEUE_KEY,
    queryFn: () => apiGet<QueuedApprovalsResponse>('/approvals/queue'),
    refetchInterval: 10_000,
  });

  const pending = useMemo(
    () => (Array.isArray(queueQ.data?.pending) ? queueQ.data.pending : []),
    [queueQ.data?.pending],
  );
  const selected = pending.find((approval) => approval.id === selectedId);

  // Auto-select the first row whenever the queue changes and the current
  // selection has fallen out of the list.
  useEffect(() => {
    if (pending.length === 0) {
      if (selectedId !== null) setSelectedId(null);
      return;
    }
    if (!selectedId || !pending.some((a) => a.id === selectedId)) {
      setSelectedId(pending[0].id);
    }
  }, [pending, selectedId]);

  // Tick the wait clock once a second while items are pending.
  useEffect(() => {
    if (pending.length === 0) return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [pending.length]);

  // SSE - refresh the queue cache on `approval_pending` notifications.
  useEventStream('/api/v1/events', {
    on: {
      approval_pending: () => {
        queryClient.invalidateQueries({ queryKey: QUEUE_KEY });
        queryClient.invalidateQueries({ queryKey: ['approvals'] });
      },
    },
  });

  // Errors deserve a longer dwell so operators can read what failed; happy-path
  // confirmations stay snappy at 2s.
  const showToast = (message: string, kind: ToastKind = 'info') => {
    setToast({ message, kind });
    if (toastTimer.current) clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(null), kind === 'error' ? 5000 : 2000);
  };

  useEffect(
    () => () => {
      if (toastTimer.current) clearTimeout(toastTimer.current);
    },
    [],
  );

  const { mutate: resolve, isPending: isResolving } = useMutation({
    mutationFn: ({ approval, decision }: { approval: QueuedApproval; decision: Decision }) =>
      apiPost<{ status: 'resolved'; id: string; decision: Decision }>(
        `/approvals/${encodeURIComponent(approval.id)}/resolve`,
        { decision, nonce: approval.nonce },
      ),
    onSuccess: ({ id, decision }) => {
      showToast(`${decision === 'allow' ? 'Approved' : 'Denied'} ${id}`);
      return queryClient.invalidateQueries({ queryKey: QUEUE_KEY });
    },
    onError: (error: unknown) => {
      if (error instanceof ApiError && (error.status === 409 || error.status === 410)) {
        showToast('This approval is no longer current. Review the refreshed queue.', 'error');
        return queryClient.invalidateQueries({ queryKey: QUEUE_KEY });
      }
      showToast(error instanceof Error ? error.message : 'Could not resolve approval', 'error');
    },
    onSettled: () => { decisionInFlight.current = false; },
  });

  useEffect(() => {
    const trackFocus = ({ target }: FocusEvent) => {
      const focus = decisionFocus.current;
      if (focus?.row.isConnected && target instanceof Node && target !== document.body) {
        focus.restore = focus.row.contains(target);
      }
    };
    // Clicking non-focusable content can leave focus on the body before refresh.
    const trackPointer = ({ target }: PointerEvent) => {
      const focus = decisionFocus.current;
      if (focus && target instanceof Node && !focus.row.contains(target)) {
        focus.restore = false;
      }
    };
    document.addEventListener('focusin', trackFocus, true);
    document.addEventListener('pointerdown', trackPointer, true);
    return () => {
      document.removeEventListener('focusin', trackFocus, true);
      document.removeEventListener('pointerdown', trackPointer, true);
    };
  }, []);

  useEffect(() => {
    const focus = decisionFocus.current;
    if (!focus) return;
    if (focus.row.isConnected) {
      if (!isResolving) decisionFocus.current = null;
      return;
    }

    decisionFocus.current = null;
    if (!focus.restore || document.activeElement !== document.body) return;
    const nextRow = queueList.current?.querySelector<HTMLButtonElement>('button[data-approval-id]');
    (nextRow ?? queueHeading.current)?.focus();
  }, [isResolving, pending]);

  const submitDecision = useCallback((decision: Decision, row?: HTMLButtonElement) => {
    if (queueQ.isError || isResolving || decisionInFlight.current || !selected?.nonce) {
      return;
    }
    decisionInFlight.current = true;
    decisionFocus.current = row ? { row, restore: true } : null;
    resolve({ approval: selected, decision });
  }, [queueQ.isError, isResolving, selected, resolve]);

  function handleQueueKey(event: KeyboardEvent<HTMLUListElement>) {
    const { target, key } = event;
    if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey) return;
    if (!(target instanceof HTMLButtonElement) || !target.dataset.approvalId) return;

    if (key === 'ArrowDown' || key === 'ArrowUp') {
      const buttons = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('button[data-approval-id]'));
      const index = buttons.indexOf(target);
      if (index < 0) return;
      const offset = key === 'ArrowDown' ? 1 : -1;
      const next = buttons[(index + offset + buttons.length) % buttons.length];
      event.preventDefault();
      setSelectedId(next.dataset.approvalId ?? null);
      next.focus();
      return;
    }

    // A focused but unselected row must never decide a different request.
    if (target.dataset.approvalId !== selectedId || event.repeat) return;
    const shortcut = key.toLowerCase();
    if (shortcut !== 'a' && shortcut !== 'd') return;
    event.preventDefault();
    submitDecision(shortcut === 'a' ? 'allow' : 'reject', target);
  }

  // -------------------------------------------------------------------------
  // Top-level state branches
  // -------------------------------------------------------------------------

  if (queueQ.isLoading) {
    return (
      <div className="grid h-full grid-cols-1 overflow-hidden md:grid-cols-[440px_1fr]">
        <div className="flex flex-col border-b border-border bg-secondary p-4 md:border-b-0 md:border-r">
          <SectionLabel className="mb-3">Approvals queue</SectionLabel>
          <LoadingState rows={5} />
        </div>
        <div className="p-6">
          <LoadingState rows={4} label="Loading selected approval" />
        </div>
      </div>
    );
  }

  if (queueQ.isError) {
    return (
      <div className="p-6">
        <ErrorState
          title="Could not load approvals"
          message={
            queueQ.error instanceof Error
              ? queueQ.error.message
              : 'Approvals queue request failed.'
          }
          retry={() => queueQ.refetch()}
        />
      </div>
    );
  }

  const oldestWait =
    pending.length > 0
      ? Math.max(...pending.map((a) => waitSeconds(a.created_at, now)))
      : 0;

  return (
    <div className="grid h-full grid-cols-1 overflow-hidden md:grid-cols-[440px_1fr]">
      {/* LEFT - queue list. On narrow viewports we stack and cap height so the
          selected approval still gets screen real estate below. */}
      <aside className="flex max-h-[40vh] flex-col overflow-hidden border-b border-border bg-secondary md:max-h-none md:border-b-0 md:border-r">
        <header className="px-[18px] pb-3 pt-[18px]">
          <h2 ref={queueHeading} tabIndex={-1} className="text-h2 text-foreground">Approvals queue</h2>
          <div className="mt-1 text-[12px] text-muted-foreground">
            <span className="font-mono tabular-nums">{pending.length}</span> pending
            {pending.length > 0 && (
              <>
                {' · oldest '}
                <span className="font-mono tabular-nums">
                  {formatDuration(oldestWait, 's')}
                </span>
              </>
            )}
          </div>
        </header>
        <div className="flex-1 overflow-auto">
          {pending.length === 0 ? (
            <div className="p-4">
              <EmptyState
                title="No pending approvals"
                description="quiet on the conducting podium"
              />
            </div>
          ) : (
            <ul
              ref={queueList}
              role="list"
              aria-label="Pending approvals"
              onKeyDown={handleQueueKey}
              className="flex flex-col"
            >
              {pending.map((approval) => (
                <QueueRow
                  key={approval.id}
                  approval={approval}
                  selected={approval.id === selectedId}
                  waitS={waitSeconds(approval.created_at, now)}
                  onClick={() => setSelectedId(approval.id)}
                />
              ))}
            </ul>
          )}
        </div>
      </aside>

      {/* RIGHT - selected approval */}
      <section className="flex flex-col gap-3.5 overflow-auto px-[22px] py-[18px]">
        {selected ? (
          <SelectedApproval
            approval={selected}
            now={now}
            pending={isResolving}
            onDecision={submitDecision}
          />
        ) : (
          <EmptyState
            title="No pending approvals"
            description="quiet on the conducting podium"
          />
        )}
      </section>

      {/* Inline toast - bottom-right; success auto-clears after 2s, errors 5s. */}
      <div
        aria-live={toast?.kind === 'error' ? 'assertive' : 'polite'}
        role={toast?.kind === 'error' ? 'alert' : 'status'}
        className="pointer-events-none fixed bottom-10 right-6 z-50"
      >
        {toast && (
          <div
            className={cn(
              'pointer-events-auto rounded-md border px-3 py-2 text-body shadow-md',
              toast.kind === 'error'
                ? 'border-destructive/50 bg-destructive/15 text-destructive'
                : 'border-border bg-card text-foreground',
            )}
          >
            {toast.message}
          </div>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Queue row
// ---------------------------------------------------------------------------

interface QueueRowProps {
  approval: QueuedApproval;
  selected: boolean;
  waitS: number;
  onClick: () => void;
}

function QueueRow({ approval, selected, waitS, onClick }: QueueRowProps) {
  const target = approvalTarget(approval);
  // Keep the selected request visible when the queue changes.
  const buttonRef = useRef<HTMLButtonElement | null>(null);
  useEffect(() => {
    if (!selected) return;
    const el = buttonRef.current;
    if (!el) return;
    el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }, [selected]);

  return (
    <li>
      <button
        ref={buttonRef}
        type="button"
        data-approval-id={approval.id}
        aria-current={selected ? 'true' : undefined}
        aria-keyshortcuts={selected ? 'ArrowUp ArrowDown A D' : 'ArrowUp ArrowDown'}
        onClick={onClick}
        className={cn(
          'group flex w-full items-start justify-between gap-2.5 border-b border-border-subtle px-4 py-3 text-left transition-colors',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset',
          selected
            ? 'border-l-2 border-l-accent bg-secondary'
            : 'border-l-2 border-l-transparent hover:bg-card/40',
        )}
      >
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="rounded-sm border border-border-subtle bg-surface-raised px-1.5 py-px font-mono text-[11px] font-medium text-foreground">
              {approval.tool_name}
            </span>
            <Pill kind="ghost">{approval.agent_role}</Pill>
          </div>
          <div className="mt-1.5 truncate font-mono text-[11.5px] text-foreground">
            {target}
          </div>
          <div className="mt-1.5 flex items-center gap-2.5">
            <span className="font-mono text-[10.5px] text-meta-foreground">
              {approval.session_id}
            </span>
          </div>
        </div>
        <div className="shrink-0 text-right">
          <div className="mt-1.5 font-mono text-[10.5px] tabular-nums text-meta-foreground">
            {formatDuration(waitS, 's')}
          </div>
        </div>
      </button>
    </li>
  );
}

// ---------------------------------------------------------------------------
// Selected approval (Why? + Diff + Action bar)
// ---------------------------------------------------------------------------

interface SelectedApprovalProps {
  approval: QueuedApproval;
  now: number;
  pending: boolean;
  onDecision: (decision: Decision) => void;
}

function SelectedApproval({
  approval,
  now,
  pending,
  onDecision,
}: SelectedApprovalProps) {
  const reasons = approvalReasons(approval);
  const diffPayload = approvalDiff(approval);
  // Only count +/- from real diff text; JSON-args fallback would otherwise
  // produce confusing "+12 −0" badges from quoted property syntax.
  const { plus, minus } =
    diffPayload.kind === 'diff' ? diffCounts(diffPayload.text) : { plus: 0, minus: 0 };
  const filename = diffFilename(approval);
  const target = approvalTarget(approval);
  const waitS = waitSeconds(approval.created_at, now);

  const disabled = pending || !approval.nonce;

  return (
    <>
      {/* Header */}
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="font-mono text-[11px] uppercase tracking-[0.1em] text-meta-foreground">
            APPROVAL · {approval.id}
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-2.5 text-h2 text-foreground">
            <span>{approval.tool_name}</span>
            <span className="text-[14px] font-normal text-meta-foreground">
              requested by
            </span>
            <span className="font-mono text-[14px] font-medium">
              {approval.session_id}
            </span>
          </div>
          <div className="mt-1.5 font-mono text-[12px] text-muted-foreground">
            <span className="truncate">{target}</span>
          </div>
        </div>
        <div className="shrink-0 text-right">
          <div className="mt-1.5 font-mono text-[11px] tabular-nums text-meta-foreground">
            waiting {formatDuration(waitS, 's')} · expires after {formatDuration(approval.ttl_seconds, 's')}
          </div>
        </div>
      </div>

      {/* Why? card */}
      <div className="rounded-md border border-border bg-card p-3.5">
        <div className="mb-2 flex items-center gap-2">
          <ShieldCheck className="size-3.5 text-foreground" strokeWidth={1.5} />
          <span className="text-body-md text-foreground">Why this needs review</span>
          <span className="flex-1" />
          <span className="font-mono text-[10.5px] text-meta-foreground">
            {reasons.length} {reasons.length === 1 ? 'reason' : 'reasons'}
          </span>
        </div>
        <ul className="m-0 flex list-none flex-col gap-1.5 p-0">
          {reasons.map((r, i) => (
            <li
              key={i}
              className="flex items-start gap-2 text-[12.5px] text-foreground"
            >
              <span className="pt-1.5">
                <StatusDot kind={REASON_DOT_KIND[r.severity ?? 'info']} />
              </span>
              <span>{r.text}</span>
            </li>
          ))}
        </ul>
      </div>

      {/* Diff card */}
      <div className="overflow-hidden rounded-md border border-border bg-card">
        <div className="sticky top-0 z-10 flex items-center justify-between border-b border-border bg-surface-raised px-3.5 py-2.5">
          <div className="font-mono text-[11.5px] text-foreground">
            {filename}
            {diffPayload.kind === 'diff' ? (
              <span className="ml-2.5 tabular-nums text-meta-foreground">
                <span className="text-success">+{plus}</span>{' '}
                <span className="text-destructive">−{minus}</span>
              </span>
            ) : (
              <span className="ml-2.5 text-meta-foreground">
                {diffPayload.kind === 'args' ? '· tool args' : '· no diff available'}
              </span>
            )}
          </div>
        </div>
        {diffPayload.kind === 'empty' ? (
          <div className="px-3.5 py-6">
            <EmptyState
              title="No diff available"
              description="This tool call did not include a diff or patch payload."
            />
          </div>
        ) : (
          <pre
            className="m-0 max-h-[240px] overflow-auto bg-background px-3.5 py-2.5 font-mono text-log text-foreground"
            aria-label={diffPayload.kind === 'diff' ? 'Approval diff' : 'Approval tool args'}
          >
            {diffPayload.text.split('\n').map((line, i) => (
              <span
                key={i}
                className={cn(
                  'block',
                  diffPayload.kind === 'diff' &&
                    line.startsWith('+') &&
                    !line.startsWith('+++') &&
                    'bg-success/20 text-success',
                  diffPayload.kind === 'diff' &&
                    line.startsWith('-') &&
                    !line.startsWith('---') &&
                    'bg-destructive/15 text-destructive',
                )}
              >
                {line || ' '}
              </span>
            ))}
          </pre>
        )}
      </div>

      {/* Action bar */}
      <div className="flex flex-wrap items-center gap-2 rounded-md border border-border bg-card p-3">
        <div className="flex items-center gap-1.5">
          <button
            type="button"
            disabled={disabled}
            onClick={() => onDecision('allow')}
            className={cn(
              'inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-body-md text-primary-foreground transition-colors',
              'hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background',
              'disabled:opacity-60',
            )}
          >
            <Check className="size-3.5" strokeWidth={2} />
            Approve once
          </button>
          <button
            type="button"
            disabled={disabled}
            onClick={() => onDecision('reject')}
            className={cn(
              'inline-flex items-center gap-1.5 rounded-md border border-destructive bg-transparent px-3 py-1.5 text-body-md text-destructive transition-colors',
              'hover:bg-destructive/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-destructive focus-visible:ring-offset-2 focus-visible:ring-offset-background',
              'disabled:opacity-60',
            )}
          >
            <X className="size-3.5" strokeWidth={2} />
            Deny
          </button>
        </div>
        {!approval.nonce && (
          <p role="alert" className="text-body text-muted-foreground">
            This request has no approval nonce. Reload the queue before deciding.
          </p>
        )}
        <div className="font-mono text-[10.5px] text-meta-foreground">
          With the selected queue row focused:{' '}
          <kbd className="mr-1 rounded-sm border border-border-subtle px-1.5 py-px">
            A
          </kbd>
          = approve ·{' '}
          <kbd className="mx-1 rounded-sm border border-border-subtle px-1.5 py-px">
            D
          </kbd>
          = deny
        </div>
      </div>
    </>
  );
}
