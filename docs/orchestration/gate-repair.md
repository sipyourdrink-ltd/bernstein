# Merge-gate repair

When the merge gate (lint + affected tests) fails on a finished branch in
the reap-and-merge path, the orchestrator seeds exactly one bounded repair
task on the same branch before falling through to the existing
reopen/permanent-fail handling.

## Why

A gate failure used to just end the run there: the branch is parked and
the exact command output that explains why lands in a log the operator
has to go excavate. That context exists at the moment of failure and was
being thrown away.

## Behaviour

| Step | Outcome |
|---|---|
| Gate fails, no prior repair attempt on this task | One repair task is created. Its description is the tail (~40 lines) of the real gate output plus "make the existing tests and lint pass; do not rewrite the feature; keep the diff as small as possible". The failing worktree is preserved so the repair task resumes the same branch instead of starting a fresh one from main. |
| Repair task's own gate check passes | The branch proceeds through the normal gate/merge path -- no special-casing, it is just a task that passed. |
| Repair task's own gate check also fails | No second repair is scheduled (the repair task's metadata already carries `gate_repair_attempted`); it goes through the existing reopen/permanent-fail budget unchanged. |
| Original (pre-repair) task | Failed with a reason pointing at the repair task id, so the same failure never runs through the generic reopen budget *and* the repair task concurrently. |

## Configuration

| Key | Type | Default | Meaning |
|---|---|---|---|
| `gate_repair_enabled` (top-level `bernstein.yaml`, threaded to `OrchestratorConfig.gate_repair_enabled`) | bool | `true` | Master switch. |
| `BERNSTEIN_GATE_REPAIR` | env | unset | Overrides the config value at runtime. Accepts `1/true/yes/on/enable/enabled` and `0/false/no/off/disable/disabled` (case-insensitive); any other value is ignored and the config value applies. |

```yaml
# bernstein.yaml
gate_repair_enabled: true
```

## Module

`bernstein.core.tasks.task_lifecycle`

| Symbol | Purpose |
|---|---|
| `_gate_repair_enabled` | Resolves the switch (env override, then config). |
| `_build_gate_repair_goal` | Builds the repair task's description from a quality-gate result. |
| `_maybe_schedule_gate_repair` | Creates the repair task and preserves its worktree; returns the new task id or `None`. |

## Tests

`tests/unit/test_gate_repair.py` covers:

- gate fails -> one repair task scheduled, its description embeds the gate
  output tail and the fix instructions.
- a task whose metadata already carries a repair attempt schedules no
  second one.
- a passing gate result schedules nothing.
- the switch, off via config or the env override, disables scheduling.
- `_reap_and_cleanup_session` preserves the worktree when a repair was
  scheduled, and still cleans up when one was not.
