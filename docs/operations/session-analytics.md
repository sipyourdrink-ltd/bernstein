# Session analytics (`bernstein recap`)

`bernstein recap` prints a post-run summary — task completion counts, git
diff stats, quality-gate score distribution, and a cost breakdown — so an
operator can see what a run accomplished without piecing it together from
the task list and the logs separately.

## CLI

```bash
bernstein recap                   # Rich tables
bernstein recap --as-json         # machine-readable JSON
```

| Flag | Default | Meaning |
|---|---|---|
| `--archive PATH` | `.sdd/archive/tasks.jsonl` | Accepted but currently unused — see [Limitations](#limitations). |
| `--as-json` | off | Emit the raw JSON payload instead of Rich tables. |

## What it shows

The command calls the task server's `GET /recap` endpoint
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

## Limitations

- `--archive PATH` is accepted by the CLI but is not read anywhere in the
  command's implementation (`cli/commands/advanced_cmd.py`) — the command
  always queries the server's `/recap` endpoint against the live task
  store, regardless of the flag's value.
- `quality_scores` and `cost_breakdown` degrade to zeroed defaults when
  their respective `.sdd/metrics/` files are missing (a project with no
  quality gates or cost tracking configured still gets a valid, empty
  response rather than an error).
- The summary reflects whatever tasks the task server currently holds, not
  a fixed archive snapshot — running `bernstein recap` again after more
  tasks complete returns different numbers.

## Source

- `src/bernstein/cli/commands/advanced_cmd.py` — `bernstein recap` command and table rendering
- `src/bernstein/core/routes/observability.py` — `GET /recap` endpoint, quality-score and cost-breakdown aggregation
