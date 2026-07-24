# Plan archival

`bernstein plan ls` and `bernstein plan show` query Bernstein's managed
plan lifecycle: every plan YAML under `plans/` is tracked through three
buckets - `active/`, `completed/`, `blocked/` - and archived plans are
made read-only with a run-summary or failure-reason header prepended.

Source: `core/planning/lifecycle.py` (`PlanLifecycle`, `PlanState`,
`ArchivedPlan`); CLI in `cli/commands/plan_archive_cmd.py`.

This is a different concept from the plan **YAML schema** documented at
[Plans](../architecture/plans.md) (stages/steps/`--from-plan`). Lifecycle
tracks *where a plan file currently lives and why*; the schema page
covers *what's inside a plan file*.

---

## Usage

```
bernstein plan ls [--state active|completed|blocked]
bernstein plan show PLAN_ID
```

- `bernstein plan ls` - lists managed plans across all three buckets in
  a table (`State`, `Plan ID`, `Path`). `--state` filters to one bucket.
- `bernstein plan show PLAN_ID` - prints the archived (or active) plan's
  full YAML body, prefixed with `# state:` and `# path:` comment lines.
  `PLAN_ID` is the filename stem, e.g. `2026-04-23-strategic-300` for an
  archived plan or the original stem for an active one.

Both commands operate on `plans/` under the current working directory
(or wherever `default_lifecycle()` is rooted); there is no `--workdir`
flag on either subcommand.

---

## Lifecycle

Three buckets, one linear state machine:

```
plans/active/      # runs in flight or queued - writable
plans/completed/   # finished successfully - read-only
plans/blocked/     # aborted - read-only
```

Only two transitions are legal: `active -> completed` and
`active -> blocked`. Every other pair raises `PlanArchiveError`.
Archived states are terminal from the lifecycle API's perspective -
copying a file out of `completed/` or `blocked/` back into `active/` is
a manual, unmanaged operation; the lifecycle layer never does it for
you.

### Archiving a plan

`archive_completed()` / `archive_blocked()` (called by orchestrator
code, not directly by either CLI subcommand):

1. Validate the source file actually lives in `plans/active/`.
2. Prepend a rendered `## Run summary` (success) or `## Failure reason`
   (failure) Markdown block to the plan's YAML text
   (`core/planning/run_summary.py`).
3. Reserve a destination filename: `YYYY-MM-DD-<slug>.yaml`, where the
   date is UTC "today" and the slug is derived from the plan name (or
   the source stem). On a collision, a deterministic 6-hex-char SHA-256
   suffix is appended; if that also collides, an incrementing counter
   extends it further (up to 1000 attempts).
4. Fire the `pre_archive` hook (if a `HookRegistry` was wired in) -
   an exception here aborts the archive before any disk write.
5. Write the new file via a same-directory temp file + `os.replace`
   (atomic), then delete the source only after the destination is
   durable.
6. `chmod 0o444` the destination (best-effort; some filesystems ignore
   the bit - `assert_writable()` is the layer's own authoritative
   refusal, independent of the filesystem mode).
7. Record an HMAC-chained audit entry (`audit_kind` = `"success"` or
   `"failure"`), when an `AuditLog` was wired into the `PlanLifecycle`.
8. Fire the `post_archive` hook.

### Backfill

`backfill_unmanaged()` moves loose `plans/*.yaml` files (not yet in any
bucket) into `plans/active/`. It's meant to run once at orchestrator
startup and is idempotent - already-managed files are left alone, and a
name collision against an existing `active/` file causes the loose file
to be skipped (logged, not moved).

### Mutation guard

`assert_writable(path)` raises `PlanArchiveError` if `path` resolves
under `completed/` or `blocked/`, regardless of the actual filesystem
permission bit. Callers that want to defensively guard an edit call
this before writing, rather than relying on the `chmod` alone.

---

## Limitations

- `bernstein plan ls` / `bernstein plan show` are read-only query
  commands. Nothing in the current CLI surface drives
  `archive_completed()` / `archive_blocked()` directly - archival is
  triggered by orchestrator run-completion code, not by an operator
  command.
- Re-running an archived plan requires manually copying the file back
  into `plans/active/`; there is no `bernstein plan restore` or
  equivalent command.

---

## Source

- `src/bernstein/core/planning/lifecycle.py` - `PlanLifecycle`,
  `PlanState`, `ArchivedPlan`, `PlanArchiveError`, `default_lifecycle`.
- `src/bernstein/core/planning/run_summary.py` - `RunSummary` /
  `FailureSummary` rendering for the prepended archive header.
- `src/bernstein/cli/commands/plan_archive_cmd.py` - `plan_ls`,
  `plan_show`.
- `docs/reference/cli-reference.md` - flag-level reference for both
  subcommands.
