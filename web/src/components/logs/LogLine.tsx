// Single log line renderer.
//
// Responsibilities:
//   - Render parsed ANSI segments with the right foreground/background.
//   - Highlight any search matches with `<mark>` semantics.
//   - Surface a level badge (ERROR/WARN/…) when one was parsed.
//   - Surface a timestamp prefix when one was parsed.
//   - Copy-on-click via a context-menu-style affordance.
//
// Performance notes: this component is rendered for every visible row inside
// the virtual window. It memoises on the input identity so the only
// re-renders are when search state changes for this line's id.

import {
  memo,
  useCallback,
  useMemo,
  useState,
  type MouseEvent,
  type ReactNode,
} from 'react';
import { Check, Copy } from 'lucide-react';

import { cn } from '@/lib/utils';

import { parseAnsi, type AnsiSegment } from './ansi';
import type { LogLine as LogLineRecord, SearchMatch } from './types';
import { copyToClipboard, formatLocalTime } from './utils';

interface Props {
  line: LogLineRecord;
  /** Matches that fall on this specific line (already filtered by id). */
  matches: ReadonlyArray<SearchMatch>;
  /** Match index of the currently-focused match (-1 if none). */
  activeMatchIndex: number;
  /** Offset into the global match array of the first entry in `matches`. */
  globalMatchOffset: number;
  onActivate?: (id: number) => void;
  showTimestamp: boolean;
  showLevel: boolean;
}

const LEVEL_TONE: Record<string, string> = {
  error: 'bg-destructive/15 text-destructive ring-destructive/30',
  warn: 'bg-warning/15 text-warning ring-warning/30',
  info: 'bg-accent/10 text-accent ring-accent/30',
  debug: 'bg-card text-meta-foreground ring-border-subtle',
  trace: 'bg-card text-meta-foreground ring-border-subtle',
};

const ANSI_FG_CLASS: Record<string, string> = {
  'ansi-black': 'text-zinc-900 dark:text-zinc-300',
  'ansi-red': 'text-red-800 dark:text-red-300',
  'ansi-green': 'text-emerald-800 dark:text-emerald-300',
  'ansi-yellow': 'text-amber-800 dark:text-amber-300',
  'ansi-blue': 'text-blue-800 dark:text-sky-300',
  'ansi-magenta': 'text-fuchsia-800 dark:text-fuchsia-300',
  'ansi-cyan': 'text-cyan-800 dark:text-cyan-300',
  'ansi-white': 'text-zinc-800 dark:text-zinc-200',
  'ansi-bright-black': 'text-zinc-700 dark:text-zinc-200',
  'ansi-bright-red': 'text-red-900 dark:text-red-200',
  'ansi-bright-green': 'text-emerald-900 dark:text-emerald-200',
  'ansi-bright-yellow': 'text-amber-900 dark:text-amber-200',
  'ansi-bright-blue': 'text-blue-900 dark:text-sky-200',
  'ansi-bright-magenta': 'text-fuchsia-900 dark:text-fuchsia-200',
  'ansi-bright-cyan': 'text-cyan-900 dark:text-cyan-200',
  'ansi-bright-white': 'text-zinc-900 dark:text-zinc-100',
};

const ANSI_BG_CLASS: Record<string, string> = {
  'ansi-red': 'bg-red-500/20',
  'ansi-green': 'bg-emerald-500/20',
  'ansi-yellow': 'bg-amber-500/20',
  'ansi-blue': 'bg-sky-500/20',
  'ansi-magenta': 'bg-fuchsia-500/20',
  'ansi-cyan': 'bg-cyan-500/20',
};

function segmentClass(seg: AnsiSegment): string {
  const parts: string[] = [];
  if (seg.fg && ANSI_FG_CLASS[seg.fg]) parts.push(ANSI_FG_CLASS[seg.fg]);
  if (seg.bg && ANSI_BG_CLASS[seg.bg]) parts.push(ANSI_BG_CLASS[seg.bg]);
  if (seg.bold) parts.push('font-semibold');
  if (seg.dim) parts.push('opacity-95');
  if (seg.italic) parts.push('italic');
  if (seg.underline) parts.push('underline');
  return parts.join(' ');
}

/**
 * Splits a single ANSI segment by character offsets so highlighted ranges can
 * be rendered as `<mark>` while preserving the ANSI styling. Offsets are
 * absolute in the line's `plain` text; `rangeStart` is the position of the
 * segment within that text so we can re-base before slicing.
 */
function spliceMatches(
  text: string,
  cls: string,
  rangeStart: number,
  highlights: ReadonlyArray<{ start: number; end: number; active: boolean }>,
): ReactNode[] {
  if (highlights.length === 0) {
    return [
      <span key="t" className={cls}>
        {text}
      </span>,
    ];
  }
  const out: ReactNode[] = [];
  let cursor = 0;
  for (const h of highlights) {
    const localStart = Math.max(0, h.start - rangeStart);
    const localEnd = Math.min(text.length, h.end - rangeStart);
    if (localStart >= text.length || localEnd <= 0 || localEnd <= localStart) continue;
    if (cursor < localStart) {
      out.push(
        <span key={`p-${cursor}`} className={cls}>
          {text.slice(cursor, localStart)}
        </span>,
      );
    }
    out.push(
      <mark
        key={`m-${localStart}`}
        className={cn(
          cls,
          h.active
            ? 'bg-warning/40 ring-1 ring-warning/60'
            : 'bg-accent/25',
          'text-foreground dark:text-foreground',
          'rounded-[2px] px-0.5',
        )}
      >
        {text.slice(localStart, localEnd)}
      </mark>,
    );
    cursor = localEnd;
  }
  if (cursor < text.length) {
    out.push(
      <span key={`p-${cursor}`} className={cls}>
        {text.slice(cursor)}
      </span>,
    );
  }
  return out;
}

function LogLineImpl({
  line,
  matches,
  activeMatchIndex,
  globalMatchOffset,
  onActivate,
  showTimestamp,
  showLevel,
}: Props) {
  const segments = useMemo(() => parseAnsi(line.raw), [line.raw]);
  const ranges = useMemo(
    () =>
      matches.map((m, localIdx) => ({
        start: m.start,
        end: m.end,
        active: globalMatchOffset + localIdx === activeMatchIndex,
      })),
    [matches, activeMatchIndex, globalMatchOffset],
  );

  const [copied, setCopied] = useState(false);
  const handleCopy = useCallback(
    async (e: MouseEvent) => {
      e.stopPropagation();
      const ok = await copyToClipboard(line.raw);
      if (ok) {
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1200);
      }
    },
    [line.raw],
  );
  const handleClick = useCallback(() => {
    if (onActivate) onActivate(line.id);
  }, [line.id, onActivate]);

  const renderedSegments = useMemo(() => {
    const nodes: ReactNode[] = [];
    let charCursor = 0;
    let segIdx = 0;
    for (const seg of segments) {
      const segLen = seg.text.length;
      const segHighlights = ranges.filter(
        (r) => r.start < charCursor + segLen && r.end > charCursor,
      );
      const cls = segmentClass(seg);
      nodes.push(
        <span key={segIdx}>{spliceMatches(seg.text, cls, charCursor, segHighlights)}</span>,
      );
      charCursor += segLen;
      segIdx += 1;
    }
    return nodes;
  }, [segments, ranges]);

  const ts = showTimestamp ? (line.timestamp ?? line.receivedAt) : null;
  const levelClass = line.level ? LEVEL_TONE[line.level] : null;

  return (
    <div
      className={cn(
        'group relative flex items-start gap-2 px-3 py-[2px] font-mono text-[11.5px] leading-[1.55]',
        'hover:bg-secondary/40',
        line.stackTrace && 'border-l-2 border-destructive/40 bg-destructive/5 pl-[10px]',
      )}
      onClick={handleClick}
      role="listitem"
      data-line-id={line.id}
    >
      {ts != null && (
        <span className="shrink-0 select-none whitespace-nowrap text-[10.5px] tabular-nums text-meta-foreground/80">
          {formatLocalTime(ts)}
        </span>
      )}
      {showLevel && line.level && levelClass && (
        <span
          className={cn(
            'mt-px inline-flex shrink-0 select-none items-center rounded-sm px-1 py-[1px] text-[9.5px] font-medium uppercase tracking-[0.08em] ring-1 ring-inset',
            levelClass,
          )}
        >
          {line.level}
        </span>
      )}
      <span className="min-w-0 flex-1 whitespace-pre-wrap break-words text-foreground/90">
        {renderedSegments}
      </span>
      <button
        type="button"
        onClick={handleCopy}
        className={cn(
          'absolute right-1 top-1 inline-flex shrink-0 items-center justify-center rounded-sm border border-border-subtle bg-card p-0.5 opacity-0 transition-opacity',
          'hover:bg-secondary group-hover:opacity-100 focus-visible:opacity-100',
        )}
        aria-label="Copy line"
        title="Copy line"
      >
        {copied ? (
          <Check className="size-3 text-success" />
        ) : (
          <Copy className="size-3 text-meta-foreground" />
        )}
      </button>
    </div>
  );
}

export const LogLine = memo(LogLineImpl);
