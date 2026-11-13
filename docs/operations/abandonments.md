# Abandonments (agent abandon primitive)

Audience: operators triaging tasks that agents quit honestly instead of
silently half-completing or being killed by the watchdog.

## Overview

`abandon` is a first-class agent verb. When an adapter calls
`ctx.abandon(reason, detail)`, the task store:

1. Flips the task to terminal `ABANDONED` (distinct from `FAILED`).
2. Appends a row to `.sdd/runtime/abandonments.jsonl`.
3. Cascades downstream consumers waiting on the task to
   `BLOCKED_BY_ABANDON` so the dependency scanner stops spinning.

**Invariant.** `ABANDONED` is terminal: the FSM blocks
`ABANDONED -> DONE/CLOSED`. A quietly-given-up task can never look
like a completion.

Source:

- `src/bernstein/core/tasks/abandon.py` - taxonomy, `AbandonmentLedger`, aggregations
- `src/bernstein/cli/commands/abandonments_cmd.py` - read-side CLI

## Reason taxonomy (closed)

| Category | Reason |
|----------|--------|
| Spec / intent | `out_of_scope`, `insufficient_context`, `conflicting_instructions`, `spec_underdetermined` |
| Environment | `time_budget_exhausted`, `budget_exceeded`, `capability_mismatch`, `env_broken` |
| Coordination | `blocked_by_external`, `unsafe_change`, `operator_override` |
| Catch-all | `other` |

The vocabulary is intentionally small so dashboards aggregate without
operator-supplied free-form noise. Agents are nudged in the prompt
preamble to prefer a precise reason over `other`.

## Ledger

`.sdd/runtime/abandonments.jsonl` (append-only). Each row carries:

| Field | Use |
|-------|-----|
| `timestamp` | Epoch seconds |
| `task_id` | Abandoned task |
| `role` | Role at abandon time |
| `reason` | One of the taxonomy values |
| `adapter` | Adapter that issued the abandon |
| `attempts` | Number of attempts before abandon |
| `detail` | Free-form context string |

## CLI

```text
bernstein abandonments list   [--workdir PATH] [--limit N] [--json]
bernstein abandonments stats  [--workdir PATH] [--json]
```

- `list` prints at most `--limit` rows (default 20), ordered newest first.
  A non-positive limit returns no rows. With `--json`, the result is a JSON
  array of row objects (`[]` when there are no valid rows in the selected
  limit).
- `stats` summarizes every valid ledger row in the project, not just the rows
  shown by `list`. Its JSON object has `total` and the grouped row-count maps
  `by_reason`, `by_role`, and `by_adapter`. These are counts, not rates; a
  malformed or invalid ledger row is skipped.

Pass `--json` (or set the global `--json` flag) for machine-readable
output. If `list` has no rows to display (including when `--limit` is
non-positive), it prints `No abandonments recorded.`; JSON `list` returns
`[]`. If the project has no valid ledger rows, text `stats` prints the same
message and JSON `stats` returns
`{"total": 0, "by_reason": {}, "by_role": {}, "by_adapter": {}}`.

## Examples

Last twenty:

```bash
bernstein abandonments list --limit 20
```

Inspect abandonment counts by role:

```bash
bernstein abandonments stats --json | jq '.by_role'
```

Filter ledger to recent unsafe-change abandons:

```bash
jq -c 'select(.reason == "unsafe_change") | select(.timestamp > (now - 86400))' \
  .sdd/runtime/abandonments.jsonl
```

## Rate helpers

The separate `abandon_rate_by_role` and `abandon_rate_by_adapter` helpers
compute rates as abandons divided by abandons plus completed tasks. They
require completed-task counts as input (`completed_by_role` or
`completed_by_adapter`) to supply the denominator; these rates are not fields
in the `stats` output. For example, 2 valid abandonment rows for `backend`
produce a `by_role.backend` count of 2. If there were also 8 completed
backend tasks, the role rate would be `2 / (2 + 8) = 0.2` (20%); the stats
command cannot calculate that rate without the separate completed-task count.

## Troubleshooting

**Downstream task stuck in `BLOCKED_BY_ABANDON`.** Cascade is by
design: a consumer that needed an abandoned upstream cannot make
progress. Re-add the upstream as a new task (different id) or mark the
consumer abandoned with a matching reason.

**Ledger rows with `reason: other`.** The agent fell back to `OTHER`
because its prompt-side reason text did not match a taxonomy value.
Inspect `detail`, then refine the prompt preamble to nudge the precise
reason.
