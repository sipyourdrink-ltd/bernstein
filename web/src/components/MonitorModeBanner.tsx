import { Command } from 'lucide-react';
import { useGuiMeta } from '@/lib/guiMeta';

export const MONITOR_BANNER_TITLE = 'Monitor-only mode';
export const MONITOR_BANNER_DESCRIPTION =
  'No active task runner is attached to this server. Start a run from the CLI with';
export const MONITOR_COMMANDS = [
  'bernstein demo',
  'bernstein run',
  'bernstein -g "<goal>"',
] as const;

export function MonitorModeBanner({
  runnerActive,
}: {
  runnerActive?: boolean | null;
}) {
  const guiMeta = useGuiMeta(runnerActive === undefined);
  const active = runnerActive === undefined ? guiMeta.data?.runner_active : runnerActive;

  // Show only once the server confirms no runner is attached so loading states
  // and 401 auth prompts do not flicker or stack banners.
  if (active !== false) return null;

  return (
    <aside
      aria-label={MONITOR_BANNER_TITLE}
      className="shrink-0 border-b border-border bg-secondary/70 px-5 py-2.5"
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[12px] text-foreground">
        <span className="inline-flex items-center gap-1.5 font-medium">
          <Command className="size-3.5 text-muted-foreground" strokeWidth={1.5} />
          {MONITOR_BANNER_TITLE}
        </span>
        <span className="text-muted-foreground">
          {MONITOR_BANNER_DESCRIPTION}{' '}
          <code className="rounded border border-border-subtle bg-background px-1.5 py-0.5 font-mono text-[11px] text-foreground">
            {MONITOR_COMMANDS[0]}
          </code>
          ,{' '}
          <code className="rounded border border-border-subtle bg-background px-1.5 py-0.5 font-mono text-[11px] text-foreground">
            {MONITOR_COMMANDS[1]}
          </code>
          , or{' '}
          <code className="rounded border border-border-subtle bg-background px-1.5 py-0.5 font-mono text-[11px] text-foreground">
            {MONITOR_COMMANDS[2]}
          </code>
          .
        </span>
      </div>
    </aside>
  );
}
