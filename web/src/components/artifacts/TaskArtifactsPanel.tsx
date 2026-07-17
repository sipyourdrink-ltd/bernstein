// Top-level orchestrator for the Artifacts tab inside the task drawer (#2553).
//
// Shows the chain-computed progress strip, then every agent-posted artifact
// grouped by key with its version history newest-first. Artifacts arrive live
// over SSE (``task.artifact`` / ``task.progress``) so a worker's freshly posted
// report appears without a manual reload. A version whose stored blob fails its
// hash check renders as tampered, never as content. The visual language mirrors
// the Gates and Diff panels so operators don't context-switch between tabs.

import { FileStack } from 'lucide-react';
import { useMemo } from 'react';

import { ErrorState } from '@/lib/states';
import { cn } from '@/lib/utils';

import { ArtifactCard } from './ArtifactCard';
import { ProgressStrip } from './ProgressStrip';
import type { ArtifactView } from './types';
import { useTaskArtifacts } from './useTaskArtifacts';

export interface TaskArtifactsPanelProps {
  taskId: string;
  /** Allows the parent to suspend polling when the tab isn't active. */
  active?: boolean;
  className?: string;
}

/** Group versions by key, newest version first within each key. */
function groupByKey(artifacts: ArtifactView[]): Array<[string, ArtifactView[]]> {
  const byKey = new Map<string, ArtifactView[]>();
  for (const a of artifacts) {
    const bucket = byKey.get(a.key) ?? [];
    bucket.push(a);
    byKey.set(a.key, bucket);
  }
  const groups: Array<[string, ArtifactView[]]> = [];
  for (const [key, versions] of byKey) {
    versions.sort((x, y) => y.version - x.version);
    groups.push([key, versions]);
  }
  // Most-recently-touched key first (highest journal index of any version).
  groups.sort((a, b) => Math.max(...b[1].map((v) => v.journal_index)) - Math.max(...a[1].map((v) => v.journal_index)));
  return groups;
}

export function TaskArtifactsPanel({ taskId, active = true, className }: TaskArtifactsPanelProps) {
  const { artifacts, progress, initialLoading, error, refetch } = useTaskArtifacts({
    taskId,
    enabled: active,
  });

  const groups = useMemo(() => groupByKey(artifacts), [artifacts]);

  if (error) {
    return <ErrorState message="Could not load task artifacts." retry={() => void refetch()} />;
  }

  return (
    <div className={cn('flex flex-col gap-3', className)}>
      <ProgressStrip progress={progress} />

      {initialLoading ? (
        <div className="rounded-md border border-border-subtle bg-card/60 px-4 py-6 text-center text-[12.5px] text-muted-foreground">
          Loading artifacts...
        </div>
      ) : groups.length === 0 ? (
        <div className="flex flex-col items-center gap-2 rounded-md border border-border-subtle bg-card/60 px-4 py-8 text-center">
          <FileStack className="h-5 w-5 text-meta-foreground" aria-hidden />
          <div className="text-[12.5px] text-muted-foreground">
            No artifacts posted yet. A worker attaches reports, tables, and preview links here as it works.
          </div>
        </div>
      ) : (
        <div className="flex flex-col gap-3">
          {groups.map(([key, versions]) => (
            <div key={key} className="flex flex-col gap-1.5">
              {versions.map((artifact) => (
                <ArtifactCard key={`${artifact.key}:${artifact.version}`} artifact={artifact} />
              ))}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
