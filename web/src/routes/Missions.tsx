// Mission timeline screen (#2510) - the outcome-level view over a multi-day run.
//
// Task-level surfaces (tasks, fleet, approvals, costs, audit) are covered, but a
// multi-day mission had no timeline showing phases, gate verdicts, envelope burn,
// and evidence. This screen renders the deterministic mission projection served
// by `/api/v1/missions/*` (see `src/bernstein/core/routes/missions.py`):
//
//   * phase lanes with per-phase state, gate receipt, and envelope burn;
//   * every element links to the receipt / evidence bundle it was derived from -
//     no element renders without provenance;
//   * the `mission_status_hash` is shown so two operators can confirm they are
//     looking at the same state;
//   * when chain verification fails (`ledger_verified === false`) the screen
//     switches to an explicit unverified banner instead of best-effort
//     rendering.
//
// The projection is a pure fold over the mission's work-ledger chain: the server
// holds no mission-side state, so the same ledger bytes render the same screen
// from any host.

import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  AlertTriangle,
  CheckCircle2,
  CircleDashed,
  FileCheck2,
  Flag,
  ShieldAlert,
  Target,
} from 'lucide-react';

import {
  getMission,
  listMissions,
  missionEvidenceUrl,
  type MissionPhaseStatus,
  type MissionProjection,
} from '@/lib/api';
import { formatUSD } from '@/lib/format';
import { EmptyState, ErrorState, LoadingState, Pill, SectionLabel } from '@/lib/states';
import { cn } from '@/lib/utils';

// ── Phase-state visuals ──────────────────────────────────────────────────────

type PillKind = 'default' | 'accent' | 'success' | 'warning' | 'danger' | 'ghost';

const PHASE_PILL: Record<string, PillKind> = {
  passed: 'success',
  active: 'accent',
  pending: 'ghost',
  halted: 'danger',
  unverified: 'warning',
};

function phaseIcon(state: string) {
  switch (state) {
    case 'passed':
      return <CheckCircle2 className="h-4 w-4 text-[var(--success,#16a34a)]" aria-hidden />;
    case 'active':
      return <Target className="h-4 w-4 text-primary" aria-hidden />;
    case 'halted':
      return <Flag className="h-4 w-4 text-destructive" aria-hidden />;
    case 'unverified':
      return <ShieldAlert className="h-4 w-4 text-[var(--warning,#d97706)]" aria-hidden />;
    default:
      return <CircleDashed className="h-4 w-4 text-muted-foreground" aria-hidden />;
  }
}

function burnPct(phase: MissionPhaseStatus): number {
  if (!phase.budget_usd) return 0;
  return Math.min(100, Math.round((phase.spend_usd / phase.budget_usd) * 100));
}

// ── Provenance handle: a short hash that links to its origin ─────────────────

function shortHash(s: string, head = 10): string {
  if (!s) return '';
  const bare = s.startsWith('sha256:') ? s.slice(7) : s;
  return bare.length > head ? `${bare.slice(0, head)}...` : bare;
}

// ── Phase lane ───────────────────────────────────────────────────────────────

function PhaseLane({
  missionId,
  phase,
  index,
}: {
  missionId: string;
  phase: MissionPhaseStatus;
  index: number;
}) {
  const pct = burnPct(phase);
  return (
    <li className="relative flex gap-3">
      {/* Rail + node */}
      <div className="flex flex-col items-center">
        <span className="flex h-7 w-7 items-center justify-center rounded-full border border-border bg-card">
          {phaseIcon(phase.state)}
        </span>
        <span className="mt-1 w-px flex-1 bg-border-subtle" aria-hidden />
      </div>

      <div className="mb-4 flex-1 rounded-md border border-border-subtle bg-surface-raised/40 p-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <span className="font-mono text-[11px] text-meta-foreground">#{index + 1}</span>
            <span className="text-body-md text-foreground">{phase.name || phase.phase_id}</span>
            <Pill kind={PHASE_PILL[phase.state] ?? 'default'}>{phase.state}</Pill>
          </div>
          <div className="font-mono text-[11px] text-muted-foreground">
            {formatUSD(phase.spend_usd)} / {phase.budget_usd ? formatUSD(phase.budget_usd) : 'unlimited'}
          </div>
        </div>

        {/* Envelope burn */}
        <div className="mt-2">
          <div className="flex items-center justify-between text-[11px] text-meta-foreground">
            <span className="font-mono">{phase.envelope}</span>
            <span>{phase.budget_usd ? `${pct}%` : ''}</span>
          </div>
          <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-muted/60">
            <div
              className={cn(
                'h-full rounded-full',
                pct >= 100 ? 'bg-destructive' : pct >= 80 ? 'bg-[var(--warning,#d97706)]' : 'bg-primary',
              )}
              style={{ width: `${phase.budget_usd ? pct : 0}%` }}
            />
          </div>
        </div>

        {/* Provenance: gate receipt + evidence bundle links. No element without provenance. */}
        <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px]">
          <span className="text-meta-foreground">
            gate: {phase.gate_passed ? 'passed' : phase.state === 'halted' ? 'failed' : 'pending'}
            {phase.gate.length ? ` (${phase.gate.length} task${phase.gate.length === 1 ? '' : 's'})` : ''}
          </span>
          {phase.receipt_hash ? (
            <span className="font-mono text-muted-foreground" title={phase.receipt_hash}>
              receipt {shortHash(phase.receipt_hash)}
            </span>
          ) : (
            <span className="text-meta-foreground">no receipt yet</span>
          )}
          {phase.gate.map((taskId) => (
            <a
              key={taskId}
              href={missionEvidenceUrl(missionId, taskId)}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 text-primary hover:underline"
              title={`Evidence bundle for ${taskId}`}
            >
              <FileCheck2 className="h-3 w-3" aria-hidden />
              {taskId}
            </a>
          ))}
        </div>
      </div>
    </li>
  );
}

// ── Verification banner ──────────────────────────────────────────────────────

function UnverifiedBanner({ projection }: { projection: MissionProjection }) {
  const chainTorn = !projection.ledger_verified;
  return (
    <div
      role="alert"
      className="flex items-start gap-3 rounded-md border border-destructive/50 bg-destructive/10 p-4"
    >
      <ShieldAlert className="mt-0.5 h-5 w-5 text-destructive" aria-hidden />
      <div>
        <div className="text-body-md text-foreground">Unverified state - not safe to trust</div>
        <p className="mt-1 text-body text-muted-foreground">
          {chainTorn
            ? 'The mission work-ledger chain no longer recomputes: a ledger entry was tampered with. '
            : 'A referenced evidence bundle no longer matches the hash its phase receipt bound. '}
          The timeline below is rendered from the projection, but every claim is unverified until the
          chain and evidence verify again.
        </p>
      </div>
    </div>
  );
}

// ── Mission detail (timeline) ────────────────────────────────────────────────

function MissionDetail({ missionId }: { missionId: string }) {
  const q = useQuery({
    queryKey: ['missions', 'projection', missionId],
    queryFn: () => getMission(missionId),
    refetchInterval: 15_000,
  });

  if (q.isLoading) return <LoadingState rows={4} label="Projecting mission from the ledger" />;
  if (q.isError) {
    return (
      <ErrorState
        title="Could not project mission"
        message={(q.error as Error).message}
        retry={() => void q.refetch()}
      />
    );
  }

  const projection = q.data!;
  const status = projection.status;
  const unverified = !projection.ledger_verified || !projection.evidence_verified || status.overall === 'unverified';

  return (
    <div className="space-y-4">
      {unverified && <UnverifiedBanner projection={projection} />}

      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <h2 className="text-h3 text-foreground">{status.mission_id}</h2>
          <Pill kind={unverified ? 'warning' : status.overall === 'complete' ? 'success' : 'accent'} strong>
            {status.overall}
          </Pill>
        </div>
        <div className="flex items-center gap-2 text-[11px] text-meta-foreground">
          {unverified ? (
            <AlertTriangle className="h-3.5 w-3.5 text-destructive" aria-hidden />
          ) : (
            <CheckCircle2 className="h-3.5 w-3.5 text-[var(--success,#16a34a)]" aria-hidden />
          )}
          <span>{projection.entry_count} ledger entries</span>
        </div>
      </div>

      {/* The status hash two operators must agree on. */}
      <div className="rounded-md border border-border-subtle bg-card px-3 py-2">
        <div className="text-meta uppercase text-meta-foreground">mission_status_hash</div>
        <div className="mt-0.5 select-all break-all font-mono text-[12px] text-foreground">
          {projection.mission_status_hash}
        </div>
      </div>

      <div>
        <SectionLabel>Phase timeline</SectionLabel>
        <ol className="mt-3">
          {status.phases.map((phase, i) => (
            <PhaseLane key={phase.phase_id} missionId={missionId} phase={phase} index={i} />
          ))}
        </ol>
        {status.phases.length === 0 && (
          <p className="text-body text-muted-foreground">This mission declares no phases yet.</p>
        )}
      </div>
    </div>
  );
}

// ── Screen ───────────────────────────────────────────────────────────────────

export default function Missions() {
  const listQ = useQuery({
    queryKey: ['missions', 'list'],
    queryFn: listMissions,
    refetchInterval: 30_000,
  });

  const missions = useMemo(() => listQ.data ?? [], [listQ.data]);
  const [selected, setSelected] = useState<string | null>(null);
  const active = selected ?? missions[0] ?? null;

  return (
    <div className="p-4 md:p-6">
      <div className="mb-4">
        <h1 className="text-h2 text-foreground">Missions</h1>
        <p className="mt-1 text-body text-muted-foreground">
          Outcome-level timeline for multi-day runs. Every phase, gate verdict, and dollar of envelope burn is a
          projection of the mission ledger - the same verified state everyone who opens this screen sees.
        </p>
      </div>

      {listQ.isLoading ? (
        <LoadingState rows={6} label="Loading missions" />
      ) : listQ.isError ? (
        <ErrorState message={(listQ.error as Error).message} retry={() => void listQ.refetch()} />
      ) : missions.length === 0 ? (
        <EmptyState
          icon={<Target className="h-6 w-6" />}
          title="No missions yet"
          description="Define a multi-day mission with `bernstein mission define <spec.json>`. Its timeline appears here once the ledger has a mission.defined transition."
        />
      ) : (
        <div className="grid gap-6 lg:grid-cols-[240px_1fr]">
          {/* Mission list */}
          <nav aria-label="Missions" className="space-y-1">
            {missions.map((id) => (
              <button
                key={id}
                type="button"
                onClick={() => setSelected(id)}
                className={cn(
                  'flex w-full items-center gap-2 rounded-md border px-3 py-2 text-left text-body-md transition-colors',
                  id === active
                    ? 'border-primary bg-primary/10 text-foreground'
                    : 'border-border-subtle bg-card text-muted-foreground hover:bg-secondary',
                )}
              >
                <Flag className="h-3.5 w-3.5 shrink-0" aria-hidden />
                <span className="truncate font-mono text-[12px]">{id}</span>
              </button>
            ))}
          </nav>

          {/* Timeline */}
          <section>{active && <MissionDetail missionId={active} />}</section>
        </div>
      )}
    </div>
  );
}
