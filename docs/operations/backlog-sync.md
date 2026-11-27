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

Requires a reachable task server. If the initial task-status query cannot connect, sync records "Cannot connect to task server - is it running?" and returns before either pass; it does not continue reconciling files.

## What it does

Sync has separate creation and archival passes, with different file collections:

| Pass | Reads | Effect on files |
|---|---|---|
| Creation | `.sdd/backlog/open/*.yaml`, `.md`; `.sdd/backlog/issues/*.yaml`, `.md` | Parses each file and creates a server task only if it is not already represented. Does not move files. |
| Archive | Metadata in `.sdd/backlog/issues/`, `open/`, and `claimed/` (`*.yaml` and `*.md`) | Matches parsed metadata titles against terminal server tasks (`done`, `failed`, `cancelled`), after title normalization. For a match, moves a same-named live file from `open/` or `claimed/` to `closed/`. Files in `issues/` are metadata only and are retained. |

For example, an issue metadata file at `.sdd/backlog/issues/123.yaml` can have the same parsed title as a terminal task and guide the move of the separate live file `.sdd/backlog/open/123.yaml` into `closed/`; the issue metadata file remains in `issues/`. The archive pass also considers metadata in `open/` and `claimed/`, but only same-named live files in those two directories can be moved.

For creation, a file is considered already-synced if its normalised title or a slug derived from its filename matches an existing server task's normalised title. New tasks are submitted in one batch via `POST /tasks/batch`; if the server does not support batching (404), the command falls back to creating them one at a time via `POST /tasks`. The originating filename is appended to each task's description as `<!-- source: <file> -->` for traceability.

The command prints how many tasks were created, how many files were skipped as already-synced, how many were moved to `closed/`, and any per-file errors (unparseable file, failed HTTP call).

Note: the command's own `--help` text and its terminal summary line both say the file moves to `backlog/done/`. The directory `.sdd/backlog/done/` is in fact created by the command, but nothing is ever written into it — the real move target, per `_move_completed_files` in `core/persistence/sync.py`, is `.sdd/backlog/closed/`. Look there, not in `done/`, for files after a sync.

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
