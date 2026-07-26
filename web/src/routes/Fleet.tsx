// Fleet screen - multi-project supervisor overview.
//
// Talks to the `/api/v1/fleet/*` aggregator stub. When the backend has no
// FleetAggregator attached the stub returns `{projects: [], stub: true}`
// and we show an empty-state pointing operators at `bernstein fleet --web`.
//
// Cache-key strategy: every fleet-mode query is namespaced under
// `['fleet', ...]` and every single-project query under `['tasks', ...]`,
// `['agents', ...]`, etc. - toggling modes therefore never reuses a cached
// payload from the wrong scope.

import { useMemo, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router';
import { useQuery } from '@tanstack/react-query';
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  CircleDashed,
  Network,
  Search,
  XCircle,
} from 'lucide-react';

import { apiGet, ApiError } from '@/lib/api';
import { formatUSD, formatRelative } from '@/lib/format';
import {
  EmptyState,
  ErrorState,
  LoadingState,
  Pill,
  SectionLabel,
} from '@/lib/states';
import { cn } from '@/lib/utils';
import SteeringControls from '@/components/SteeringControls';

// ── Domain types ───────────────────────────────────────────────────────────
// Mirrors `ProjectSnapshot.to_dict()` from
// `src/bernstein/core/fleet/aggregator.py`.

type ProjectState =
  | 'initializing'
  | 'online'
  | 'degraded'
  | 'offline'
  | 'paused';

interface ProjectSnapshot {
  name: string;
  state: ProjectState | string;
  agents: number;
  active_agents_roles?: string[];
  pending_approvals: number;
  last_sha?: string;
  cost_usd: number;
  cost_history?: number[];
  last_event_ts?: number;
  last_error?: string;
  offline_since?: number | null;
  run_state?: string;
}

interface FleetProjectsResponse {
  projects: ProjectSnapshot[];
  errors?: { index: number; message: string }[];
  stub?: boolean;
  hint?: string;
}

interface FleetSearchResponse {
  query: string;
  free_text: string;
  filters: Record<string, string>;
  matches: { project: string; task_id?: string; title?: string }[];
  stub?: boolean;
}

// ── Slug helper ────────────────────────────────────────────────────────────
// Project drill-down route is `/tasks?project=<slug>`; we lowercase + dasherise
// because the slug is round-tripped through the URL bar.
function slugify(name: string): string {
  return name
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');
}

// ── Search-bar syntax parser ──────────────────────────────────────────────
// Backend mirrors this in `core/routes/fleet.py::fleet_search`; the
// frontend echoes the parsing so the operator can see filter chips locally
// even before the network hop returns.
export interface ParsedSearch {
  filters: Record<string, string>;
  freeText: string;
}

export function parseSearchQuery(q: string): ParsedSearch {
  const filters: Record<string, string> = {};
  const freeText: string[] = [];
  for (const token of q.trim().split(/\s+/)) {
    if (!token) continue;
    const idx = token.indexOf(':');
    if (idx > 0 && idx < token.length - 1) {
      const key = token.slice(0, idx).toLowerCase();
      const value = token.slice(idx + 1);
      if (key && value) {
        filters[key] = value;
        continue;
      }
    }
    freeText.push(token);
  }
  return { filters, freeText: freeText.join(' ') };
}

// ── Health icon ────────────────────────────────────────────────────────────
function HealthIcon({ state }: { state: ProjectState | string }) {
  const common = 'size-3.5 shrink-0';
  if (state === 'online') {
    return (
      <CheckCircle2 className={cn(common, 'text-success')} strokeWidth={1.5} />
    );
  }
  if (state === 'degraded') {
    return (
      <AlertTriangle className={cn(common, 'text-warning')} strokeWidth={1.5} />
    );
  }
  if (state === 'offline') {
    return <XCircle className={cn(common, 'text-destructive')} strokeWidth={1.5} />;
  }
  if (state === 'paused') {
    return (
      <CircleDashed className={cn(common, 'text-muted-foreground')} strokeWidth={1.5} />
    );
  }
  return (
    <CircleDashed className={cn(common, 'text-meta-foreground')} strokeWidth={1.5} />
  );
}

// ── Card ───────────────────────────────────────────────────────────────────
function ProjectCard({
  snap,
  onOpen,
}: {
  snap: ProjectSnapshot;
  onOpen: () => void;
}) {
  const roleSummary = (snap.active_agents_roles ?? []).slice(0, 3).join(', ');
  return (
    <button
      type="button"
      onClick={onOpen}
      className={cn(
        'group flex flex-col items-start gap-2.5 rounded-md border border-border bg-card p-4 text-left',
        'transition-colors hover:bg-secondary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background',
      )}
      aria-label={`Open ${snap.name}`}
    >
      <div className="flex w-full items-center gap-2">
        <HealthIcon state={snap.state} />
        <span className="flex-1 truncate text-[13px] font-semibold text-foreground">
          {snap.name}
        </span>
        <Pill kind={snap.state === 'online' ? 'success' : snap.state === 'offline' ? 'danger' : 'default'}>
          {String(snap.state)}
        </Pill>
      </div>

      <div className="grid w-full grid-cols-3 gap-2 pt-1 text-[11.5px]">
        <div className="flex flex-col">
          <span className="font-mono uppercase tracking-[0.12em] text-meta-foreground">
            agents
          </span>
          <span className="font-mono tabular-nums text-foreground">
            <Activity className="mr-1 inline size-3 align-[-2px]" strokeWidth={1.5} />
            {snap.agents}
          </span>
        </div>
        <div className="flex flex-col">
          <span className="font-mono uppercase tracking-[0.12em] text-meta-foreground">
            today
          </span>
          <span className="font-mono tabular-nums text-foreground">
            {formatUSD(snap.cost_usd)}
          </span>
        </div>
        <div className="flex flex-col">
          <span className="font-mono uppercase tracking-[0.12em] text-meta-foreground">
            approvals
          </span>
          <span className="font-mono tabular-nums text-foreground">
            {snap.pending_approvals}
          </span>
        </div>
      </div>

      {roleSummary && (
        <div className="truncate text-[11px] text-muted-foreground">
          {roleSummary}
        </div>
      )}
      {snap.run_state && (
        <div className="font-mono text-[10.5px] uppercase tracking-[0.12em] text-meta-foreground">
          {snap.run_state}
        </div>
      )}
      {snap.last_event_ts && (
        <div className="font-mono text-[10px] text-meta-foreground">
          last event {formatRelative(new Date(snap.last_event_ts * 1000).toISOString())}
        </div>
      )}
    </button>
  );
}

// ── Search-bar ─────────────────────────────────────────────────────────────
function CrossProjectSearch({
  value,
  onChange,
  parsed,
}: {
  value: string;
  onChange: (v: string) => void;
  parsed: ParsedSearch;
}) {
  const chips = Object.entries(parsed.filters);
  return (
    <div className="flex flex-col gap-2">
      <label
        htmlFor="fleet-search"
        className="font-mono text-meta uppercase tracking-widest text-meta-foreground"
      >
        Cross-project search
      </label>
      <div className="flex items-center gap-2 rounded-md border border-border bg-card px-3 py-2">
        <Search className="size-3 text-meta-foreground" strokeWidth={1.5} />
        <input
          id="fleet-search"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder="agent:claude status:running across:all"
          spellCheck={false}
          autoCorrect="off"
          autoCapitalize="off"
          className="flex-1 bg-transparent text-[13px] text-foreground placeholder:text-meta-foreground focus:outline-none"
        />
      </div>
      {(chips.length > 0 || parsed.freeText) && (
        <div className="flex flex-wrap items-center gap-1.5 text-[11px]">
          {chips.map(([k, v]) => (
            <Pill key={`${k}=${v}`} kind="accent">
              <span className="text-meta-foreground">{k}:</span>
              <span>{v}</span>
            </Pill>
          ))}
          {parsed.freeText && (
            <span className="font-mono text-meta-foreground">
              text · {parsed.freeText}
            </span>
          )}
        </div>
      )}
    </div>
  );
}

// ── Screen ─────────────────────────────────────────────────────────────────
export default function Fleet() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const [query, setQuery] = useState('');
  const [steerTaskId, setSteerTaskId] = useState('');
  const parsed = useMemo(() => parseSearchQuery(query), [query]);

  // React Query keys are namespaced by mode (see Tasks.tsx for the
  // single-project counterpart) - see the PR body for the cache-key
  // strategy diagram.
  const projectsQ = useQuery({
    queryKey: ['fleet', 'projects'],
    queryFn: () => apiGet<FleetProjectsResponse>('/fleet/projects'),
    refetchInterval: 5_000,
    refetchOnWindowFocus: true,
  });

  // Only hit the search endpoint once the operator typed something useful.
  const trimmedQuery = query.trim();
  const searchQ = useQuery({
    enabled: trimmedQuery.length > 0,
    queryKey: ['fleet', 'search', trimmedQuery],
    queryFn: () =>
      apiGet<FleetSearchResponse>(
        `/fleet/search?q=${encodeURIComponent(trimmedQuery)}`,
      ),
    staleTime: 30_000,
  });

  const openProject = (name: string) => {
    const slug = slugify(name);
    const fleetFlag = searchParams.get('fleet');
    const target = new URLSearchParams();
    target.set('project', slug);
    // Preserve the fleet flag so the operator can flip back to the fleet
    // overview via the topbar without losing context.
    if (fleetFlag) target.set('fleet', fleetFlag);
    navigate(`/tasks?${target.toString()}`);
  };

  const data = projectsQ.data;
  const projects = data?.projects ?? [];
  const stubbed = data?.stub ?? false;

  return (
    <div className="flex h-full flex-col gap-5 p-6">
      <div className="flex items-start justify-between gap-3">
        <div className="flex flex-col gap-1">
          <h1 className="text-h2 text-foreground">Fleet overview</h1>
          <p className="max-w-2xl text-body text-muted-foreground">
            Multi-project supervisor view. Each Bernstein installation is one
            card. Click a card to drill into per-project tasks; the topbar
            toggle returns you here.
          </p>
        </div>
        <div className="flex items-center gap-2 font-mono text-meta text-meta-foreground">
          <Network className="size-3" strokeWidth={1.5} />
          <span>fleet mode</span>
        </div>
      </div>

      <CrossProjectSearch value={query} onChange={setQuery} parsed={parsed} />

      <div className="flex flex-col gap-2">
        <SectionLabel>Steer a running worker</SectionLabel>
        <input
          value={steerTaskId}
          onChange={(e) => setSteerTaskId(e.target.value)}
          placeholder="task id to steer"
          className="w-full max-w-sm rounded-md border border-border bg-background p-2 font-mono text-[12px] text-foreground"
        />
        <SteeringControls taskId={steerTaskId} />
      </div>

      {trimmedQuery && searchQ.data && (
        <div className="rounded-md border border-border bg-card p-3 text-[12px] text-muted-foreground">
          <span className="font-mono text-meta-foreground">matches · </span>
          <span className="tabular-nums text-foreground">
            {searchQ.data.matches.length}
          </span>
          {searchQ.data.stub && (
            <span className="ml-2 text-meta-foreground">
              (backend search is stubbed - see PR for follow-up)
            </span>
          )}
        </div>
      )}

      <div className="flex-1">
        <SectionLabel
          trailing={
            data && (
              <span className="font-mono text-meta-foreground">
                {projects.length} project{projects.length === 1 ? '' : 's'}
                {stubbed && ' · stub'}
              </span>
            )
          }
        >
          Projects
        </SectionLabel>

        <div className="mt-3">
          {projectsQ.isLoading && <LoadingState rows={4} />}

          {projectsQ.isError && (
            <ErrorState
              title="Could not load fleet"
              message={
                projectsQ.error instanceof ApiError
                  ? `${projectsQ.error.status} - ${projectsQ.error.message}`
                  : (projectsQ.error as Error | null)?.message ?? 'unknown error'
              }
              retry={() => projectsQ.refetch()}
            />
          )}

          {!projectsQ.isLoading && !projectsQ.isError && projects.length === 0 && (
            <EmptyState
              title={stubbed ? 'No fleet aggregator attached' : 'No projects yet'}
              description={
                data?.hint ??
                'Register projects in your fleet config to populate the overview.'
              }
              icon={<Network className="size-5" strokeWidth={1.5} />}
              action={{
                label: 'Open Tasks',
                onClick: () => navigate('/tasks'),
                variant: 'secondary',
              }}
            />
          )}

          {projects.length > 0 && (
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
              {projects.map((snap) => (
                <ProjectCard
                  key={snap.name}
                  snap={snap}
                  onOpen={() => openProject(snap.name)}
                />
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
