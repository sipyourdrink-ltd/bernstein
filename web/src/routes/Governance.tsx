// Governance coverage panel (#5068, slice 1 of #5067).
//
// The question an operator arrives with is "what can this installation prove
// about what its agents did?". Every other governance console answers it with
// a single number that is green by construction. This one answers it with
// coverage, and it holds three rules that keep the answer honest:
//
//   * every number is a fraction over a denominator that is named on screen,
//     so "34%" can never mean "34% of something we did not say";
//   * a metric with no data is rendered as absent - the words "not measured"
//     plus the reason - never as a measured 0%. The two failures are
//     different and the screen must not blur them;
//   * no bar is coloured by its own value. There is no score, no grade and no
//     traffic light, because a colour that turns green is a claim the audit
//     chain has not made.
//
// This slice renders a checked-in fixture. Computing the fractions from the
// chain is #5067 slice 2; the "not covered" list and receipt verification are
// slices 3 and 4. The fixture is shaped exactly like the projection those
// slices will serve, so the panel does not change when the data becomes real.

import { Pill, SectionLabel } from '@/lib/states';
import fixture from './governance-coverage.fixture.json';

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

export type CoverageReport = {
  run_id: string;
  captured_at: string;
  /** Where these numbers came from. Shown so the panel never implies more than it has. */
  source_note?: string;
  metrics: CoverageMetric[];
};

export const coverageFixture: CoverageReport = fixture;

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
  const measuredCount = coverage.metrics.filter(isMeasured).length;
  return (
    <div className="space-y-6 p-6">
      <header className="space-y-2">
        <SectionLabel
          trailing={
            <Pill kind="ghost">
              {`${measuredCount} of ${coverage.metrics.length} measured`}
            </Pill>
          }
        >
          governance coverage
        </SectionLabel>
        <p className="max-w-3xl text-body text-muted-foreground">
          What this run can attribute, decide and recompute - as fractions over named
          populations. A metric with no evidence behind it is shown as not measured
          rather than as zero.
        </p>
        <p className="font-mono text-log text-meta-foreground">
          {`run ${coverage.run_id} · captured ${coverage.captured_at}`}
        </p>
        {coverage.source_note ? (
          <p className="text-body text-muted-foreground">{coverage.source_note}</p>
        ) : null}
      </header>

      <section className="rounded-md border border-border bg-card px-4 py-1">
        {coverage.metrics.map((metric) => (
          <MetricRow key={metric.id} metric={metric} />
        ))}
      </section>
    </div>
  );
}

export default function Governance() {
  return <GovernancePanel coverage={coverageFixture} />;
}
