// Drives the live log buffer for a single task.
//
// Responsibilities:
//   1. Subscribe to the SSE endpoint `/api/v1/dashboard/tasks/{id}/logs/stream`
//      via the shared `useEventStream` helper.
//   2. Maintain a bounded FIFO buffer of parsed `LogLine` records.
//   3. Track lifecycle phase (connecting / live / paused / reconnecting /
//      complete / failed) so the UI can render the right affordance.
//   4. When the server ends a terminal task before any line lands, fall back
//      to the cached `log_tail` from the task-detail endpoint so the operator
//      still sees historical output.
//   5. Honour a `paused` flag: while paused, incoming lines accrue in a
//      separate "pending" queue, surfaced as a "+N new" pill the user clicks
//      to flush.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { apiGet } from '@/lib/api';
import { useEventStream } from '@/lib/sse';

import { parseLine } from './parseLine';
import type { CompleteStatus, LogLine, LogPhase } from './types';
import { LOG_BUFFER_CAP } from './types';

export interface UseTaskLogStreamOptions {
  /** Resolved task identifier. Empty string disables the hook. */
  taskId: string;
  /** Mount/unmount toggle - used to suspend SSE when the tab isn't active. */
  enabled: boolean;
  /** When true, new lines go into the pending queue instead of `lines`. */
  paused: boolean;
}

export interface TaskLogStreamState {
  lines: LogLine[];
  pendingCount: number;
  totalBytes: number;
  totalLines: number;
  phase: LogPhase;
  completeStatus: CompleteStatus;
  flushPending: () => void;
  clear: () => void;
}

interface InternalBuffer {
  visible: LogLine[];
  pending: LogLine[];
  totalBytes: number;
  totalLines: number;
}

const EMPTY_BUFFER: InternalBuffer = {
  visible: [],
  pending: [],
  totalBytes: 0,
  totalLines: 0,
};

function trimToCap(arr: LogLine[]): LogLine[] {
  if (arr.length <= LOG_BUFFER_CAP) return arr;
  return arr.slice(arr.length - LOG_BUFFER_CAP);
}

export function useTaskLogStream({
  taskId,
  enabled,
  paused,
}: UseTaskLogStreamOptions): TaskLogStreamState {
  const [buffer, setBuffer] = useState<InternalBuffer>(EMPTY_BUFFER);
  const [phase, setPhase] = useState<LogPhase>('connecting');
  const [completeStatus, setCompleteStatus] = useState<CompleteStatus>(null);
  const [fallbackChecked, setFallbackChecked] = useState(false);
  // Monotonically increasing across the entire mount, including across pause
  // and tail-fetch fallback. Stored as a ref so multiple state setters in the
  // same React batch can read the latest value without races.
  const idCounterRef = useRef(0);
  const pausedRef = useRef(paused);
  pausedRef.current = paused;

  const url = useMemo(() => {
    if (!taskId) return '';
    return `/api/v1/tasks/${encodeURIComponent(taskId)}/logs/stream`;
  }, [taskId]);

  const ingestLine = useCallback((raw: string, receivedAt: number) => {
    const id = idCounterRef.current;
    idCounterRef.current += 1;
    const parsed = parseLine(raw, id, receivedAt);
    const bytes = raw.length + 1; // +1 for the newline the backend stripped
    setBuffer((prev) => {
      const isPaused = pausedRef.current;
      if (isPaused) {
        return {
          visible: prev.visible,
          pending: trimToCap([...prev.pending, parsed]),
          totalBytes: prev.totalBytes + bytes,
          totalLines: prev.totalLines + 1,
        };
      }
      return {
        visible: trimToCap([...prev.visible, parsed]),
        pending: prev.pending,
        totalBytes: prev.totalBytes + bytes,
        totalLines: prev.totalLines + 1,
      };
    });
  }, []);

  // Move every pending line into the visible buffer in a single tick. The
  // operator does this by clicking the "+N new lines" pill or by scrolling
  // to the bottom while the panel is paused.
  const flushPending = useCallback(() => {
    setBuffer((prev) => {
      if (prev.pending.length === 0) return prev;
      const merged = trimToCap([...prev.visible, ...prev.pending]);
      return { ...prev, visible: merged, pending: [] };
    });
  }, []);

  const clear = useCallback(() => {
    idCounterRef.current = 0;
    setBuffer(EMPTY_BUFFER);
  }, []);

  // ── SSE wiring ───────────────────────────────────────────────────────────
  useEventStream(url, {
    enabled: enabled && url !== '',
    on: {
      log: (data: unknown) => {
        const line = typeof data === 'string' ? data : JSON.stringify(data);
        ingestLine(line, Date.now());
        setPhase((p) => (p === 'paused' ? p : 'live'));
      },
      ping: () => {
        setPhase((p) => {
          if (p === 'connecting' || p === 'reconnecting') return 'live';
          return p;
        });
      },
      complete: (data: unknown) => {
        const status = (data as { status?: string } | null)?.status ?? null;
        setCompleteStatus(
          status === 'done' || status === 'failed' || status === 'cancelled'
            ? status
            : null,
        );
        setPhase('complete');
      },
      close: () => {
        setPhase((p) => (p === 'failed' ? p : 'complete'));
      },
    },
    onError: () => setPhase('failed'),
  });

  // Reflect `paused` prop changes into the phase pill - but only when we're
  // currently live. Pausing a failed/completed stream does nothing.
  useEffect(() => {
    setPhase((p) => {
      if (paused && p === 'live') return 'paused';
      if (!paused && p === 'paused') return 'live';
      return p;
    });
  }, [paused]);

  // Terminal-task fallback: SSE has closed and we received zero lines →
  // fetch the cached tail so the operator still sees history. Runs at most
  // once per (taskId, mount).
  useEffect(() => {
    if (phase !== 'complete' || fallbackChecked || !taskId) return;
    if (buffer.totalLines > 0) {
      setFallbackChecked(true);
      return;
    }
    setFallbackChecked(true);
    let cancelled = false;
    // ``apiGet`` prepends ``/api/v1`` via ``buildUrl``; pass the bare path so
    // there's no risk of a future regression producing ``/api/v1/api/v1/...``.
    apiGet<{ log_tail?: string }>(
      `/tasks/${encodeURIComponent(taskId)}/detail`,
    )
      .then((detail) => {
        if (cancelled) return;
        const tail = detail.log_tail;
        if (typeof tail !== 'string' || tail.length === 0) return;
        const lines = tail.split('\n').filter((l) => l.length > 0);
        const now = Date.now();
        for (const raw of lines) {
          ingestLine(raw, now);
        }
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [phase, fallbackChecked, taskId, buffer.totalLines, ingestLine]);

  // Reset state when `taskId` changes - otherwise the next task would start
  // with the previous one's buffer.
  const prevTaskRef = useRef(taskId);
  useEffect(() => {
    if (prevTaskRef.current !== taskId) {
      prevTaskRef.current = taskId;
      idCounterRef.current = 0;
      setBuffer(EMPTY_BUFFER);
      setPhase('connecting');
      setCompleteStatus(null);
      setFallbackChecked(false);
    }
  }, [taskId]);

  return {
    lines: buffer.visible,
    pendingCount: buffer.pending.length,
    totalBytes: buffer.totalBytes,
    totalLines: buffer.totalLines,
    phase,
    completeStatus,
    flushPending,
    clear,
  };
}
