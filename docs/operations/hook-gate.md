# In-process verification gates

Verification normally happens *after* a worker finishes: the scheduler
inspects results and re-dispatches on failure, paying a full round-trip for
every miss. A gate-capable adapter instead wires blocking hooks into the
worker's own runtime so the required checks run **inside the session**,
before the worker's turn ends.

## What it does

For a gate-capable adapter, Bernstein renders two hooks at spawn time:

| Hook | Trigger | Behaviour |
|---|---|---|
| Tool-permission matcher | `PreToolUse` on `Write` / `Edit` / `MultiEdit` / `NotebookEdit` | Refuses a write whose target falls outside the task's path allowlist. Realpath containment refuses a `..` traversal or an in-scope symlink that resolves outside the worktree. |
| Completion gate | `Stop` | Runs the task's *required* evidence producers in-session; refuses to let the turn end while any of them fail. |

Both hooks shell out to:

```
bernstein hook-gate check --session <id> --event PreToolUse < event.json
bernstein hook-gate check --session <id> --event Stop < event.json
```

The command reads the hook event JSON on stdin, evaluates the policy
persisted at `.sdd/runtime/hook_gate/<session>.json` (written at spawn time
from the task's `owned_files` and required `evidence_producers`), and exits
`2` to block or `0` to allow.

## Which adapters support it

Only adapters with a genuine blocking hook surface are wired up. Currently:
`claude` and `claude_routine`. Every other adapter renders no gate hooks and
degrades to the scheduler-side gate with no policy weakening — there is no
partial or best-effort in-process enforcement for an adapter that lacks the
surface.

## Trust model

The in-process gate is **defence in depth and a cost optimisation**, not a
replacement for the authoritative check. The scheduler-side evidence gate
(`bernstein evidence show` / `verify`) remains authoritative and runs
regardless of what happened in-session.

Every gate outcome — a blocked completion or a refused out-of-scope write —
is sealed as the same signed, chain-anchored evidence bundle the
scheduler-side gate produces (via `bernstein audit verify`). A downstream
verifier cannot tell from the receipt schema whether enforcement fired
in-process or scheduler-side.

## Failure handling

Enforcement is fail-open on unexpected errors: a bug while sealing a receipt
must never wedge a worker, so sealing failures are logged and swallowed —
but a computed block/allow decision is always emitted from the check itself.
An unsafe or unrecognised session id degrades to allow-through rather than
touching the filesystem with an unvalidated path.

A policy that enforces nothing (no path allowlist, no required producers) is
a pass-through: both hooks always allow.

## Source

`src/bernstein/adapters/hook_gate_render.py` (hook rendering per adapter),
`src/bernstein/core/security/hook_gate.py` (policy model and gate
evaluation), `src/bernstein/cli/commands/hook_gate_cmd.py`
(`bernstein hook-gate check`).
