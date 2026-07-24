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
bernstein watch --glob "src/**/*.py"   # only re-run on Python source changes
```

| Flag / argument | Default | Meaning |
|---|---|---|
| `DIRECTORY` (argument) | `.` | Directory to watch, recursively. |
| `--glob PATTERN` | none | Restrict triggering changes to files matching this glob (e.g. `"src/**/*.py"`). |

Requires the `watchdog` package (`pip install watchdog`); the command exits
with an install hint if it isn't present.

## How it behaves

- Watches recursively with `watchdog.observers.Observer`, reacting to
  create, modify, delete, and move events.
- Changes inside `.sdd/`, `.git/`, `__pycache__/`, `node_modules/`,
  `.tox/`, `.venv/`, `venv/`, `dist/`, `build/`, `.pytest_cache/`,
  `.mypy_cache/`, `.ruff_cache/`, and `.eggs/` are always ignored,
  regardless of `--glob`.
- Changed paths are debounced for 2 seconds: rapid successive saves collapse
  into a single re-run instead of firing one per keystroke/save.
- After the debounce window fires, the command queries the task server for
  open tasks (`GET /tasks?status=open`) and prints, per changed file, either
  "no open tasks", how many open tasks are being re-run, or up to 3 task
  IDs matched by mentioning the file's path or basename in their title or
  description (falling back to *all* open tasks if none match by name).
- It then spawns `python -m bernstein run --auto-approve` as a detached
  background subprocess (stdout/stderr discarded) — one spawn per debounce
  window, not one per matched task.
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
