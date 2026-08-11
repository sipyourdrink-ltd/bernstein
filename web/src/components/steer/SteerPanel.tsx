// Steer tab inside the task drawer (#1262).
//
// Composes the receipt-backed steering controls that already ship on the
// Fleet screen into the drawer for one task, so the operator can pause,
// guide, redirect, or abort the task they are looking at without switching
// screens. Terminal tasks render a disabled card with the reason instead of
// the live controls: there is no worker to steer.

import { ShieldCheck } from 'lucide-react';

import SteeringControls from '@/components/SteeringControls';
import { cn } from '@/lib/utils';

export interface SteerPanelProps {
  taskId: string;
  /** True when the task can no longer be steered (done, failed, cancelled). */
  terminal: boolean;
  /** Short label for the terminal state, shown as the disable reason. */
  /** Why steering is unavailable. Present exactly when ``terminal`` is true:
   *  the caller derives both from the task record, so a terminal task always
   *  carries a reason and a live one never needs a placeholder. */
  terminalLabel?: string;
  className?: string;
}

export function SteerPanel({ taskId, terminal, terminalLabel, className }: SteerPanelProps) {
  if (terminal) {
    return (
      <div
        className={cn(
          'flex flex-col gap-3 rounded-md border border-border-subtle bg-card p-3',
          className,
        )}
        role="status"
      >
        <div className="flex items-center gap-2 text-meta text-meta-foreground">
          <ShieldCheck className="size-3" strokeWidth={1.5} />
          <span className="font-mono">steer worker · {taskId}</span>
        </div>
        <div className="rounded-md border border-border-subtle bg-muted/30 px-3 py-2.5 text-[12px] text-muted-foreground">
          Steering is not available for this task: <span className="text-foreground">{terminalLabel}</span>.
          Pause, guidance, redirect, and abort apply only while the worker is
          running. Use Re-run to start it again.
        </div>
      </div>
    );
  }

  return <SteeringControls taskId={taskId} />;
}
