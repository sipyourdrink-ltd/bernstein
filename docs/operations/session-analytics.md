# Session analytics (`bernstein recap`)

`bernstein recap` prints a post-run summary — task completion counts, git
diff stats, quality-gate score distribution, and a cost breakdown — so an
operator can see what a run accomplished without piecing it together from
the task list and the logs separately.

## CLI

```bash
bernstein recap                   # Rich tables
bernstein recap --as-json         # machine-readable JSON
bernstein recap --archive tasks.jsonl --as-json  # local task archive
bernstein recap --since 45m --workdir ./project --as-json  # local interval
```

| Flag | Default | Meaning |
|---|---|---|
| `--archive PATH` | unset | Read a local JSONL task archive instead of the server. |
| `--since DURATION` | unset | Summarize recent workspace records, for example `45m`, `6h`, or `2d`. |
| `--workdir DIR` | `.` | Project root containing `.sdd`, used with `--since`. |
| `--as-json` | off | Emit the raw JSON payload instead of Rich tables. |

`--archive` and `--since` are mutually exclusive; combining them is a
usage error (exit 2). Neither local mode contacts the task server.

## What each source shows

### Live task store (default)

With neither source flag, the command calls the task server's `GET /recap` endpoint
(`core/routes/observability.py`), which reads every task currently known to
the live task store and returns:

| Section | Contents |
|---|---|
| `summary` | Total / completed / failed task counts and success rate. |
| `diff_stats` | `git diff --numstat` rollup for completed tasks: files changed, additions, deletions. |
| `quality_scores` | Average quality score, A-F grade distribution, last 10 scores, and per-gate averages (lint, tests, type-check, security scan), read from `.sdd/metrics/quality_scores.jsonl`. |
| `cost_breakdown` | Total spend, per-model cost/tokens/invocations, and per-role cost, read from the most recent `.sdd/metrics/costs_*.json` snapshot. |
| `tasks` | Flat list of every task's id, title, status, role, and complexity. |

In the Rich (non-JSON) view, these render as four separate tables: Recap,
Git Diff Stats, Quality Scores, and Cost Breakdown.

### Local task archive (`--archive`)

The archive must contain one task object per JSONL line, with a `task_id`
(or `id`) and `status`. The last record for each ID supplies its current
status. This mode emits `summary`, `tasks`, and `cost_breakdown`; it does
not reconstruct diff stats or quality scores. Recorded `cost_usd` values
from all archive records, including retry attempts, contribute to the
total. `cost_breakdown.records_with_cost` counts those records. If no records contain costs,
`total_cost_usd` is null (displayed as unknown). When some costs are
missing, the numeric total sums only the recorded costs; a recorded
`cost_usd: 0` contributes a known zero. An unreadable or invalid
archive exits 1.

### Workspace interval (`--since`)

This mode reads timestamped task records in `.sdd/runtime/tasks.jsonl`
and API-usage and error-rate JSONL metrics in the selected workspace. It
returns `completed_tasks`, `failed_tasks`, `cost_spent`, `duration_s`, and
`events`, rather than the live report's diff, quality, and per-model tables.
Cost is estimated from local API-usage records and falls back to zero
when no cost is recorded; it does not have the archive's unknown-cost marker.

## Limitations

- In live mode, `quality_scores` and `cost_breakdown` degrade to zeroed defaults when
  their respective `.sdd/metrics/` files are missing (a project with no
  quality gates or cost tracking configured still gets a valid, empty
  response rather than an error).
- The live summary reflects whatever tasks the task server currently holds, not
  a fixed archive snapshot — running `bernstein recap` again after more
  tasks complete returns different numbers.

## Source

- `src/bernstein/cli/commands/advanced_cmd.py` — `bernstein recap` command and table rendering
- `src/bernstein/core/routes/observability.py` — `GET /recap` endpoint, quality-score and cost-breakdown aggregation
