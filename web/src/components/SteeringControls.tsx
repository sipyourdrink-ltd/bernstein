// Receipt-backed steering controls for a single running worker (#2508).
//
// Pause, resume, guidance, redirect, or abort a worker mid-task. Every action
// is a receipt first and an effect second: the server binds the command into
// the audit chain before any effect runs and returns the receipt hash the
// delivered effect references. The "queued" indicator below the action bar is
// that returned receipt - proof the intervention was recorded, not just sent.

import { useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import { CircleSlash, Compass, MessageSquare, Pause, Play, ShieldCheck } from 'lucide-react';

import { steerTask, ApiError } from '@/lib/api';
import type { SteerCommand, SteerKind, SteerReceipt } from '@/lib/api';
import { cn } from '@/lib/utils';

const KIND_META: Record<SteerKind, { label: string; icon: typeof Pause }> = {
  pause: { label: 'Pause', icon: Pause },
  resume: { label: 'Resume', icon: Play },
  guidance: { label: 'Guidance', icon: MessageSquare },
  redirect: { label: 'Redirect', icon: Compass },
  abort: { label: 'Abort', icon: CircleSlash },
};

const KIND_ORDER: SteerKind[] = ['pause', 'resume', 'guidance', 'redirect', 'abort'];

interface SteeringControlsProps {
  taskId: string;
}

export default function SteeringControls({ taskId }: SteeringControlsProps) {
  const [kind, setKind] = useState<SteerKind>('guidance');
  const [guidance, setGuidance] = useState('');
  const [redirectTarget, setRedirectTarget] = useState('');
  const [sessionId, setSessionId] = useState('');
  const [reason, setReason] = useState('');
  const [receipt, setReceipt] = useState<SteerReceipt | null>(null);

  const mutation = useMutation({
    mutationFn: (command: SteerCommand) => steerTask(taskId, command),
    onSuccess: (r) => setReceipt(r),
  });

  const needsGuidance = kind === 'guidance';
  const needsRedirect = kind === 'redirect';
  const needsSession = kind === 'pause' || kind === 'abort';

  const submit = () => {
    const command: SteerCommand = { kind };
    if (needsGuidance) command.guidance = guidance;
    if (needsRedirect) command.redirect_target = redirectTarget;
    if (needsSession) command.session_id = sessionId;
    if (reason) command.reason = reason;
    mutation.mutate(command);
  };

  const disabled =
    mutation.isPending ||
    !taskId ||
    (needsGuidance && !guidance.trim()) ||
    (needsRedirect && !redirectTarget.trim()) ||
    (needsSession && !sessionId.trim());

  return (
    <div className="flex flex-col gap-3 rounded-md border border-border bg-card p-3">
      <div className="flex items-center gap-2 text-meta text-meta-foreground">
        <ShieldCheck className="size-3" strokeWidth={1.5} />
        <span className="font-mono">steer worker · {taskId || 'no task selected'}</span>
      </div>

      <div className="flex flex-wrap gap-1.5">
        {KIND_ORDER.map((k) => {
          const Meta = KIND_META[k];
          const Icon = Meta.icon;
          return (
            <button
              key={k}
              type="button"
              onClick={() => setKind(k)}
              className={cn(
                'flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-[12px]',
                kind === k
                  ? 'border-foreground bg-foreground text-background'
                  : 'border-border text-muted-foreground hover:text-foreground',
              )}
            >
              <Icon className="size-3" strokeWidth={1.5} />
              {Meta.label}
            </button>
          );
        })}
      </div>

      {needsGuidance && (
        <textarea
          value={guidance}
          onChange={(e) => setGuidance(e.target.value)}
          placeholder="stop refactoring, focus on the failing test"
          rows={2}
          className="w-full rounded-md border border-border bg-background p-2 text-[12px] text-foreground"
        />
      )}
      {needsRedirect && (
        <input
          value={redirectTarget}
          onChange={(e) => setRedirectTarget(e.target.value)}
          placeholder="new objective for this worker"
          className="w-full rounded-md border border-border bg-background p-2 text-[12px] text-foreground"
        />
      )}
      {needsSession && (
        <input
          value={sessionId}
          onChange={(e) => setSessionId(e.target.value)}
          placeholder="worker session id"
          className="w-full rounded-md border border-border bg-background p-2 text-[12px] text-foreground"
        />
      )}
      <input
        value={reason}
        onChange={(e) => setReason(e.target.value)}
        placeholder="reason (optional)"
        className="w-full rounded-md border border-border bg-background p-2 text-[12px] text-foreground"
      />

      <div className="flex items-center justify-between gap-2">
        <button
          type="button"
          onClick={submit}
          disabled={disabled}
          className={cn(
            'rounded-md px-3 py-1.5 text-[12px] font-medium',
            disabled
              ? 'cursor-not-allowed bg-muted text-muted-foreground'
              : 'bg-foreground text-background hover:opacity-90',
          )}
        >
          {mutation.isPending ? 'Recording receipt...' : `Send ${KIND_META[kind].label.toLowerCase()}`}
        </button>

        {mutation.isError && (
          <span className="text-[12px] text-red-500">
            {mutation.error instanceof ApiError
              ? `${mutation.error.status} - rejected`
              : 'failed'}
          </span>
        )}
      </div>

      {receipt && (
        <div className="rounded-md border border-border bg-background p-2 text-[11px] text-muted-foreground">
          <span className="font-mono text-meta-foreground">queued · </span>
          <span className="text-foreground">{receipt.kind}</span>
          <span className="ml-2 font-mono">seq {receipt.mailbox_seq}</span>
          <span className="ml-2 font-mono" title={receipt.receipt_hash}>
            receipt {receipt.receipt_hash.slice(0, 18)}
          </span>
        </div>
      )}
    </div>
  );
}
