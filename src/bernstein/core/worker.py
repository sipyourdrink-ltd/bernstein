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
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

# Setup minimal logging for the worker
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("bernstein-worker")

_BASH_ERROR_RE = re.compile(r"\[(Bash|shell)\]\s+.*exited\s+with\s+code\s+([1-9]\d*)")


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


def _monitor_logs(log_path: Path, session_id: str, child: subprocess.Popen[bytes], workdir: Path) -> None:
    """Scan the agent log for tool errors and trigger aborts if needed."""
    if not log_path.exists():
        # Wait up to 5s for log to appear
        for _ in range(50):
            if log_path.exists():
                break
            time.sleep(0.1)
        else:
            return

    from bernstein.plugins.manager import get_plugin_manager

    pm = get_plugin_manager(workdir)
    last_size = 0

    while child.poll() is None:
        try:
            current_size = log_path.stat().st_size
            if current_size > last_size:
                with log_path.open("r", encoding="utf-8", errors="replace") as f:
                    f.seek(last_size)
                    new_lines = f.readlines()
                    for line in new_lines:
                        match = _BASH_ERROR_RE.search(line)
                        if match:
                            tool = match.group(1)
                            exit_code = match.group(2)
                            error_msg = f"Tool {tool} failed with exit code {exit_code}"
                            logger.warning("Sibling abort triggered: %s", error_msg)

                            # Fire hook
                            pm.fire_tool_error(session_id, tool, error_msg)

                            # Kill child process immediately to prevent sibling conflicts
                            logger.error("Killing agent %s due to Bash error in batch", session_id)
                            child.kill()
                            return
                last_size = current_size
        except Exception as exc:
            logger.debug("Log monitor error: %s", exc)
        time.sleep(0.5)


def main() -> None:
    """Entry point for bernstein-worker."""
    parser = argparse.ArgumentParser(
        description="Bernstein agent worker — wraps CLI agents for process visibility",
    )
    parser.add_argument("--role", required=True, help="Agent role (qa, backend, etc.)")
    parser.add_argument("--session", required=True, help="Session ID")
    parser.add_argument("--pid-dir", required=True, help="Directory for PID metadata files")
    parser.add_argument("--workdir", default=".", help="Project root directory")
    parser.add_argument("--log-path", help="Path to the agent log file")
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

    # 4. Start log monitor for sibling abort (T439)
    if args.log_path:
        log_path = Path(args.log_path)
        workdir = Path(args.workdir)
        monitor_thread = threading.Thread(
            target=_monitor_logs,
            args=(log_path, args.session, child, workdir),
            daemon=True,
            name="log-monitor",
        )
        monitor_thread.start()

    # 5. Forward signals to child
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
# ---------------------------------------------------------------------------
# Color-coded agent identity in all output (T562)
# ---------------------------------------------------------------------------

import sys as _sys

# ANSI color codes for agent roles
_AGENT_COLORS: dict[str, str] = {
    "manager": "\033[1;36m",  # bright cyan
    "backend": "\033[1;32m",  # bright green
    "frontend": "\033[1;33m",  # bright yellow
    "qa": "\033[1;35m",  # bright magenta
    "security": "\033[1;31m",  # bright red
    "architect": "\033[1;34m",  # bright blue
    "devops": "\033[1;37m",  # bright white
    "docs": "\033[1;90m",  # bright black (gray)
    "reviewer": "\033[1;95m",  # bright magenta
    "ml-engineer": "\033[1;96m",  # bright cyan
    "prompt-engineer": "\033[1;93m",  # bright yellow
    "retrieval": "\033[1;92m",  # bright green
    "vp": "\033[1;97m",  # bright white
    "analyst": "\033[1;94m",  # bright blue
    "resolver": "\033[1;91m",  # bright red
    "visionary": "\033[1;95m",  # bright magenta
}
_RESET = "\033[0m"


def _colorize_agent_output(role: str, session_id: str, text: str) -> str:
    """Prefix agent output with color-coded role tag (T562)."""
    color = _AGENT_COLORS.get(role, "\033[1;90m")  # default: bright gray
    tag = f"[{role}:{session_id[:8]}]"
    return f"{color}{tag}{_RESET} {text}"


def _write_colored_output(role: str, session_id: str, text: str) -> None:
    """Write color-coded output to stdout/stderr (T562)."""
    colored = _colorize_agent_output(role, session_id, text)
    _sys.stdout.write(colored)
    _sys.stdout.flush()
