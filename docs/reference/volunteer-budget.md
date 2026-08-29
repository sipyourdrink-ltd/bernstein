# Volunteer donor budgets

Donor budgets put a persistent ceiling on volunteer work handled by a hub. They
are separate from Bernstein's per-project daily usage provisioning: a donor
budget does not reset at midnight and survives worker restarts.

```bash
bernstein volunteer budget --budget-tasks 10 --budget-hours 6 \
  --budget-tokens 500000 --max-size m --local-only
bernstein volunteer budget                 # inspect limits and consumption
bernstein volunteer budget --json          # machine-readable form
```

The defaults are stored with the existing volunteer run state under
`.sdd/runtime/volunteer/budget/`:

- `config.yaml` contains the donor's authorized limits.
- `ledger.json` contains completed usage and in-flight reservations.

Both files may be redirected with `--config` and `--ledger`. Writes use a
flushed temporary file in the destination directory followed by an atomic
replace, so a worker restart observes either the old complete ledger or the
new complete ledger, never a partial write. A malformed ledger is refused
instead of silently restoring spent capacity.

## Admission and reconciliation

Before making an external claim, the worker reserves one task slot plus that
task's estimated wall-clock time and tokens. Completion or abort removes the
reservation and records actual wall-clock and token usage. The estimate is
retained for audit, while future token admission uses actual completed usage
plus estimates still reserved by in-flight work.

Exhausting a limit refuses the next claim. It never kills a task already in
flight: terminal reconciliation deliberately does not re-run admission.

`--max-size` accepts `xs`, `s`, or `m` and rejects larger task labels.
`--local-only` admits only adapters whose registered
`AdapterCapabilityProfile.local_models` capability is true; absence of a
capability profile is a refusal, not an assumption that an adapter is local.

`LeaseStore.claim()` performs this admission before making a lease durable, so
calling the store directly cannot bypass the policy. The hub claim endpoint
accepts `task_size`, `token_estimate`, `wall_clock_hours`, and `adapter_id` and
returns a conflict whose detail names the exhausted dimension.
Submit and release requests may provide the task's measured `actual_tokens`;
when unavailable, reconciliation conservatively charges the reserved estimate.

Signed volunteer result receipts carry task, wall-clock, and token line items
with authorized, used, reserved, and remaining values. This makes the budget
decision auditable without adding pricing policy or synchronizing balances
between machines.
