// Governance coverage panel (#5068, slice 1 of #5067).
//
// The question an operator arrives with is "what can this installation prove
// about what its agents did?". A single summary number cannot answer it: it
// hides which population it was taken over and it reads the same whether a
// control was measured and failed or never measured at all. So the screen
// answers with coverage, under three rules:
//
//   * every number is a fraction over a denominator that is named on screen,
//     so "34%" can never mean "34% of something we did not say";
//   * a metric with no data is rendered as absent - the words "not measured"
//     plus the reason - never as a measured 0%. The two failures are
//     different and the screen must not blur them;
//   * no bar is coloured by its own value. There is no score, no grade and no
//     traffic light: a colour that turns green would assert a threshold this
//     screen has no evidence for.
//
// This slice renders a checked-in fixture. Computing the fractions from the
// chain is #5067 slice 2; the "not covered" list and receipt verification are
// slices 3 and 4. The fixture is shaped exactly like the projection those
// slices will serve, so the panel does not change when the data becomes real.
//
// The "not covered" list below (#5069, slice 3 of #5067) is data, not markup:
// a new gap is one entry in ``governance-not-covered.json``, not a new
// component. Each entry carries its own ``resolved`` flag rather than
// checking GitHub issue state live - the panel has no backend and no network
// call, so "self-maintaining" here means the whole maintenance action is
// flipping one boolean in the data file when the linked issue closes, not
// writing a live status check. Two of the six checked-in gaps (#5047, #5051)
// are already closed as of this list's writing, so ``resolved: true`` on
// those two is this property demonstrated with real data, not a hypothetical.

import { Pill, SectionLabel } from '@/lib/states';
import fixture from './governance-coverage.fixture.json';
import notCoveredFixture from './governance-not-covered.json';

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

export type CoverageGap = {
  id: string;
  /** Short, plain-language name of the gap (e.g. "tool calls"). */
  label: string;
  /** The consequence, in one line an operator understands without more context. */
  consequence: string;
  /** GitHub issue number that would close this gap, or ``null`` when the fix is a setting, not code. */
  issue: number | null;
  /** ``true`` once the linked issue has closed. The row stays, rendered differently, rather than vanishing. */
  resolved: boolean;
};

export const notCoveredFixtureGaps: CoverageGap[] = notCoveredFixture.gaps;

const ISSUE_BASE_URL = 'https://github.com/sipyourdrink-ltd/bernstein/issues/';

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

function GapRow({ gap }: { gap: CoverageGap }) {
  return (
    <div
      data-gap={gap.id}
      data-gap-state={gap.resolved ? 'resolved' : 'open'}
      className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 border-b border-border-subtle py-3 last:border-0"
    >
      <div className="min-w-0">
        <span
          className={
            gap.resolved
              ? 'text-body text-muted-foreground line-through'
              : 'text-body text-foreground'
          }
        >
          {gap.label}
        </span>
        <p className="mt-0.5 text-body text-muted-foreground">{gap.consequence}</p>
      </div>
      {gap.resolved ? (
        <Pill kind="ghost">resolved</Pill>
      ) : gap.issue !== null ? (
        <a
          href={`${ISSUE_BASE_URL}${gap.issue}`}
          target="_blank"
          rel="noreferrer"
          className="shrink-0 font-mono text-log text-meta-foreground underline decoration-dotted hover:text-foreground"
        >
          {`#${gap.issue}`}
        </a>
      ) : (
        <span className="shrink-0 font-mono text-log text-meta-foreground">settings</span>
      )}
    </div>
  );
}

/**
 * The "not covered" list: every gap this checked-in list knows about, each
 * one linked to what would close it. An empty list is rendered explicitly
 * ("nothing known to be uncovered") rather than as an absent section, which
 * would read the same as "nothing was checked" - a very different claim this
 * panel must never make by omission.
 */
function NotCoveredSection({ gaps }: { gaps: CoverageGap[] }) {
  const openCount = gaps.filter((gap) => !gap.resolved).length;
  return (
    <section>
      <SectionLabel
        trailing={<Pill kind="ghost">{`${openCount} open of ${gaps.length}`}</Pill>}
      >
        not covered in this run
      </SectionLabel>
      {gaps.length === 0 ? (
        <p
          data-testid="not-covered-empty"
          className="mt-2 rounded-md border border-dashed border-border-subtle p-4 text-body text-muted-foreground"
        >
          Nothing known to be uncovered - not the same claim as nothing having been checked.
        </p>
      ) : (
        <div className="mt-2 rounded-md border border-border bg-card px-4 py-1">
          {gaps.map((gap) => (
            <GapRow key={gap.id} gap={gap} />
          ))}
        </div>
      )}
    </section>
  );
}

export function GovernancePanel({
  coverage,
  notCoveredGaps = notCoveredFixtureGaps,
}: {
  coverage: CoverageReport;
  notCoveredGaps?: CoverageGap[];
}) {
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

      <NotCoveredSection gaps={notCoveredGaps} />
    </div>
  );
}

export default function Governance() {
  return <GovernancePanel coverage={coverageFixture} />;
}
