# Calibration (Brier score + reliability report)

Audience: operators wanting to know whether the router's / judge's
probability outputs are calibrated against real outcomes.

## Overview

The calibration log records every probability output the router or
judge produced alongside the eventual binary outcome. From that log
Bernstein computes:

- **Brier score** - mean squared error between predicted probability
  and the actual outcome; lower is better, range `[0, 1]`.
- **Expected calibration error (ECE)** - bucketed gap between predicted
  probability and observed frequency.
- **Reliability diagram data** - per-bucket counts, predicted mean,
  observed rate.

Source: `src/bernstein/eval/calibration.py`,
`src/bernstein/cli/commands/eval_benchmark_cmd.py` (`eval calibration` group).

Log path: `.sdd/metrics/calibration.jsonl` (configurable).

## Logging

Decisions are logged by the routing layer through
`bernstein.eval.calibration.log_decision()`. Each record carries:

| Field | Use |
|-------|-----|
| `ts` | Timestamp in epoch seconds |
| `decision_kind` | Decision category, e.g. `model_route` or `judge_pass` |
| `policy_path` | Identifier of the policy that produced the probability |
| `predicted_prob` | Predicted probability in `[0, 1]` |
| `observed_outcome` | Eventual outcome, encoded as a JSON boolean: `true` or `false` |
| `decision_id` | Optional identifier for correlating log records |
| `metadata` | Optional auxiliary JSON object |

A row is written once its outcome is known; `observed_outcome` is not a
pending `null` value. For example, this is a valid JSONL record:

```json
{"ts": 1710000000.0, "decision_kind": "model_route", "policy_path": "router/v2", "predicted_prob": 0.8, "observed_outcome": true, "decision_id": "d-123", "metadata": {"slice": "router"}}
```

The log is append-only JSONL. Blank lines are ignored. An invalid nonblank
row aborts parsing before time or kind filters can discard it. For example,
a non-boolean `observed_outcome` raises `CalibrationLogError`.

## CLI

```text
bernstein eval calibration report
  [--since DURATION]       e.g. "30m", "24h", "7d"
  [--kind DECISION_KIND]   e.g. model_route
  [--log-path PATH]        override .sdd/metrics/calibration.jsonl
  [--bins N]               reliability buckets; default 10
  [--output FILE]          write JSON to file (stdout if omitted)
```

The command emits a JSON report. A missing log or a valid log with no
rows matching the filters returns `{"decisions": 0, "brier": null, ...}`.
This valid empty report is distinct from a malformed row: an invalid row
aborts parsing and fails the report, even when a time or kind filter would
otherwise discard that row.

## Examples

Weekly Brier check from cron:

```bash
bernstein eval calibration report --since 7d \
  --output .sdd/audit/calibration-$(date +%Y%m%d).json
```

Drill into model routing only:

```bash
bernstein eval calibration report --since 24h --kind model_route
```

Adjust bucket resolution for a sharper reliability diagram:

```bash
bernstein eval calibration report --since 30d --bins 20
```

## Reading the report

| Brier | Interpretation |
|-------|----------------|
| `< 0.05` | Calibrated; predicted probabilities track outcomes closely |
| `0.05 - 0.15` | Acceptable; tune retry / cascade thresholds at the margin |
| `> 0.15` | Miscalibrated; router scoring needs re-grounding against fresh traces |

`expected_calibration_error` is bounded `[0, 1]`; same scale as Brier.
The reliability diagram exposes whether the miscalibration is
over-confident (predicted > observed) or under-confident (predicted <
observed) per bucket.

## Troubleshooting

**`decisions: 0` with a populated log.** Either `--since` is too tight
or `--kind` filtered everything out. Drop the filters first and
re-narrow.

**Brier looks fine but routing still feels off.** Brier rewards
sharpness *and* calibration; a router that always predicts `0.5` can
post a misleadingly low score on a balanced dataset. Look at the
reliability diagram buckets, not just the headline number.

**Invalid `--since` value.** Accepted suffixes are `s`, `m`, `h`, `d`.
Anything else raises a `ValueError` before reading the log.
