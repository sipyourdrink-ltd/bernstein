"""bernstein-worker: visible process wrapper for spawned CLI agents.

Wraps any CLI agent (claude, codex, gemini, etc.) so that:
1. The process is visible in Activity Monitor / ps as "bernstein: <role> [<session>]"
2. A PID metadata file is written for `bernstein ps` to read
3. Signals are forwarded to the child process
4. Cleanup happens on exit
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def _set_proctitle(title: str) -> None:
    """Set the process title for ps / Activity Monitor."""
    try:
        import setproctitle

        setproctitle.setproctitle(title)
    except ImportError:
        pass


def _write_pid_file(pid_dir: Path, session: str, info: dict[str, object]) -> Path:
    """Write PID metadata JSON file."""
    pid_dir.mkdir(parents=True, exist_ok=True)
    pid_file = pid_dir / f"{session}.json"
    pid_file.write_text(json.dumps(info), encoding="utf-8")
    return pid_file


def main() -> None:
    """Entry point for bernstein-worker."""
    parser = argparse.ArgumentParser(
        description="Bernstein agent worker — wraps CLI agents for process visibility",
    )
    parser.add_argument("--role", required=True, help="Agent role (qa, backend, etc.)")
    parser.add_argument("--session", required=True, help="Session ID")
    parser.add_argument("--pid-dir", required=True, help="Directory for PID metadata files")
    parser.add_argument("--model", default="", help="Model name for metadata")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="CLI command to wrap")
    args = parser.parse_args()

    # Strip leading "--" separator
    cmd = args.command
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]

    if not cmd:
        print("bernstein-worker: no command specified", file=sys.stderr)
        sys.exit(1)

    # 1. Set process title
    _set_proctitle(f"bernstein: {args.role} [{args.session}]")

    # 2. Write PID metadata
    pid_file = _write_pid_file(
        Path(args.pid_dir),
        args.session,
        {
            "worker_pid": os.getpid(),
            "role": args.role,
            "session": args.session,
            "command": cmd[0],
            "model": args.model,
            "started_at": time.time(),
        },
    )

    # 3. Spawn child process (inherits our stdout/stderr/stdin)
    try:
        child = subprocess.Popen(cmd)
    except FileNotFoundError:
        print(f"bernstein-worker: command not found: {cmd[0]}", file=sys.stderr)
        pid_file.unlink(missing_ok=True)
        sys.exit(127)
    except PermissionError:
        print(f"bernstein-worker: permission denied: {cmd[0]}", file=sys.stderr)
        pid_file.unlink(missing_ok=True)
        sys.exit(126)

    # Update PID file with child PID
    try:
        info = json.loads(pid_file.read_text(encoding="utf-8"))
        info["child_pid"] = child.pid
        pid_file.write_text(json.dumps(info), encoding="utf-8")
    except OSError:
        pass

    # 4. Forward signals to child
    def _forward(signum: int, _frame: object) -> None:
        with contextlib.suppress(OSError):
            child.send_signal(signum)

    signal.signal(signal.SIGTERM, _forward)
    signal.signal(signal.SIGINT, _forward)

    # 5. Wait for child, clean up, exit
    try:
        exit_code = child.wait()
    except Exception:
        child.kill()
        exit_code = 1
    finally:
        pid_file.unlink(missing_ok=True)

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
