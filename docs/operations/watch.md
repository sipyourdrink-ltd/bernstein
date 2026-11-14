# File watch and auto re-run

`bernstein watch` watches a directory for source-file changes and
automatically re-runs open tasks against the task server when files change.
It's a local dev-loop convenience, not a trigger source: it doesn't create
or route tasks, it just kicks off `bernstein run --auto-approve` whenever
something in the watched tree is saved.

> This is a different feature from the `file_watch` trigger source adapter
> (`core/trigger_sources/file_watch.py`) used by `bernstein triggers`. The
> two are unrelated — `bernstein watch` doesn't use the trigger-source
> class, and the trigger-source class isn't wired into any running
> orchestrator loop. See [Trigger sources](trigger-sources.md).

## Usage

```bash
bernstein watch                        # watch the current directory
bernstein watch src/                   # watch a subdirectory
bernstein watch --glob "src/**/*.py"   # only react to matching changes
```

For example, when run from the repository root, `bernstein watch src/`
monitors `<repo>/src`, but the resulting `bernstein run --auto-approve`
still runs with `<repo>` as its working directory. The watched directory
selects which file changes are monitored; it does not change the working
directory of the run. `--glob` filters changed paths relative to the watched
directory (so with the default directory, `src/**/*.py` matches paths under
`src/`).

| Flag / argument | Default | Meaning |
|---|---|---|
| `DIRECTORY` (argument) | `.` | Directory to watch, recursively. |
| `--glob PATTERN` | none | Restrict monitored file changes to paths matching this glob (e.g. `"src/**/*.py"`). |

Requires the `watchdog` package (`pip install watchdog`); the command exits
with an install hint if it isn't present.

## How it behaves

- Watches recursively with `watchdog.observers.Observer`, reacting to
  create, modify, delete, and move events. For a move, the destination path
  is treated as the changed path (and is the path checked against `--glob`).
- Changes inside `.sdd/`, `.git/`, `__pycache__/`, `node_modules/`,
  `.tox/`, `.venv/`, `venv/`, `dist/`, `build/`, `.pytest_cache/`,
  `.mypy_cache/`, `.ruff_cache/`, and `.eggs/` are always ignored,
  regardless of `--glob`.
- Changed paths are debounced for 2 seconds: rapid successive saves collapse
  into a single re-run instead of firing one per keystroke/save.
- After the debounce window fires, the command queries the task server once
  for open tasks (`GET /tasks?status=open`) and prints a task-list preview for
  each changed file. If there are no open tasks, it reports that and does not
  start a run. A task is considered affected when its title or description
  mentions the file's path or basename; if none match, all open tasks are
  considered affected. The preview shows the count when all open tasks are
  affected, or up to 3 affected task IDs otherwise. With multiple changed files
  in one debounce batch, it prints one preview per file but starts only one run
  for the batch.
- The preview is informational: the run processes all open tasks, not only
  the tasks shown for a changed file. The command starts
  `python -m bernstein run --auto-approve` as a detached background subprocess
  (stdout/stderr discarded) when open tasks exist.
- Runs until `Ctrl-C`; on exit it stops and joins the underlying
  `watchdog` observer.

## Limitations

- The per-file "affected tasks" list shown in the console is informational
  only — the actual re-run always processes *all* open tasks via
  `bernstein run --auto-approve`, not just the ones whose title/description
  matched the changed file.
- No dedup across overlapping runs: if a previous
  `bernstein run --auto-approve` is still in flight when another debounce
  window fires, `watch` spawns another one regardless.

## Source

`src/bernstein/cli/commands/watch_cmd.py` (`watch_cmd`, registered as
`bernstein watch`).
