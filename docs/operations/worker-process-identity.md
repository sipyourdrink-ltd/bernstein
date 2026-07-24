# Worker process identity

Every `bernstein-worker` process sets its own OS-level process title, so it
shows up in `ps`, `top`, or Activity Monitor as `bernstein: <role>
[<session>]` instead of a generic `python` command line. This makes it
possible to tell which worker is running which role directly from the OS
process list, without going through `bernstein status`.

## How it works

`bernstein-worker` is the console-script entry point for a single spawned
agent worker (`bernstein.core.worker:main`, aliased to
`bernstein.core.orchestration.worker:main`). As the first step after
argument parsing, `main()` calls `_set_proctitle()`:

```python
_set_proctitle(f"bernstein: {args.role} [{args.session}]")
```

`_set_proctitle()` sets the process title via the `setproctitle` package:

```python
def _set_proctitle(title: str) -> None:
    """Set the process title for ps / Activity Monitor."""
    with contextlib.suppress(ImportError):
        import setproctitle

        setproctitle.setproctitle(title)
```

`setproctitle` is a core Bernstein dependency, so the title is set on every
normal install. The `ImportError` guard is defensive only: it keeps a
worker from crashing on an incomplete or broken install rather than
signalling that the feature is optional. `--role` and `--session` are the
same arguments `bernstein-worker` was invoked with; `--session` is
validated against `^[a-zA-Z0-9_.-]+$` before it reaches the title (the
worker refuses to start otherwise), so the process title can't be used to
inject arbitrary characters into `ps` output.

## What you see

```bash
$ ps aux | grep 'bernstein:'
user   41213  0.3  0.4  ...  bernstein: qa [a1b2c3d4]
user   41220  0.2  0.4  ...  bernstein: backend [a1b2c3d4]
```

The title is `bernstein: {role} [{session}]` — role is the agent role
(`qa`, `backend`, `security`, ...) and session is the session ID shared by
every worker in that run.

## Limitations

- The title is set once, at worker startup, from the `--role` and
  `--session` arguments the process was launched with. It does not update
  if the worker's role or session context changes later in its lifetime.
- If `setproctitle` cannot be imported, the process keeps the default
  interpreter-derived title silently — no warning is logged.

## Source

`src/bernstein/core/orchestration/worker.py` — `_set_proctitle()`, called
from `main()` (the `bernstein-worker` entry point).
