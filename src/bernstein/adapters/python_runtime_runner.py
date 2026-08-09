"""Runner script for the generic Python-invoked agent-runtime adapter (#2959).

Executed as a child process by
:class:`bernstein.adapters.python_runtime.PythonRuntimeAdapter`. Imports the
configured runtime module, calls its entrypoint inside the task workdir, and
writes the run boundary to stdout as newline-delimited JSON.

Event schema
------------

One JSON object per line, each carrying an ``event`` key:

``{"event": "start", "prompt": str, "model": str, "workdir": str}``
    Emitted once, before the runtime is imported.
``{"event": "result", "output": str, "status": "completed"}``
    Emitted when the entrypoint returned. Process exits ``0``.
``{"event": "error", "error": str, "status": "failed"}``
    Emitted when the module cannot be imported, the entrypoint is missing or
    not callable, or the entrypoint raised. Process exits non-zero.

stdout carries only these events. Anything the configured runtime prints is
redirected to stderr, so runtime chatter cannot interleave with the JSONL.

Import path
-----------

The task workdir is appended to ``sys.path`` before the import, so a runtime
that lives in the checkout resolves. Appended, not prepended: an installed
distribution keeps precedence over a same-named file in the worktree.

Exit-status contract
--------------------

The exit status is authoritative for supervisors that do not parse the JSONL:
``0`` only when a ``result`` event was emitted, :data:`EXIT_RUNTIME_FAILURE`
for every path that emits an ``error`` event. A supervisor that only reads the
exit code therefore never mistakes a failed run for a successful one.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import json
import sys
from pathlib import Path
from typing import Any

#: Exit status used for every path that emits an ``error`` event.
EXIT_RUNTIME_FAILURE = 1


def _emit(payload: dict[str, Any]) -> None:
    """Write one JSON event to stdout and flush it."""
    print(json.dumps(payload))
    sys.stdout.flush()


def _fail(message: str) -> int:
    """Emit a terminal ``error`` event and return the failure exit status."""
    _emit({"event": "error", "error": message, "status": "failed"})
    return EXIT_RUNTIME_FAILURE


def main(argv: list[str] | None = None) -> int:
    """Drive the configured runtime entrypoint; return the process exit status."""
    parser = argparse.ArgumentParser(description="Python Agent Runtime Worker")
    parser.add_argument("--prompt", required=True, help="Task prompt")
    parser.add_argument("--model", default="gpt-4o", help="Model name")
    parser.add_argument("--workdir", required=True, help="Workdir path")
    parser.add_argument("--runtime-module", required=True, help="Module name to import")
    parser.add_argument(
        "--runtime-entrypoint",
        default="chat",
        help="Entrypoint function/method name",
    )

    args = parser.parse_args(argv)
    workdir = Path(args.workdir).resolve()

    _emit(
        {
            "event": "start",
            "prompt": args.prompt,
            "model": args.model,
            "workdir": str(workdir),
        }
    )

    # ``sys.path[0]`` is this script's directory, and the worker's PYTHONPATH
    # carries the orchestrator's own path - neither includes the task
    # worktree, so a runtime that lives in the checkout is unimportable
    # without help. Append rather than prepend: an installed distribution
    # keeps precedence, so a file in the worktree cannot shadow a real
    # package by sharing its name.
    if str(workdir) not in sys.path:
        sys.path.append(str(workdir))

    try:
        module = importlib.import_module(args.runtime_module)
    except Exception as exc:
        return _fail(f"Failed importing {args.runtime_module}: {exc}")

    entrypoint = getattr(module, args.runtime_entrypoint, None)
    if not callable(entrypoint):
        return _fail(f"Entrypoint {args.runtime_entrypoint!r} not found in {args.runtime_module!r}")

    try:
        # stdout is the protocol channel; anything the runtime prints goes to
        # stderr so it cannot interleave with - and corrupt - the JSONL.
        # ``BaseException`` rather than ``Exception``: a runtime that calls
        # ``sys.exit(0)`` would otherwise unwind past this handler and end the
        # process with status 0 having emitted no terminal event, which is the
        # exact false-success this contract exists to prevent.
        with contextlib.redirect_stdout(sys.stderr):
            result = entrypoint(prompt=args.prompt, model=args.model, workdir=workdir)
    except BaseException as exc:
        return _fail(f"Failed executing {args.runtime_module}: {type(exc).__name__}: {exc}")

    _emit({"event": "result", "output": str(result), "status": "completed"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
