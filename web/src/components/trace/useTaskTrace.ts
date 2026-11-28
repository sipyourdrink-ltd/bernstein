// Polls /dashboard/tasks/{id}/trace and keeps the Trace tab in sync.
//
// Lifecycle:
//   1. Mount → immediate fetch; transitions phase from 'idle' → 'loading' →
//      either 'ready' or 'error'.
//   2. While `has_open_trace` is true and the tab is `enabled`, refetches on
//      a fixed interval (`TRACE_POLL_INTERVAL_MS`) so the timeline streams in
//      new events without the user reloading.
//   3. As soon as the trace closes (every embedded trace has `end_ts`), the
//      poller stops - we treat traces as append-only, so the last successful
//      snapshot is the final one.
//   4. Honours an `enabled` flag so we don't burn cycles when the user
//      switches to a different tab.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { apiGet } from '@/lib/api';
import { ApiError } from '@/lib/api';

import type { TraceTimelineEvent, TraceTimelineResponse } from './types';
import { TRACE_PAGE_LIMIT, TRACE_POLL_INTERVAL_MS } from './types';

export type TracePhase = 'idle' | 'loading' | 'ready' | 'error';

export interface UseTaskTraceState {
  events: TraceTimelineEvent[];
  total: number;
  firstTs: number | null;
  lastTs: number | null;
  hasOpenTrace: boolean;
  phase: TracePhase;
  error: string | null;
  /** Server-reported clock used for relative-time rendering. */
  lastFetchedAt: number | null;
  /** Increments every successful fetch so panels can flash "live" indicators. */
  generation: number;
  /** Imperative refresh button - also clears prior errors before retrying. */
  refresh: () => void;
}

export function useTaskTrace(taskId: string, enabled: boolean): UseTaskTraceState {
  const [events, setEvents] = useState<TraceTimelineEvent[]>([]);
  const [total, setTotal] = useState(0);
  const [firstTs, setFirstTs] = useState<number | null>(null);
  const [lastTs, setLastTs] = useState<number | null>(null);
  const [hasOpenTrace, setHasOpenTrace] = useState(false);
  const [phase, setPhase] = useState<TracePhase>('idle');
  const [error, setError] = useState<string | null>(null);
  const [lastFetchedAt, setLastFetchedAt] = useState<number | null>(null);
  const [generation, setGeneration] = useState(0);

  // Race-condition guard: when the user switches tasks mid-flight, an in-flight
  // fetch from the previous taskId must not clobber state for the new one.
  const requestSeq = useRef(0);
  // Used by the refresh() callback to bypass the "skip duplicate" logic.
  const forceCounter = useRef(0);

  const doFetch = useCallback(async () => {
    if (!taskId) return;
    const mySeq = ++requestSeq.current;
    setPhase((prev) => (prev === 'ready' ? prev : 'loading'));
    try {
      // Trace endpoint paginates server-side; we always page through to the
      // end so the FE sees the full timeline.
      const all: TraceTimelineEvent[] = [];
      let nextCursor: number | null = 0;
      let firstResponse: TraceTimelineResponse | null = null;
      while (nextCursor != null) {
        const params: URLSearchParams = new URLSearchParams({
          limit: String(TRACE_PAGE_LIMIT),
          cursor: String(nextCursor),
        });
        const resp: TraceTimelineResponse = await apiGet<TraceTimelineResponse>(
          `/tasks/${encodeURIComponent(taskId)}/trace?${params.toString()}`,
        );
        if (firstResponse == null) firstResponse = resp;
        all.push(...resp.events);
        nextCursor = resp.cursor;
        // Defensive ceiling: refuse to spin forever on a misbehaving backend.
        if (all.length >= TRACE_PAGE_LIMIT * 20) break;
      }
      // A newer fetch already wrote - drop this stale result.
      if (mySeq !== requestSeq.current) return;
      setEvents(all);
      setTotal(firstResponse?.total ?? all.length);
      setFirstTs(firstResponse?.first_ts ?? (all[0]?.ts ?? null));
      setLastTs(firstResponse?.last_ts ?? (all[all.length - 1]?.ts ?? null));
      setHasOpenTrace(Boolean(firstResponse?.has_open_trace));
      setPhase('ready');
      setError(null);
      setLastFetchedAt(Date.now());
      setGeneration((g) => g + 1);
    } catch (err) {
      if (mySeq !== requestSeq.current) return;
      const msg =
        err instanceof ApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : 'Failed to load trace';
      setPhase('error');
      setError(msg);
    }
  }, [taskId]);

  // Initial fetch + dependency-driven refetch (taskId change, enable toggle).
  useEffect(() => {
    if (!enabled || !taskId) return;
    void doFetch();
  }, [enabled, taskId, doFetch]);

  // Poll while the trace is still open.
  useEffect(() => {
    if (!enabled || !taskId) return;
    if (phase === 'error') return;
    if (!hasOpenTrace) return;
    const id = window.setInterval(() => {
      void doFetch();
    }, TRACE_POLL_INTERVAL_MS);
    return () => {
      window.clearInterval(id);
    };
  }, [enabled, taskId, hasOpenTrace, phase, doFetch]);

  const refresh = useCallback(() => {
    forceCounter.current += 1;
    setError(null);
    void doFetch();
  }, [doFetch]);

  return useMemo(
    () => ({
      events,
      total,
      firstTs,
      lastTs,
      hasOpenTrace,
      phase,
      error,
      lastFetchedAt,
      generation,
      refresh,
    }),
    [events, total, firstTs, lastTs, hasOpenTrace, phase, error, lastFetchedAt, generation, refresh],
  );
}
