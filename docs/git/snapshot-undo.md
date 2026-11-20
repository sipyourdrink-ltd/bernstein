# Snapshot undo and stacked branches

Bernstein takes a cheap git snapshot of the work tree before every
tool call that mutates files. Operators can rewind any single step,
diff two snapshots, and inspect the chain of branches each task ran
through.

## TL;DR

| Command | Purpose |
|---|---|
| `bernstein git snapshots [--task <id>]` | List recent snapshots, newest first |
| `bernstein git undo <snapshot_id>` | Restore the work tree to that snapshot |
| `bernstein git diff <a> <b>` | Show diff --stat between two snapshot trees |
| `bernstein git stack --task <id>` | List branches in the task's stack (oldest first) |
| `bernstein git stack-clear --task <id>` | Drop the stack ordering refs for a task |
| `bernstein git gc [--days N]` | Garbage-collect snapshots older than N days (default 30) |

All commands operate on the current working directory by default;
pass `--workdir <path>` to point at another repo.

## How snapshots work

A snapshot is a single git tree object. Capturing one is cheap: the
orchestrator drives a throwaway index (the same trick `git stash`
uses) and writes the tree under the side ref namespace
`refs/bernstein/snapshots/<id>`. Identical trees deduplicate by content
hash, so two snapshots between near-identical states cost a few
hundred bytes each.

Snapshots are never pushed by the default `git push` invocation - the
ref namespace is local-only. If you want to share them, push the refs
explicitly: `git push origin 'refs/bernstein/snapshots/*'`.

### What gets captured

When the repository has a current commit (`HEAD`), the temporary index starts
from that commit. The snapshot therefore contains the current working-tree
contents of files tracked in that commit, including edits made after staging,
and records tracked files deleted from the working tree as removals. New
(non-ignored) files are also captured, even if they have not been staged.

New files that match the repository's ordinary Git ignore rules are omitted:
`git add -A` does not add ignored, untracked files. A file already tracked in
the current commit is still captured if it is edited or deleted, even if an
ignore rule now matches its path. In a repository with no current commit, the
temporary index starts empty, and the snapshot captures the addable files in
the working tree.

The snapshot describes the working-tree state, not the real staging index.
For example, if `notes.txt` is staged as `staged version` and then edited to
`later working-tree version`, the snapshot contains `later working-tree
version`; the real index still contains `staged version`. A new ignored file
such as `.env.local` is omitted from the snapshot. Snapshotting leaves the
real index unchanged.

What is **not** captured:

- Environment variables, secrets caches, or anything outside the work
  tree.
- New files excluded by ordinary Git ignore rules.
- The agent's deliberate staging decisions - the real index is left
  untouched.

### Storage and inspection

The temporary index lives in the **current worktree's Git admin directory**,
resolved by `git rev-parse --path-format=absolute --git-dir`. In a linked
worktree, `.git` is a file pointing to that admin directory, not a directory
in which to store the index. The temporary index is removed after capture;
the real staging index is left unchanged.

Snapshot metadata JSON sidecars instead live at
`<common-git-dir>/bernstein/snapshots/<snapshot_id>.json`, where
`<common-git-dir>` is resolved by
`git rev-parse --path-format=absolute --git-common-dir`. The sidecars are
shared by the main and linked worktrees, just like the snapshot refs.
Removing the linked worktree that created a snapshot does not remove this
shared metadata: the snapshot and its task information remain accessible
from another worktree of the same repository. Cloning a repository does not
copy these JSON sidecars.

For inspection, `bernstein git snapshots` lists snapshot refs, sorted
newest-first by the metadata timestamp (`ts_ns`). If a ref still exists but
its sidecar is missing, `SnapshotStore.get()` and `SnapshotStore.list()`
reconstruct a minimal **ref-backed record**: the snapshot ID, tree SHA and
ref are retained, the label is the snapshot ID, and `ts_ns=0`. These records
sort last, after snapshots with positive metadata timestamps, regardless of
the timestamp embedded in the snapshot ID.

The fallback record has no task, tool-call, agent or parent-snapshot
information. In particular, `task_id` is `None`, so
`bernstein git snapshots --task <id>` excludes it even if the snapshot
originally belonged to that task. Use the unfiltered list to find such
snapshots; their ref-backed trees can still be used for undo.

## Pre-tool-use hook

The orchestrator calls `SnapshotStore.take(...)` from the
`preToolUse` lifecycle hook for any tool call that mutates the
workspace. Each snapshot records the task ID, tool call ID, and agent
slug so it joins cleanly against the lineage audit log.

Disabling: set `BERNSTEIN_SNAPSHOTS_DISABLED=1` in the agent's
environment, or override the hook in `bernstein.yaml`.

## Stacked branches

Each agent run inside a task creates its own branch. Rather than all
runs targeting the same base, Bernstein **stacks** them: run N+1's
branch is created from the tip of run N's branch. This keeps the
chronological order visible in the eventual PR review.

The stack is recorded under `refs/bernstein/stacks/<task_id>/<n>` -
one ref per run. `bernstein git stack --task <id>` enumerates the
stack in ascending order.

When a task is archived, call `bernstein git stack-clear --task <id>`
to drop the stack ordering refs (snapshots are left intact and follow
the normal GC window).

## Retention

The default retention window is **30 days**. Run
`bernstein git gc --days 30` periodically (or wire it into your
maintenance cron) to prune older snapshots. The command only deletes
the side refs and metadata sidecars; the orphaned tree objects are
reclaimed by the next normal `git gc` pass.

## CLI examples

```
# List the last 20 snapshots for one task.
bernstein git snapshots --task T-1234 --limit 20

# Rewind to a specific snapshot. Refuses to clobber uncommitted
# changes unless you pass --force.
bernstein git undo 20260519T091201Z-9af83b7d4c10

# What changed between two checkpoints?
bernstein git diff 20260519T091201Z-9af83b7d4c10 20260519T091534Z-aabbccddeeff

# Show the branch stack for a task.
bernstein git stack --task T-1234

# Prune anything older than the default window.
bernstein git gc
```

## Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `not inside a git work tree` | `--workdir` points outside a repo | Pass `--workdir <repo-root>` or `cd` into it |
| `work tree has uncommitted changes` | Dirty undo guardrail | Commit / stash, or pass `--force` |
| `snapshot <id> not found` | Snapshot was GC'd or never existed | List snapshots first; check the retention window |
| `branch <name> does not exist` | `stack_push` was called before the branch | Create the branch first, then record the stack entry |

## API reference

Module: `bernstein.core.git.snapshot`

- `SnapshotStore(cwd)` - facade with `take`, `undo`, `list`, `diff`,
  `get`, `delete`, `gc`.
- `Snapshot` - frozen dataclass of captured metadata.
- `stack_push(cwd, *, task_id, branch, parent_branch=None)` - record
  a stack entry.
- `stack_list(cwd, *, task_id)` - enumerate stack entries.
- `stack_clear(cwd, *, task_id)` - drop every stack entry for a task.
