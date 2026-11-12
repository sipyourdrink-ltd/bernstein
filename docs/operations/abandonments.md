# Abandonments (agent abandon primitive)

Audience: operators triaging tasks that agents quit honestly instead of
silently half-completing or being killed by the watchdog.

## Overview

`abandon` is a first-class agent verb. When an adapter calls
`ctx.abandon(reason, detail)` (or an operator runs the future
`bernstein task abandon`), the task store:

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

- `list` prints the most recent rows (newest first; default 20).
- `stats` shows roll-ups by reason / role / adapter plus a total.

Pass `--json` (or set the global `--json` flag) for machine-readable
output.

## Examples

Last twenty:

```bash
bernstein abandonments list --limit 20
```

Find roles abandoning more than expected:

```bash
bernstein abandonments stats --json | jq '.by_role'
```

Filter ledger to recent unsafe-change abandons:

```bash
jq -c 'select(.reason == "unsafe_change") | select(.timestamp > (now - 86400))' \
  .sdd/runtime/abandonments.jsonl
```

## Aggregations exposed by the module

- `abandon_rate_by_role` - rate per role, range `[0, 1]`
- `abandon_rate_by_adapter` - rate per adapter, range `[0, 1]`

Both are imported by the cost / observability dashboards and surface in
`bernstein abandonments stats`.

## Troubleshooting

**Downstream task stuck in `BLOCKED_BY_ABANDON`.** Cascade is by
design: a consumer that needed an abandoned upstream cannot make
progress. Re-add the upstream as a new task (different id) or mark the
consumer abandoned with a matching reason.

**Ledger rows with `reason: other`.** The agent fell back to `OTHER`
because its prompt-side reason text did not match a taxonomy value.
Inspect `detail`, then refine the prompt preamble to nudge the precise
reason.
