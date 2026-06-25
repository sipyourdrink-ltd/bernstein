# Worktrees CLI (`bernstein worktrees`)

Audience: operators with a `.sdd/runtime/worktrees/` (or legacy
`.sdd/worktrees/`) tree that has accumulated orphans after kills,
crashes, or aborted experiments.

## Overview

`bernstein worktrees` inspects and reaps worktrees the orchestrator
left behind. The classifier in
`src/bernstein/core/worktrees/classifier.py` is the single source of
truth for state. The CLI module
(`src/bernstein/cli/commands/worktrees_cmd.py`) handles I/O:
rendering the table, holding the GC lock, prompting the operator,
appending a tamper-evident `worktree.reap` event to the HMAC audit
chain (see [Audit trail](#audit-trail)), and emitting the `worktree.gc`
lifecycle event for plugins.

The tool honours both the spec layout (`.sdd/runtime/worktrees/`) and
the legacy layout (`.sdd/worktrees/`) the `WorktreeManager` currently
produces.

## State machine

| State | Rule |
|-------|------|
| `active` | Task record exists at `.sdd/runtime/pids/<sid>.json` AND `os.kill(pid, 0)` succeeds. |
| `orphan` | Directory exists but no task record. |
| `stale` | Task record exists but PID is dead AND last trace mtime > 24h ago. |
| `corrupt` | Directory exists but the `.git` anchor is missing. |

Priority on conflicts: `corrupt > orphan > stale > active`. A dead PID
with a fresh trace stays `active` to avoid racing a restart.

## Unsaved-work safety

Reaching a terminal state (`orphan`/`stale`/`corrupt`) is necessary but
not sufficient for a reap. Before a worktree is considered reapable the
classifier probes its git state *inside the worktree itself*:

- `git status --porcelain` - any uncommitted change (tracked or
  untracked) marks the worktree as holding unsaved work.
- `git merge-base --is-ancestor HEAD <integration-branch>` (default
  `main`, with an upstream-ahead fallback) - if the worktree branch has
  commits that are not reachable from the integration branch they are
  unmerged work that a reap would destroy.

A worktree that holds unsaved work is **not reapable** and `gc` skips it
with an operator-visible message naming the directory. This matches the
invariant the rest of the codebase already enforces (the `maintenance`
command preserves unmerged branches by default). A missing PID record -
the trigger for `orphan` - is exactly the crash-recovery case where
committed-but-unmerged work is most likely stranded, so "no PID record"
is never treated as "safe to delete".

A `corrupt` worktree has no readable `.git` and cannot be probed: it is
reapable only when its directory is empty of files, otherwise it is
preserved for manual handling. Any git probe that cannot decide
(timeout, missing ref) errs toward preserving the worktree - the guard
only ever blocks deletion, it never deletes more.

## CLI

```text
bernstein worktrees list   [--workdir DIR] [--json]
bernstein worktrees gc     [--workdir DIR] [--yes] [--dry] [--force-unsaved]
bernstein worktrees unlock [--workdir DIR] [--force] [--json]
```

- `list` - tabular dump with path, task id, state, age, size, PID. Use
  `--json` for scripting. The JSON `reapable` field already reflects the
  unsaved-work veto.
- `gc` - reap non-`active` worktrees that carry no unsaved work. `--yes`
  skips the confirmation prompt; `--dry` prints what would be reaped
  without touching disk.
- `--force-unsaved` - also reap worktrees that hold unsaved work
  (uncommitted changes or unmerged commits). **Dangerous:** this
  destroys the only copy of that work. It requires a second explicit
  confirmation (unless `--yes` is also passed) and mirrors the
  `maintenance` command's `--force`. The reap is recorded with
  `forced=true` in the audit trail.
- `unlock` - inspect and clear a stuck GC lock. It reports who holds the
  lock (pid, liveness, age) and removes it when the owner process is gone;
  pass `--force` to clear a lock whose owner still looks alive. The unlock
  is recorded as a `worktree.gc_unlock` event in the audit trail, so a
  manual recovery is never silent.

## GC lock

A single-file lock at `.sdd/runtime/worktree-gc.lock` is held via
`O_EXCL` for the duration of `gc`. The lock file records the owner pid and
start time, and is released on exception.

A lock left behind by a crashed or killed `gc` is reclaimed automatically on
the next run once its owner process is gone (or the lock is older than a
generous bound), so a crash no longer wedges future runs. Exit code `2`
still indicates a genuine collision with a live, recent `gc`. To inspect or
clear a lock by hand, use `bernstein worktrees unlock`.

## Lifecycle event

The CLI emits `worktree.gc` after each reap (or, with `--dry`,
once per would-be reap), and once per worktree preserved for safety.
Plugins can hook this event; the env keys exposed to handlers are
`BERNSTEIN_WORKTREE_GC_*`, including `BERNSTEIN_WORKTREE_GC_REAPED`
(`0` for a safety-skip, `1` for a reap) and
`BERNSTEIN_WORKTREE_GC_UNSAVED` (`1` when the worktree held unsaved
work). This notification is best-effort and ephemeral - it requires a
plugin `HookRegistry` and leaves no durable record. For the durable,
tamper-evident record see the audit trail below.

## Audit trail

Reaping a worktree is the orchestrator's only routine destructive
action, so each reap is anchored to the HMAC-chained audit log under
`.sdd/audit/` alongside the best-effort lifecycle event. The audit
write does **not** depend on a plugin `HookRegistry`: it is written
even when the CLI runs standalone.

For every reaped worktree - and for every worktree *skipped* for safety -
`gc` appends one event:

| Field | Value |
|-------|-------|
| `event_type` | `worktree.reap` |
| `actor` | `worktrees-gc` |
| `resource_type` | `worktree` |
| `resource_id` | session id (worktree directory basename) |

The `details` payload captures, before deletion: `state`, `task_id`,
`path`, `size_bytes`, `age_seconds`, `last_trace_mtime`, the
pre-deletion git `head_sha`, a `dirty` flag (uncommitted/unmerged
changes present), `has_unsaved_work` (the classifier's reap veto),
`reaped` (`false` for a safety-skip, `true` for an actual deletion),
`forced` (`true` when `--force-unsaved` overrode the veto), and
`dry_run`.

**Safety-skips are recorded, not silent.** When `gc` preserves a
worktree because it holds unsaved work, it still appends a
`worktree.reap` event flagged `reaped=false` with the unsaved-work
reason, so the decision to preserve is anchored in the same
tamper-evident chain as a real reap.

**Pre-deletion fingerprint.** `head_sha` and `dirty` are read from the
worktree *before* `rmtree`, so the entry proves exactly which commit
and working-tree state were destroyed - enough to decide whether to
restore from reflog after an accidental GC. A `corrupt` worktree may
have no readable `.git`; its fingerprint degrades to `head_sha=null`
and `dirty=null` rather than crashing GC or attributing the enclosing
repository's HEAD.

**Fail-closed contract.** The audit event is appended *before* the
directory is removed. If the append fails (e.g. audit key permission
error, full disk) the reap is aborted and the error propagates - a
worktree is never destroyed without a record. Operators whose audit
key is misconfigured will see `gc` fail rather than silently delete;
fix the key (see `docs/security/audit-log.md`) and retry.

**`--dry` mode** records the event flagged `dry_run=true` and performs
no `rmtree`, so a dry run is itself auditable.

Verify and inspect the reap events:

```bash
bernstein audit verify-hmac                       # exits non-zero on any tamper
bernstein audit query --event-type worktree.reap  # list reap events
```

## TUI integration

The TUI's `WorktreeListPanel` refreshes the same classifier output
every 10 seconds and uses `count_reapable()` to drive the status-bar
badge.

## Examples

Inspect what's on disk:

```bash
bernstein worktrees list
```

Preview reap plan without touching disk:

```bash
bernstein worktrees gc --dry
```

Reap non-interactively (CI / cron):

```bash
bernstein worktrees gc --yes
```

JSON dump for piping into `jq`:

```bash
bernstein worktrees list --json | jq '.[] | select(.state == "orphan")'
```

## Troubleshooting

**Exit code 2 from `gc`.** A peer `gc` holds the lock. Inspect
`.sdd/runtime/worktree-gc.lock`; if its PID is dead, remove the file
and retry. Otherwise wait for the peer to finish.

**`active` row but the task already died.** Either the task record at
`.sdd/runtime/pids/<sid>.json` is stale (PID matches a recycled
process) or the last trace is fresh enough that the classifier holds
`active`. Wait 24h for the rule to flip to `stale`, or remove the
`pids/<sid>.json` file by hand.

**`corrupt` rows after a bad merge.** The classifier marks any worktree
without a `.git` anchor as corrupt regardless of task record. An empty
corrupt directory is reaped automatically. A corrupt directory that
still holds files cannot be probed for unsaved work, so `gc` preserves
it and reports it; inspect the contents, salvage anything you need, then
delete the directory by hand (or rerun `gc --force-unsaved`).

**`gc` skipped a worktree with unsaved work.** This is the safety guard:
the worktree has uncommitted changes or commits not merged into the
integration branch. Open the directory and commit/push or salvage the
work. Once it is clean and merged, `gc` reaps it normally. To delete it
regardless - destroying the unsaved work - rerun with `--force-unsaved`
(an extra confirmation is required).
