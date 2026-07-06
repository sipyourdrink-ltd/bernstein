# Fork-from-step

Rewind a run to any prior step and branch a new run from it, using a git
commit as the snapshot primitive.

```
bernstein fork --run <run-id> --from-step N
```

## Why

Session rewind needs a restore point for the working tree at a given
step. Bernstein already isolates each task in a git worktree, and a git
commit is a cheap, content-addressed snapshot. Rather than wait for
cloud-sandbox snapshot APIs, fork-from-step commits the worktree tree to
a dedicated snapshot ref and branches a new run from it.

## How it works

1. During a run, each snapshot commits the worktree tree to
   `refs/bernstein/snapshots/<run_id>/<step_index>` and records the
   commit sha in the run's event journal at that step.
2. `bernstein fork --run <id> --from-step N` reads the journal, finds
   the snapshot sha recorded at step N, and confirms the snapshot ref
   still resolves to that exact sha.
3. The snapshot commit is checked out into a fresh, isolated worktree.
4. A new run is started whose journal's first event parent-links the
   fork point (parent run id, fork step, snapshot sha), so the run
   history becomes a tree rather than a list.

## Verifiability

The snapshot id is a git commit sha, so it is content-addressed. The
same sha is stored in the journal and pinned to the ref, so the two are
cross-verifiable: a verifier holding the parent journal and the snapshot
ref can confirm the child branched from exactly that step.

If a snapshot ref is repointed at a different commit, `fork` refuses:
the ref sha no longer matches the sha the journal recorded at that step.

When an audit directory is passed (`--audit-dir`), the fork is also
recorded as a `replay.fork_snapshot` event in the HMAC-chained audit
log, binding the fork lineage into the tamper-evident chain.

## Backend support

Worktree is the supported snapshot backend. Cloud sandbox backends
(Docker, E2B, Modal, Daytona, Vercel, Blaxel, SSH) still decline
snapshot with `NotImplementedError`; fork-from-step runs against the
worktree backend.

## Flags

| Flag | Default | Meaning |
|---|---|---|
| `--run ID` | required | Parent run id to fork from. |
| `--from-step N` | required | Journal step index to fork at; a snapshot must have been recorded there. |
| `--repo-root DIR` | current dir | Repository root that owns the snapshot refs. |
| `--sdd-dir DIR` | `<repo-root>/.sdd` | Run state dir holding `runs/<run_id>/journal.jsonl`. |
| `--audit-dir DIR` | unset | When present, the fork is recorded into the HMAC audit chain. |
| `--json` | off | Emit machine-readable JSON. |

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Fork created. |
| 1 | Fork failed: no snapshot at the step, a tampered snapshot ref, or a checkout error. |
