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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
# Max output tokens escalation signal (T565)
# ---------------------------------------------------------------------------


def check_token_escalation(
    task_id: str,
    role: str,
    model: str,
    requested_tokens: int,
    max_allowed_tokens: int,
    escalation_reason: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Check for token escalation and signal if needed."""
    from bernstein.core.router import signal_max_tokens_escalation

    if requested_tokens > max_allowed_tokens:
        signal_max_tokens_escalation(
            task_id=task_id,
            role=role,
            model=model,
            requested_tokens=requested_tokens,
            max_allowed_tokens=max_allowed_tokens,
            escalation_reason=escalation_reason,
            metadata=metadata,
        )


# ---------------------------------------------------------------------------
# Permission denied hooks for retry hints (T570)
# ---------------------------------------------------------------------------


@dataclass
class PermissionDeniedHint:
    """Hint for handling permission denied errors."""

    pattern: str  # Regex pattern to match error messages
    suggestion: str  # Suggested fix or retry hint
    priority: int = 1  # Priority (higher = more important)
    context: dict[str, Any] = field(default_factory=lambda: {})


class PermissionDeniedHook:
    """Hook system for permission denied errors with retry hints."""

    def __init__(self):
        self.hooks: list[PermissionDeniedHint] = []
        self._register_default_hooks()

    def _register_default_hooks(self):
        """Register default permission denied patterns."""
        default_hooks = [
            PermissionDeniedHint(
                pattern=r"permission denied|access denied|permission.*denied",
                suggestion="Check file permissions and ensure the process has write access",
                priority=1,
            ),
            PermissionDeniedHint(
                pattern=r"EACCES|EACCES", suggestion="Check file permissions and ownership", priority=2
            ),
            PermissionDeniedHint(
                pattern=r"read-only filesystem|read only",
                suggestion="Filesystem is mounted as read-only. Check mount options.",
                priority=2,
            ),
            PermissionDeniedHint(
                pattern=r"operation not permitted|operation not permitted",
                suggestion="Check if the process has the required capabilities",
                priority=2,
            ),
            PermissionDeniedHint(
                pattern=r"permission.*denied.*git",
                suggestion="Check git repository permissions and SSH keys",
                priority=1,
            ),
        ]

        for hook in default_hooks:
            self.hooks.append(hook)
        self.hooks.sort(key=lambda x: x.priority, reverse=True)

    def register_hook(self, pattern: str, suggestion: str, priority: int = 1) -> None:
        """Register a new permission denied hook."""
        hook = PermissionDeniedHint(pattern=pattern, suggestion=suggestion, priority=priority)
        self.hooks.append(hook)
        # Sort by priority (higher priority first)
        self.hooks.sort(key=lambda x: x.priority, reverse=True)

    def get_hint(self, error_message: str) -> str | None:
        """Get hint for a permission denied error."""
        for hook in self.hooks:
            if re.search(hook.pattern, error_message, re.IGNORECASE):
                return hook.suggestion
        return None


# Global permission denied hook manager
_permission_hook_manager = PermissionDeniedHook()


def get_permission_hint(error_message: str) -> str | None:
    """Get a hint for a permission denied error."""
    return _permission_hook_manager.get_hint(error_message)


def register_permission_hook(pattern: str, suggestion: str, priority: int = 1) -> None:
    """Register a permission denied hook."""
    _permission_hook_manager.register_hook(pattern, suggestion, priority)
