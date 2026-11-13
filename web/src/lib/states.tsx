// State primitives - every screen-level component must use these (no rolling-your-own).
// Per Variant A handoff §8 + system-tokens-and-states ticket.

import type { MouseEventHandler, ReactNode } from 'react';
import { cn } from '@/lib/utils';

type Action = { label: string; onClick: MouseEventHandler<HTMLButtonElement>; variant?: 'primary' | 'secondary' };

interface EmptyStateProps {
  title: string;
  description?: string;
  action?: Action;
  icon?: ReactNode;
  className?: string;
}

export function EmptyState({ title, description, action, icon, className }: EmptyStateProps) {
  return (
    <div
      className={cn(
        'flex flex-col items-start justify-center gap-3 rounded-md border border-border-subtle bg-surface-raised/50 p-8',
        className,
      )}
    >
      {icon && <div className="text-muted-foreground">{icon}</div>}
      <div className="text-h3 text-foreground">{title}</div>
      {description && (
        <p className="max-w-md text-body text-muted-foreground">{description}</p>
      )}
      {action && (
        <button
          type="button"
          onClick={action.onClick}
          className={cn(
            'mt-1 rounded-md border px-3 py-1.5 text-body-md transition-colors',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background',
            (action.variant ?? 'primary') === 'primary'
              ? 'border-primary bg-primary text-primary-foreground hover:bg-primary/90'
              : 'border-border bg-card text-foreground hover:bg-secondary',
          )}
        >
          {action.label}
        </button>
      )}
    </div>
  );
}

interface LoadingStateProps {
  label?: string;
  rows?: number;
  className?: string;
}

/** Skeleton block - mirrors final layout. Buttons in loading state retain their text. */
export function LoadingState({ rows = 5, label, className }: LoadingStateProps) {
  return (
    <div className={cn('animate-fade-in', className)} aria-busy="true" aria-live="polite">
      {label && (
        <div className="mb-3 text-meta uppercase text-meta-foreground">{label}</div>
      )}
      <div className="space-y-2">
        {Array.from({ length: rows }).map((_, i) => (
          <div
            key={i}
            className="h-9 animate-pulse rounded-sm bg-muted/60"
            style={{ animationDelay: `${i * 60}ms` }}
          />
        ))}
      </div>
    </div>
  );
}

interface ErrorStateProps {
  title?: string;
  message: string;
  retry?: () => void;
  helpHref?: string;
  className?: string;
}

export function ErrorState({
  title = 'Something failed',
  message,
  retry,
  helpHref,
  className,
}: ErrorStateProps) {
  return (
    <div
      className={cn(
        'flex flex-col items-start gap-3 rounded-md border border-destructive/40 bg-destructive/5 p-6',
        className,
      )}
      role="alert"
    >
      <div className="text-h3 text-foreground">{title}</div>
      <p className="text-body text-muted-foreground">{message}</p>
      <div className="flex items-center gap-2">
        {retry && (
          <button
            type="button"
            onClick={retry}
            className={cn(
              'rounded-md border border-border bg-card px-3 py-1.5 text-body-md text-foreground hover:bg-secondary',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background',
            )}
          >
            Retry
          </button>
        )}
        {helpHref && (
          <a
            href={helpHref}
            target="_blank"
            rel="noreferrer"
            className={cn(
              'rounded-md px-3 py-1.5 text-body-md text-muted-foreground hover:text-foreground',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background',
            )}
          >
            Open docs
          </a>
        )}
      </div>
    </div>
  );
}

interface SectionLabelProps {
  children: ReactNode;
  trailing?: ReactNode;
  className?: string;
}

export function SectionLabel({ children, trailing, className }: SectionLabelProps) {
  return (
    <div
      className={cn(
        'flex items-center justify-between text-meta uppercase tracking-widest text-meta-foreground',
        className,
      )}
    >
      <span className="font-mono">{children}</span>
      {trailing}
    </div>
  );
}

interface StatusDotProps {
  kind: 'running' | 'queued' | 'stalled' | 'failed' | 'done' | 'merging' | 'idle';
  className?: string;
}

const STATUS_DOT_CLASSES: Record<StatusDotProps['kind'], string> = {
  running: 'bg-success ring-2 ring-success/30',
  queued: 'bg-meta-foreground',
  stalled: 'bg-warning',
  failed: 'bg-destructive',
  done: 'bg-foreground',
  merging: 'bg-accent',
  idle: 'bg-meta-foreground',
};

export function StatusDot({ kind, className }: StatusDotProps) {
  return (
    <span
      className={cn(
        'inline-block size-1.5 shrink-0 rounded-full',
        STATUS_DOT_CLASSES[kind],
        className,
      )}
      aria-label={kind}
    />
  );
}

interface PillProps {
  children: ReactNode;
  kind?: 'default' | 'accent' | 'success' | 'warning' | 'danger' | 'ghost';
  strong?: boolean;
  className?: string;
}

export function Pill({ children, kind = 'default', strong = false, className }: PillProps) {
  const base = 'inline-flex items-center gap-1 rounded-full border px-2 py-px font-mono text-[11px] tabular-nums';
  const palette: Record<NonNullable<PillProps['kind']>, string> = {
    default: 'bg-surface-raised text-muted-foreground border-border-subtle',
    accent: strong
      ? 'bg-accent text-accent-foreground border-accent'
      : 'bg-accent/15 text-accent border-accent/40',
    success: 'bg-success/15 text-success border-success/40',
    warning: 'bg-warning/15 text-warning border-warning/40',
    danger: 'bg-destructive/15 text-destructive border-destructive/40',
    ghost: 'bg-transparent text-muted-foreground border-border-subtle',
  };
  return <span className={cn(base, palette[kind], className)}>{children}</span>;
}
