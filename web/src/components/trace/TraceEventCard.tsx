// A single event card in the Trace timeline.
//
// The card is collapsed by default - clicking the header (or pressing Enter /
// Space while focused) reveals the full JSON payload. Hover shows the absolute
// ISO timestamp; the visible label uses wall-clock + relative time so the
// operator can scrub quickly without reading every digit.

import { useCallback, useMemo, useState } from 'react';

import { cn } from '@/lib/utils';

import { cardStyle, formatIso, formatRelativeSeconds, formatWallClock, kindLabel, prettyJson } from './format';
import type { TraceTimelineEvent } from './types';

interface Props {
  event: TraceTimelineEvent;
  // Search query - when non-empty, matched substrings in the summary are
  // visually highlighted (case-insensitive).
  query: string;
  /** Now in unix seconds, passed down so cards animate in lockstep when the clock ticks. */
  now: number;
}

function highlight(text: string, query: string): React.ReactNode {
  if (!query) return text;
  const idx = text.toLowerCase().indexOf(query.toLowerCase());
  if (idx < 0) return text;
  return (
    <>
      {text.slice(0, idx)}
      <mark className="rounded-sm bg-warning/30 px-0.5 text-foreground">
        {text.slice(idx, idx + query.length)}
      </mark>
      {text.slice(idx + query.length)}
    </>
  );
}

export function TraceEventCard({ event, query, now }: Props) {
  const [expanded, setExpanded] = useState(false);
  const [copied, setCopied] = useState(false);

  const style = useMemo(() => cardStyle(event.kind, event.outcome), [event.kind, event.outcome]);
  const wall = useMemo(() => formatWallClock(event.ts), [event.ts]);
  const iso = useMemo(() => formatIso(event.ts), [event.ts]);
  const rel = useMemo(() => formatRelativeSeconds(event.ts, now), [event.ts, now]);

  const onCopy = useCallback(
    async (e: React.MouseEvent) => {
      e.stopPropagation();
      try {
        await navigator.clipboard.writeText(prettyJson(event.payload));
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1200);
      } catch {
        /* clipboard blocked - silent */
      }
    },
    [event.payload],
  );

  const onToggle = useCallback(() => setExpanded((b) => !b), []);

  const onKey = useCallback(
    (e: React.KeyboardEvent<HTMLDivElement>) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        onToggle();
      }
    },
    [onToggle],
  );

  return (
    <div className="group relative flex gap-3">
      <div className="flex w-[78px] shrink-0 flex-col items-end pt-1.5 text-right" title={iso}>
        <span className="font-mono text-[11px] tabular-nums text-foreground">{wall}</span>
        <span className="font-mono text-[10px] tabular-nums text-meta-foreground">{rel}</span>
      </div>
      <div className="relative flex flex-1 flex-col">
        {/* Dot + connector rail */}
        <span
          aria-hidden="true"
          className={cn('absolute left-[-14px] top-3 size-2.5 rounded-full ring-2', style.rail)}
        />
        <div
          role="button"
          tabIndex={0}
          aria-expanded={expanded}
          onClick={onToggle}
          onKeyDown={onKey}
          className={cn(
            'cursor-pointer rounded-md border border-border-subtle bg-card px-3 py-2 transition-colors hover:bg-surface-raised/60',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background',
          )}
        >
          <div className="flex flex-wrap items-center gap-2">
            <span
              className={cn(
                'inline-flex items-center gap-1 rounded-full border px-2 py-px font-mono text-[10px] uppercase tracking-[0.12em]',
                style.pill,
              )}
            >
              {kindLabel(event.kind)}
            </span>
            {event.actor && (
              <span className="font-mono text-[10.5px] text-muted-foreground">{event.actor}</span>
            )}
            {event.trace_id && (
              <span
                className="font-mono text-[10px] text-meta-foreground"
                title={`session ${event.session_id}`}
              >
                {event.trace_id.slice(0, 8)}
              </span>
            )}
            <button
              type="button"
              onClick={onCopy}
              className="ml-auto rounded-sm border border-border-subtle bg-card px-2 py-px font-mono text-[10px] text-muted-foreground hover:bg-surface-raised hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
              aria-label="Copy event payload"
            >
              {copied ? 'copied' : 'copy'}
            </button>
          </div>
          <div className="mt-1 truncate text-[12.5px] text-foreground">
            {highlight(event.summary || '(no summary)', query)}
          </div>
          {expanded && (
            <pre
              className="mt-2 max-h-[320px] overflow-auto rounded-sm bg-surface-raised/60 p-2 font-mono text-[11px] leading-snug text-foreground"
              // The <pre> tag is keyboard-focusable already; stop propagation so
              // clicking inside (e.g. to select text) doesn't collapse the card.
              onClick={(e) => e.stopPropagation()}
            >
              {prettyJson(event.payload)}
            </pre>
          )}
        </div>
      </div>
    </div>
  );
}
