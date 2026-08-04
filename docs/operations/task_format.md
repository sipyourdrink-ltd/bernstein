# Task file format: parallel flag and story link

This document describes the per-task fields the planner sets so the
orchestrator can schedule parallel batches and roll back at the
user-story level. See also [orchestration/task-dag.md](../orchestration/task-dag.md)
for the DAG walker and CLI.

## Fields

| Field | Default | Meaning |
| --- | --- | --- |
| `parallel_safe` | `false` | Task may run concurrently with other parallel-safe tasks whose dependencies are also satisfied. Absence is treated as serial-only. |
| `story_id` | `null` | User-story slice this task belongs to. All tasks sharing a `story_id` form one rollback unit. |

Both fields are persisted on the `Task` dataclass and round-trip through
`Task.from_dict`.

## Required backlog file format

Files under `.sdd/backlog/open/` (and `.sdd/backlog/issues/`) must be one of
two shapes so the orchestrator can extract a title and route the task:

1. **YAML frontmatter** - a `---` delimited block at the top of the file
   (see [YAML frontmatter](#yaml-frontmatter-ticket-format-v1) below).
2. **Markdown fields** - a `# ` heading for the title plus optional
   `**Role:**` / `**Priority:**` / `**Scope:**` / `**Complexity:**` lines.

A file that is neither (for example a plain YAML file with no `---` block and
no `# ` heading) cannot be parsed as a ticket. It is skipped rather than
spawned, and `bernstein --dry-run` prints a warning naming the file so the
gap is visible instead of silent.

## YAML frontmatter (Ticket Format v1)

```yaml
---
id: T001
title: Add YAML loader
role: backend
parallel_safe: true
story_id: US1
context_files:
  - docs/adr/0007-retries.md
---
```

### Context files

`context_files` names the reference files (worktree-relative) the worker
on this task should read. The declaration reaches the worker: the parser
carries it in the task payload under `metadata["context_files"]`, the
stored task keeps it, and at spawn the orchestrator lists the files in
the worker's task-specific CLAUDE.md.

The attachment is recorded, not just copied. At dispatch each declared
path is resolved in declared order against the worker's worktree and
content-addressed as `(path, order, sha256)`; the resolved set is
recorded in the run journal as a `context.files_attached` event next to
`agent_spawned`, so which reference material the worker saw - at which
content - is answerable offline, and a verifier can recompute the
digests from the files and match. A path that does not resolve keeps its
position in the record with a reason code (`missing`, `is_directory`,
`unreadable`, `outside_root`, or `invalid` for a path the filesystem
cannot represent at all) and a log warning instead of being silently
skipped; it does not abort the spawn. Crash-recovery resumes record the
same event, re-resolved against the preserved worktree, so a resumed
worker's context is pinned as it exists after the crashed agent's edits.
Tickets that declare nothing produce byte-identical payloads and records
as before.

`ticket_type` and `affected_paths` ride in the same payload `metadata`
mapping when set. `depends_on` in frontmatter still refers to ticket ids
and is not forwarded to the server (task-id resolution is a separate
concern).

Plans declare the same thing at the top level; see
[architecture/plans.md](../architecture/plans.md).

## Markdown checkbox DAG

For hand-authored multi-task plans, use one checkbox per task:

```
- [ ] [T001] [P] [US1] Add YAML loader
- [ ] [T002] [US1] Wire orchestrator -> T001
```

| Marker | Effect |
| --- | --- |
| `[T<id>]` | Required identifier. |
| `[P]` | Sets `parallel_safe = true`. Absence keeps the default serial-only behaviour. |
| `[US<n>]` | Sets `story_id` to the user-story slice. |
| `-> T###, T###` | Trailing arrow declares inline dependencies. |

## Scheduler behaviour

The scheduler resolves parallel-safety in this order:

1. **Declarative wins.** If both candidate tasks have `parallel_safe`
   set, the boolean answer is exact: both `True` allows concurrency;
   either `False` forces serial.
2. **Legacy fallback.** Tasks lacking the attribute (older entries
   from a stale store) fall through to the file-overlap heuristic on
   `owned_files`.

## Rollback semantics

When every task in a `story_id` group completes, the orchestrator
surfaces "story `<id>` complete" as a single milestone. A
story-scoped revert reverses **only** the changes attributed to that
story id - sibling stories remain intact. Tasks without a `story_id`
participate in milestone reporting individually and are not bundled.

## Out of scope

- Full DAG dependency editor UI.
- Cross-story dependency inference.
