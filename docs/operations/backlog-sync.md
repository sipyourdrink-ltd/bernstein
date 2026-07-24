# Backlog sync

`bernstein sync` reconciles the file-backed backlog under `.sdd/backlog/`
with the running task server: it creates a server task for every backlog
file that isn't tracked yet, and moves the backlog files for finished tasks
out of the way. It exists for the case where backlog `.yaml` / `.md` files
were hand-authored or edited on disk (by a human or an agent) and need to be
picked up without restarting the orchestrator.

The command is registered but hidden from `bernstein --help` (`hidden=True`
on the Click command); it is fully functional and safe to invoke directly.

## Usage

```bash
bernstein sync
bernstein sync --port 8052 --dir .
```

| Flag | Default | Meaning |
|---|---|---|
| `--port` | `8052` | Task server port. The command talks to `http://127.0.0.1:<port>`. |
| `--dir` | `.` | Project root directory (parent of `.sdd/`). |

Requires a reachable task server; if the server can't be reached the command
reports "Cannot connect to task server - is it running?" and continues
without creating tasks.

## What it does

1. Scans `.sdd/backlog/open/*.yaml`, `.sdd/backlog/open/*.md`, and
   `.sdd/backlog/issues/*.yaml` / `*.md`.
2. For each file, parses a `BacklogTask` (title, role, priority, scope,
   complexity, description) and checks it against every task already on the
   server (queried across `open`, `claimed`, `in_progress`, `done`, `failed`,
   and `cancelled` status). A file is considered already-synced if its
   normalised title *or* a slug derived from its filename matches an
   existing server task's normalised title.
3. New tasks are submitted in one batch via `POST /tasks/batch`; if the
   server doesn't support batching (404), the command falls back to
   creating them one at a time via `POST /tasks`. The originating filename
   is appended to each task's description as `<!-- source: <file> -->` for
   traceability.
4. For tasks that have reached `done`, `failed`, or `cancelled` on the
   server, the matching backlog file is moved out of
   `.sdd/backlog/open/` or `.sdd/backlog/claimed/` into
   **`.sdd/backlog/closed/`**.

   Note: the command's own `--help` text and its terminal summary line both
   say the file moves to `backlog/done/`. The directory `.sdd/backlog/done/`
   is in fact created by the command, but nothing is ever written into it —
   the real move target, per `_move_completed_files` in
   `core/persistence/sync.py`, is `.sdd/backlog/closed/`. Look there, not in
   `done/`, for files after a sync.

5. Prints a summary: how many tasks were created, how many files were
   skipped as already-synced, how many were moved to `closed/`, and any
   per-file errors (unparseable file, failed HTTP call).

## Deduplication

Matching is fuzzy on purpose, so a backlog file surviving a rename doesn't
get re-submitted as a duplicate task:

- **Title match**: the file's parsed title, lower-cased and slugified, is
  compared against every existing server task's title, slugified the same
  way.
- **Filename match**: the filename itself (numeric/priority prefixes like
  `115-` or `p0_c1_030426_feat_` stripped, extension stripped) is compared
  the same way, to catch files whose title text has since drifted from what
  the server task was created with.

## Source

`src/bernstein/cli/commands/task_cmd.py` (`sync`, hidden Click command),
`src/bernstein/core/persistence/sync.py` (`sync_backlog_to_server`,
`BacklogTask`, `SyncResult`) — reachable at runtime via the back-compat
alias `bernstein.core.sync`.
