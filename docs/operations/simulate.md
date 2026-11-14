# Simulate (digital-twin dry-run)

Audience: operators about to launch an expensive plan who want a cost
and risk forecast before any real agent spawns.

## Overview

`bernstein simulate <plan.yaml>` dry-runs a full multi-agent cycle on
synthetic data with mock LLMs. It never spawns a real agent and never
hits the network. The runner reads `.sdd/traces/` and `.sdd/metrics/`
for calibration; with no historical data it falls back to documented
cold-start priors.

Output:

- Per-task cost band (p50 / p90)
- Per-task latency band (p50 / p90)
- Abandonment probability per (role, adapter)
- Blast-radius score (production scorer, identical to `bernstein run`)
- Aggregate critical-path wall-clock, expected abandonments, max blast radius
- Bottleneck identification (fan-out, high abandon, high blast, long latency)
- Criterion-profile bias chart (speed / cost / quality / safety mix)
- Mermaid decision-flow graph
- Optional `--budget-cap` enforcement with non-zero exit on breach

Source: `src/bernstein/cli/commands/simulate_cmd.py` and
`src/bernstein/core/simulate/`.

## CLI

```text
bernstein simulate PLAN
  [--from-traces N]      max history samples per (role, adapter); default 50
  [--seed S]             RNG seed; identical seed + plan + traces -> byte-identical report; default 42
  [--budget-cap USD]     non-zero exit when predicted p90 spend exceeds this
  [--out FILE]           write report.json or report.md sidecar
  [--metrics-dir DIR]    default .sdd/metrics if present, else cold-start
  [--traces-dir DIR]     default .sdd/traces if present, else cold-start
  [--format md|json]     stdout format; sidecar follows --out extension
```

## How historical samples are used

`--from-traces` does not cap the same population for every estimate:

- **Cost** uses recorded cost samples from `.sdd/metrics/cost.jsonl`, filtered
  by role and adapter. The limit keeps at most that many samples for each pair.
- **Latency** uses valid, non-negative duration samples from the `latency_seconds`
  or `duration_s` fields in trace rows, independently
  for each role and adapter. The loader visits `*.jsonl` files in filename
  order and rows in each file in order; after collecting the duration samples,
  it keeps their tail up to the requested limit. Rows without a usable duration
  do not contribute a latency sample.
- **Abandonment** uses every accepted trace row for a role and adapter pair,
  including rows with no usable duration. The limit does not truncate this
  population, so changing it does not change the observed abandonment rate.

For example, suppose one trace file contains these rows in order, all for
`backend` / `mock`:

```json
{"role":"backend","adapter":"mock","status":"completed","latency_seconds":10}
{"role":"backend","adapter":"mock","status":"abandoned"}
{"role":"backend","adapter":"mock","status":"completed","latency_seconds":30}
```

With `--from-traces 1`, the latency sample is `[30]`; with
`--from-traces 2`, it is `[10, 30]`. In both cases the abandonment rate is
`1/3`, because all three trace rows count, including the abandoned row without
a duration.

Determinism: same seed + plan + trace history yields a byte-identical
report. CI can lock the seed and diff reports against a stored fixture.

## Examples

Smoke-test before a real run:

```bash
bernstein simulate plan.yaml --budget-cap 25.00
```

Lock historical sampling and emit a JSON artefact for CI:

```bash
bernstein simulate plan.yaml \
  --from-traces 200 \
  --seed 17 \
  --out .sdd/audit/simulate-$(date +%Y%m%d).json \
  --format json
```

Force cold-start (ignore local history) by pointing at an empty dir:

```bash
mkdir -p /tmp/cold && bernstein simulate plan.yaml \
  --traces-dir /tmp/cold --metrics-dir /tmp/cold
```

## Exit codes

| Code | Meaning |
|------|---------|
| 0    | Report produced, no budget-cap breach |
| 2    | `SimulationError` (plan failed to load / parse) |
| 3    | `--budget-cap` breached by predicted p90 spend |

## Config keys

| Key | Default | Effect |
|-----|---------|--------|
| `--metrics-dir` | `./.sdd/metrics` | Source for calibration |
| `--traces-dir` | `./.sdd/traces` | Source for historical sampling |
| `BERNSTEIN_TRACES_DIR` | unset | Honoured by `bernstein compare`; `simulate` uses `--traces-dir` |

## Troubleshooting

**Predicted p90 looks too pessimistic.** Cold-start priors are
conservative on purpose. Run a small real workload first so
`.sdd/traces/` and `.sdd/metrics/` get populated, then re-simulate. The
calibrated band tightens as trace history grows.

**`simulate` exit code 3 in CI but the plan was supposed to fit.** The
predicted p90 includes retry budget consumption. Either raise
`--budget-cap`, tighten `--retry-budget` on the real run, or drop
high-blast-radius tasks from the plan.
