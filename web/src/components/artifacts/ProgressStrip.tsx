// Progress strip for the Artifacts tab (#2553).
//
// Renders the chain-computed progress vector: a projection of journaled work
// (checkpoints, diffs, gates, evidence, ledger phase), never a self-reported
// number. The strip shows the earned-steps count and the phase, plus the
// vector hash so an operator can eyeball that two reads agree. There is no bar
// a worker can fill by asserting - the number moves only when real work lands.

import { cn } from '@/lib/utils';

import type { ProgressVector } from './types';

export interface ProgressStripProps {
  progress: ProgressVector | null;
  className?: string;
}

function Stat({ label, value }: { label: string; value: number | string }) {
  return (
    <div className="flex flex-col items-start">
      <span className="font-mono text-[15px] font-semibold tabular-nums text-foreground">{value}</span>
      <span className="text-[10px] uppercase tracking-[0.12em] text-meta-foreground">{label}</span>
    </div>
  );
}

export function ProgressStrip({ progress, className }: ProgressStripProps) {
  if (progress === null) {
    return (
      <div
        className={cn(
          'rounded-md border border-border-subtle bg-card/60 px-3 py-2 text-[12px] text-muted-foreground',
          className,
        )}
      >
        No journaled work yet - progress is earned, not posted.
      </div>
    );
  }

  const phase = progress.ledger_phase || 'pending';
  return (
    <div
      className={cn(
        'flex flex-wrap items-center gap-x-6 gap-y-2 rounded-md border border-border bg-card px-3 py-2.5',
        className,
      )}
      title={`Chain-computed progress. Vector hash ${progress.vector_hash.slice(0, 16)}...`}
    >
      <Stat label="steps earned" value={progress.earned_steps} />
      <Stat label="checkpoints" value={progress.checkpoints} />
      <Stat label="diffs" value={progress.diffs_captured} />
      <Stat label="gates" value={progress.gate_attempts} />
      <Stat label="evidence" value={`${progress.evidence_passed}/${progress.evidence_declared}`} />
      <div className="flex flex-col items-start">
        <span
          className={cn(
            'rounded px-1.5 py-0.5 font-mono text-[11px] font-medium',
            progress.terminal ? 'bg-secondary text-foreground' : 'bg-secondary/60 text-muted-foreground',
          )}
        >
          {phase}
        </span>
        <span className="text-[10px] uppercase tracking-[0.12em] text-meta-foreground">phase</span>
      </div>
      <span className="ml-auto font-mono text-[10px] text-meta-foreground" aria-label="progress vector hash">
        {progress.vector_hash.slice(0, 12)}
      </span>
    </div>
  );
}
