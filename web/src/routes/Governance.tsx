import { useQuery } from '@tanstack/react-query';
import { type FormEvent } from 'react';
import { useSearchParams } from 'react-router';
import { apiGet } from '@/lib/api';
import { EmptyState, ErrorState, LoadingState } from '@/lib/states';
import { ReceiptVerification } from '@/components/governance/ReceiptVerification';

export type CoverageMetric = {
  id: string;
  label: string;
  /** Names what the denominator counts, so the fraction cannot be read loosely. */
  denominator_label: string;
  /** ``null`` when the numerator could not be counted at all. */
  covered: number | null;
  /** ``null`` when the population was never recorded. */
  total: number | null;
  /** Why the metric has no fraction. Rendered instead of a bar. */
  unmeasured_reason: string | null;
};

type ChainStatus = 'ok' | 'no_entries' | 'seal_only' | 'tampered';

type MetricCounts = { covered: number; total: number };

type CoverageDocument = {
  v: number;
  run_id: string;
  chain_status: ChainStatus;
  recorded_actions: number;
  recorded_decisions: number;
  metrics: {
    attributable_action_ratio: MetricCounts;
    decision_coverage: MetricCounts;
  };
};

type CoverageReport = {
  run_id: string;
  chain_status?: Exclude<ChainStatus, 'tampered'>;
  recorded_actions?: number;
  recorded_decisions?: number;
  metrics: CoverageMetric[];
};

const CHAIN_LABELS = {
  ok: 'Run chain verified',
  no_entries: 'No run chain entries',
  seal_only: 'Run chain contains only a journal seal',
};

const METRIC_LABELS = [
  ['attributable_action_ratio', 'Actions attributable to a principal'],
  ['decision_coverage', 'Actions covered by an allow decision'],
] as const;

function projectCoverage(document: CoverageDocument & { chain_status: Exclude<ChainStatus, 'tampered'> }): CoverageReport {
  return {
    run_id: document.run_id,
    chain_status: document.chain_status,
    recorded_actions: document.recorded_actions,
    recorded_decisions: document.recorded_decisions,
    metrics: METRIC_LABELS.map(([id, label]) => {
      const counts = document.metrics[id];
      return {
        id,
        label,
        denominator_label: 'agent actions recorded in this run',
        ...counts,
        unmeasured_reason: counts.total === 0 ? 'This run has no recorded agent actions.' : null,
      };
    }),
  };
}

type MeasuredMetric = CoverageMetric & { covered: number; total: number };

/**
 * A metric is measured only when both sides of the fraction exist and the
 * population is non-empty. An empty population is not 0% coverage - there is
 * nothing to cover - so it is reported as absent like any other missing datum.
 */
function isMeasured(metric: CoverageMetric): metric is MeasuredMetric {
  return (
    typeof metric.covered === 'number' &&
    typeof metric.total === 'number' &&
    metric.total > 0
  );
}

/**
 * Format the fraction as a percentage that never overstates or understates a
 * partial result: only ``covered === total`` prints 100%, and only an empty
 * numerator prints 0%. Everything between is floored, so a fraction that is
 * nearly complete still reads as incomplete.
 */
export function percentLabel(covered: number, total: number): string {
  if (covered >= total) return '100%';
  if (covered <= 0) return '0%';
  const raw = (covered / total) * 100;
  if (raw < 1) return '<1%';
  return `${Math.floor(raw)}%`;
}

function barWidth(covered: number, total: number): string {
  const raw = (covered / total) * 100;
  // Keep a hairline visible for a non-empty numerator so a tiny fraction is
  // not indistinguishable from nothing at all.
  return `${Math.max(covered > 0 ? 1 : 0, Math.min(100, raw))}%`;
}

// One fill class for every bar, whatever the value. Kept as a module constant
// so a future edit cannot quietly reintroduce value-dependent colour.
const BAR_FILL_CLASS = 'h-full rounded-full bg-foreground';

function MetricRow({ metric }: { metric: CoverageMetric }) {
  const measured = isMeasured(metric);
  return (
    <div
      data-metric={metric.id}
      data-metric-state={measured ? 'measured' : 'not-measured'}
      className="border-b border-border-subtle py-3 last:border-0"
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span className="text-body text-foreground">{metric.label}</span>
        <span className="font-mono text-body-md tabular-nums text-foreground">
          {measured ? percentLabel(metric.covered, metric.total) : 'not measured'}
        </span>
      </div>

      {measured ? (
        <>
          <div
            data-metric-bar="track"
            className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-surface-raised"
          >
            <div
              data-metric-bar="fill"
              className={BAR_FILL_CLASS}
              style={{ width: barWidth(metric.covered, metric.total) }}
            />
          </div>
          <p className="mt-1.5 font-mono text-log text-meta-foreground">
            {`${metric.covered} / ${metric.total} ${metric.denominator_label}`}
          </p>
        </>
      ) : (
        <>
          <div className="mt-2 h-1.5 w-full rounded-full border border-dashed border-border-subtle" />
          <p className="mt-1.5 font-mono text-log text-meta-foreground">
            {`no fraction over ${metric.denominator_label}`}
          </p>
          {metric.unmeasured_reason ? (
            <p className="mt-1 text-body text-muted-foreground">{metric.unmeasured_reason}</p>
          ) : null}
        </>
      )}
    </div>
  );
}

export function GovernancePanel({ coverage }: { coverage: CoverageReport }) {
  return (
    <div className="space-y-3">
      {coverage.chain_status && (
        <div className="space-y-1" role="status">
          <p className="text-body-md text-foreground">{CHAIN_LABELS[coverage.chain_status]}</p>
          <p className="break-all font-mono text-log text-meta-foreground">Run {coverage.run_id}</p>
          <p className="font-mono text-log text-meta-foreground">
            {coverage.recorded_actions} recorded actions · {coverage.recorded_decisions} recorded decisions
          </p>
        </div>
      )}
      <section aria-label="Coverage by metric" className="rounded-md border border-border bg-card px-4 py-1">
        {coverage.metrics.map((metric) => <MetricRow key={metric.id} metric={metric} />)}
      </section>
    </div>
  );
}

export default function Governance() {
  const [params, setParams] = useSearchParams();
  const runId = params.get('run_id')?.trim() ?? '';
  const coverage = useQuery({
    queryKey: ['governance', 'coverage', runId],
    queryFn: async () => {
      const data = await apiGet<CoverageDocument>(`/governance/coverage?${new URLSearchParams({ run_id: runId })}`);
      if (data?.v !== 1 || data.run_id !== runId) throw new Error('Coverage response did not identify the requested run.');
      return data;
    },
    enabled: Boolean(runId),
  });

  function selectRun(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const id = String(new FormData(event.currentTarget).get('run_id') ?? '').trim();
    const next = new URLSearchParams(params);
    if (id) next.set('run_id', id);
    else next.delete('run_id');
    setParams(next);
  }

  let content;
  if (!runId) {
    content = <EmptyState title="Select a run" description="Enter a run ID to load its governance coverage." />;
  } else if (coverage.isPending) {
    content = <LoadingState label="Loading run coverage" rows={2} />;
  } else if (coverage.isError) {
    content = <ErrorState title="Could not load coverage" message={coverage.error.message} retry={() => coverage.refetch()} />;
  } else if (coverage.data.chain_status === 'tampered') {
    content = <ErrorState title="Run chain verification failed" message="The run chain did not verify. Coverage is unavailable for this run." />;
  } else {
    content = <GovernancePanel coverage={projectCoverage({ ...coverage.data, chain_status: coverage.data.chain_status })} />;
  }

  return (
    <div className="space-y-5 p-6">
      <h1 className="text-h2 text-foreground">governance coverage</h1>
      <form onSubmit={selectRun} className="flex flex-wrap items-end gap-2">
        <label className="block min-w-0 space-y-1 text-body-md">
          <span>Run ID</span>
          <input
            key={runId}
            name="run_id"
            defaultValue={runId}
            className="block w-80 max-w-full rounded-md border border-meta-foreground bg-background px-3 py-2 font-mono text-body text-foreground focus-visible:ring-2 focus-visible:ring-ring"
          />
        </label>
        <button type="submit" className="rounded-md border border-meta-foreground bg-card px-3 py-2 text-body-md text-foreground hover:bg-secondary focus-visible:ring-2 focus-visible:ring-ring">
          Load coverage
        </button>
      </form>
      {content}
      <ReceiptVerification />
    </div>
  );
}
