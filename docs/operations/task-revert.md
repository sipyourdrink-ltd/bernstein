# Reverting a task's change (`bernstein undo --dry-run`)

Audience: operators who need to undo one agent task's work and want to
see exactly which files that covers before anything moves.

## Why a task needs its own change set

An agent task is normally one logical change spread over several files.
Reverting it by hand — picking files, or reverting the merge commit —
routinely misses part of the change or reverts unrelated work that
landed nearby. To revert a task cleanly you first need a precise,
reproducible answer to *what did this task change*.

Two sources cannot supply that answer today:

| Source | Why it cannot answer |
|---|---|
| Commit subjects | `bernstein undo` matches `task:<task_id>` against the last 50 subjects. Nothing writes that string — agent commits read `[WIP] <title>` and `feat: <summary>` — so the scan finds nothing for a real task. |
| The lineage spine | A spine entry carries `artifact_path`, `content_hash`, `actor` and `step_id`, and **no task id**. Writes made by a CLI adapter's own subprocess never cross that boundary at all. |

The task's worktree does know. Every task runs on its own
`agent/<session_id>` branch, and the session-to-task binding is already
recorded in `.sdd/runtime/pids/<session_id>.json`.

## Resolving the change set

`src/bernstein/core/worktrees/change_set.py` exposes
`resolve_task_change_set(repo_root, task_id)`. It:

1. finds the worktree whose PID record carries `task_id`, via
   `classify_worktrees`;
2. computes the merge base of `agent/<session_id>` and the integration
   branch (`main`);
3. diffs **three-dot** — from the merge base, not from where the
   integration branch now stands — and returns each changed path with
   its pre- and post-change blob hash.

The three dots are load-bearing. A two-dot `main..agent/<sid>` diff
reports every path that landed on `main` *after* the task forked as a
deletion the task never made. A revert built on that set would restore
files the task never removed and still report a clean run.

Each entry is a `TaskChangePath`:

| Field | Meaning |
|---|---|
| `path` | Repo-relative path, as git names it |
| `change_type` | `added` / `modified` / `deleted` / `typechange` |
| `pre_hash` | Blob before the task; `None` when the task added the path |
| `post_hash` | Blob on the task branch; `None` when the task deleted it |

Paths come back in git's path order, so two operators resolving the same
task get the same set in the same order.

## Refusals

The resolver raises `TaskChangeSetUnresolved` rather than returning an
empty set when it cannot tell. An empty set is a real answer — a task
that touched no files — so the two must not look alike:

| Situation | Behaviour |
|---|---|
| No worktree records the task | Refuse, naming the task id |
| Several worktrees claim the task | Refuse, naming the sessions — the binding cannot be arbitrated |
| Branch missing, or git fails / times out | Refuse, quoting git's error |

## `bernstein undo --dry-run`

```
bernstein undo <task_id> --dry-run
```

Prints one line per changed path — kind, path, and abbreviated
`pre -> post` blob — and exits. It never touches the working tree, the
index, or `HEAD`: `git status --porcelain` is byte-identical before and
after.

```
╭────────── Change set for task-abc123 (agent/backend-abc123) ──────────╮
│ added      docs/new.md  - -> 3e757656                                 │
│ deleted    old/legacy.py  da9ee9d1 -> -                               │
│ modified   src/thing.py  3367afdb -> b66ba06d                         │
╰───────────────────────────────────────────────────────────────────────╯
Dry run: nothing was reverted.
```

`--dry-run` requires a task id. `--all` names no single task, so it has
no change set to print; combining the two is a usage error rather than
an empty report that would read as "this session changed nothing".

An unresolvable task id exits non-zero with the reason.

## Not yet implemented

The dry run reports the change set; it does not revert it. Without
`--dry-run`, `bernstein undo` still uses the commit-subject scan
described above. Still to come: the isolated restore of each path to its
pre-task blob, detection of paths a later task has since changed, and a
signed reversal receipt anchored in the audit chain.
