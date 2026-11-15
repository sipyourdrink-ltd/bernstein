# On-disk state migrations

Audience: operators upgrading Bernstein across versions, and contributors
who change the shape of anything Bernstein persists under `.sdd/`.

## Overview

Everything Bernstein writes to disk lives under the project `.sdd/`
directory: the `.sdd/runtime/` tree, SQLite stores, and JSON state files.
When the on-disk shape of any of these changes, older state must be
upgraded so a newer build can read it.

Historically that upgrade logic lived inline in the modules that read the
data: each call site that touched a session record, a backlog entry, or a
SQLite table carried a small "if this looks like an old shape, upgrade it"
branch. Those branches accumulate, never get deleted, and silently break
when shapes change again.

The migrations package replaces those scattered branches with numbered,
ordered, idempotent migration modules and a single version stamp.

Source:

- `src/bernstein/core/persistence/migrations/` - the package
- `src/bernstein/core/persistence/migrations/runner.py` - discovery and execution
- `src/bernstein/cli/commands/doctor/migrations.py` - the `bernstein doctor migrations` surface

## The version stamp

`.sdd/.schema_version` is a plain-text file holding a single integer: the
highest migration version applied to that state directory.

- A missing stamp reads as `0` and means a fresh, unmigrated install.
- An unparseable stamp also reads as `0`. Migrations are idempotent, so
  re-running forward over already-correct state is safe.
- The stamp is written atomically (temp + fsync + rename) so a reader never
  observes a torn value.

## When migrations run

Migrations run offline, at startup, on load. `ensure_sdd()` (the workspace
bootstrap that runs before the server starts) calls the runner after it has
created the `.sdd/` layout. The runner:

1. Discovers every `vNNN_*` module in the package.
2. Reads the current stamp.
3. Applies every migration whose version is greater than the stamp, in
   ascending order.
4. Advances the stamp after each successful step.

If a migration raises, the stamp stays at the last cleanly applied version
rather than half-advancing, and startup logs the failure without crashing.

## Idempotency and exit codes

Re-running `migrate()` on an up-to-date install does no work. Each
migration's `apply` is also required to be idempotent on its own, so a
partial previous run is safe to repeat.

The runner exposes documented exit codes:

| Constant              | Code | Meaning                                              |
| --------------------- | ---- | ---------------------------------------------------- |
| `EXIT_APPLIED`        | `0`  | One or more migrations were applied.                 |
| `EXIT_NOOP`           | `0`  | Nothing pending; idempotent no-op.                   |
| `EXIT_OK`             | `0`  | Alias of the above success codes.                    |
| `EXIT_FUTURE_VERSION` | `3`  | Stamp is newer than this build knows; refused.       |

`bernstein doctor migrations --apply` returns `3` when the state directory
was written by a newer build (see Rollback story below).

## Inspecting state

```
bernstein doctor migrations          # list applied and pending migrations
bernstein doctor migrations --json   # machine-readable output
bernstein doctor migrations --apply  # run pending migrations forward
```

The command reports the current schema version (the on-disk stamp), the
latest version this build knows about, and a table splitting known migrations
into applied and pending. `--json` emits the same post-operation snapshot as
`schema_version`, `latest_version`, `applied`, and `pending`. With no
`--apply`, this is read-only: it does not run migrations. With `--apply`, the
command runs pending migrations first, then reports the resulting stamp and
migration lists. After a successful apply, `schema_version` is the last version
applied (normally the latest known version) and `pending` is empty.

A failed step has a different outcome from that normal table or JSON snapshot.
The runner records an in-memory report with the failing exception text in
`error`, the versions still to run in `pending` (including the failed step),
`exit_code` 1, and `to_version` at the last successfully stamped version. Its
`applied` list contains steps completed in this run. The runner then re-raises
the original exception; it does not return that report. Consequently,
`bernstein doctor migrations --apply` does not print the normal table or JSON
snapshot for a failed step. The stamp stays at the last successful version,
so a later read-only invocation shows the remaining known versions as pending.

If the stamp is newer than this build knows, the read-only command still just
reports that stamp and the versions known to this build; it does not try to
migrate. `--apply` refuses to proceed, prints the future-version error and
exits with code `3` (without printing the normal table or JSON snapshot). Do
not edit the stamp to work around this; use a build that understands the state.

## Adding a migration

1. Create `src/bernstein/core/persistence/migrations/vNNN_<description>.py`
   where `NNN` is the next integer version (zero-padded) and `<description>`
   is snake_case.
2. Expose either:
   - top-level `VERSION`, `DESCRIPTION`, `apply(state_dir)` and optional
     `down(state_dir)` callables; or
   - a module-level `MIGRATION` instance of `Migration` for complex steps.
3. Make `apply` idempotent: running it twice on the same state must equal
   running it once. Tolerate missing files and already-migrated shapes.
4. Add a test under `tests/unit/persistence/test_migrations.py` covering the
   forward transform and its idempotency.

The first migration (`v001_baseline`) encodes the pre-migrations shape: its
`apply` is a no-op that establishes the baseline marker. Every shape the
codebase produced before migrations existed is, by definition, version 1.

## Rollback story

Migrations are forward-first. Each module ships a `down(state_dir)` for
symmetry, but for forward-only changes `down` is a documented no-op stub.

Downgrading the binary below the version that wrote the state is not
supported. The older build cannot know how to read shapes a newer build
produced, so the runner refuses to touch a state directory whose stamp is
newer than the latest migration it knows about: it raises
`FutureSchemaVersionError` and `bernstein doctor migrations --apply` exits
`3`. The remedy is to upgrade Bernstein back to a build that understands the
state, not to mutate the stamp by hand.

To recover a known-good earlier state, restore the `.sdd/` directory from a
backup taken before the upgrade rather than running migrations in reverse.

## Out of scope

- Online migrations with concurrent writers. Migrations assume an offline
  startup window with no other process writing the state directory.
- Cross-host state migration tooling.
