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
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

# Setup minimal logging for the worker
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("bernstein-worker")

_BASH_ERROR_RE = re.compile(r"\[(Bash|shell)\][ \t]+[^\n]{0,500}exited[ \t]+with[ \t]+code[ \t]+([1-9]\d*)")

# Session IDs must be safe for use as filenames (no path separators or traversal).
_SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9_.-]+$")

# Valid tool-abort policy values (used by --tool-abort-policy CLI arg).
# contain  → write TOOL_ABORT signal only; agent session continues.
# sibling  → write TOOL_ABORT + send SHUTDOWN to sibling agents.
# session  → write TOOL_ABORT + kill this agent session immediately.
_TOOL_ABORT_POLICIES = ("contain", "sibling", "session")

# Grace window before forwarding SIGTERM/SIGINT to the child. The child
# shares the worker's process group, so a group-directed signal (Ctrl-C,
# ``os.killpg``, the manager's kill paths) is already delivered to the
# child by the kernel. The worker forwards only when the child is still
# running after this window, i.e. the signal was addressed to the worker
# alone and forwarding is the only delivery path.
_FORWARD_GRACE_S = 0.2
_FORWARD_POLL_INTERVAL_S = 0.01


# Windows batch-shim extensions. A ``.cmd``/``.bat`` file is not a PE
# executable, so ``CreateProcess`` (what ``subprocess.Popen`` calls with a
# non-shell argv) cannot launch it even with the full path -- it must be
# routed through ``cmd.exe /c``. Real ``.exe`` targets are launched directly.
_WINDOWS_SHIM_SUFFIXES = (".cmd", ".bat")


def _resolve_launch_cmd(cmd: list[str]) -> list[str]:
    """Resolve ``cmd[0]`` to a launchable form, cross-platform.

    Two problems this solves, both Windows-only in effect:

    1. ``CreateProcess`` (used by ``subprocess.Popen`` without ``shell=True``)
       does not consult ``PATHEXT`` for ``argv[0]``. A bare ``"codex"`` never
       resolves to ``codex.cmd`` and raises ``FileNotFoundError`` even though
       ``shutil.which("codex")`` finds it. We resolve the bare name to its
       absolute path via ``shutil.which`` so ``CreateProcess`` gets a concrete
       target.
    2. Even with the full path, ``CreateProcess`` cannot execute a
       ``.cmd``/``.bat`` batch shim (they are not PE binaries). On Windows we
       wrap those as ``["cmd.exe", "/c", resolved, *rest]``; ``cmd.exe``
       propagates the child's exit code, so the worker's ``128 + N`` exit
       translation and signal forwarding keep working on ``child.wait()``.

    Behaviour is a no-op on POSIX for anything that already worked: a bare
    name that ``shutil.which`` resolves becomes its absolute path (which spawns
    identically to the bare name once found on ``PATH``), and a name that does
    not resolve is returned unchanged so the existing ``FileNotFoundError`` ->
    "command not found" path in :func:`main` still fires. Absolute or
    path-qualified ``cmd[0]`` values are left untouched.

    Args:
        cmd: The command argv. Must be non-empty.

    Returns:
        A new argv list ready to hand to ``subprocess.Popen`` with no shell.
    """
    if not cmd:
        return cmd

    first = cmd[0]
    rest = cmd[1:]

    # Only resolve a bare program name (no path separator). An operator or
    # adapter that already passed an absolute/relative path knows what it
    # wants; do not second-guess it.
    if os.sep in first or (os.altsep and os.altsep in first):
        return cmd

    resolved = shutil.which(first)
    if resolved is None:
        # Leave the bare name so the caller's FileNotFoundError handler still
        # produces the "command not found" diagnostic and the 127 exit code.
        return cmd

    # On Windows, batch shims (.cmd/.bat) cannot be launched by CreateProcess
    # directly; route them through cmd.exe. Real .exe (and every POSIX target)
    # spawns fine from its absolute path.
    if os.name == "nt" and resolved.lower().endswith(_WINDOWS_SHIM_SUFFIXES):
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        return [comspec, "/c", resolved, *rest]

    return [resolved, *rest]


def _set_proctitle(title: str) -> None:
    """Set the process title for ps / Activity Monitor."""
    with contextlib.suppress(ImportError):
        import setproctitle

        setproctitle.setproctitle(title)


def _atomic_write_json(path: Path, info: dict[str, object]) -> None:
    """Write ``info`` as JSON to *path* atomically (crash-safe, fsync-backed).

    Delegates to the audited persistence helper, which writes to a sibling
    temp file and renames via ``os.replace``. A reaper or supervisor reading the
    pid file concurrently therefore always sees either the complete old bytes or
    the complete new bytes, never a truncated half-written mix.
    """
    from bernstein.core.persistence.atomic_write import write_atomic_json

    write_atomic_json(path, info, indent=None)


def _write_pid_file(
    pid_dir: Path,
    session: str,
    info: dict[str, object],
    on_resolved: Callable[[Path], None] | None = None,
) -> Path:
    """Write PID metadata JSON file.

    ``on_resolved`` is invoked with the resolved target path *before* the file
    is created, so a caller can register the path with its signal handler and
    guarantee the file can never exist without a cleanup path (#2341).
    """
    pid_dir.mkdir(parents=True, exist_ok=True)
    pid_file = (pid_dir / f"{session}.json").resolve()
    if not pid_file.is_relative_to(pid_dir.resolve()):
        print(f"bernstein-worker: invalid session id: {session}", file=sys.stderr)
        sys.exit(1)
    if on_resolved is not None:
        on_resolved(pid_file)
    _atomic_write_json(pid_file, info)
    return pid_file


def _wait_for_log(log_path: Path) -> bool:
    """Wait up to 5s for the log file to appear. Returns True if found."""
    if log_path.exists():
        return True
    for _ in range(50):
        if log_path.exists():
            return True
        time.sleep(0.1)
    return False


def _build_abort_policy(tool_abort_policy: str) -> Any:
    """Build an AbortPolicy from the requested level string."""
    from bernstein.core.abort_chain import AbortPolicy

    if tool_abort_policy == "sibling":
        return AbortPolicy(tool_to_sibling=True, sibling_to_session=False)
    # "session" and "contain" both use no sibling cascading
    return AbortPolicy(tool_to_sibling=False, sibling_to_session=False)


def _handle_tool_error(
    line_match: re.Match[str],
    *,
    session_id: str,
    child: subprocess.Popen[bytes],
    tool_abort_policy: str,
    pm: Any,
    abort_chain: Any,
    policy: Any,
) -> bool:
    """Process a single tool error match. Returns True if session was killed."""
    from bernstein.core.abort_chain import AbortScope

    tool = line_match.group(1)
    exit_code_str = line_match.group(2)
    error_msg = f"Tool {tool} failed with exit code {exit_code_str}"
    logger.warning("Tool error detected (policy=%s): %s", tool_abort_policy, error_msg)

    pm.fire_tool_error(session_id, tool, error_msg)

    cascaded = abort_chain.abort_tool(
        session_id,
        tool,
        error_msg,
        policy=policy if tool_abort_policy == "sibling" else None,
    )
    if cascaded:
        logger.info(
            "Sibling abort: sent SHUTDOWN to %d sibling(s): %s",
            len(cascaded),
            ", ".join(cascaded),
        )

    if tool_abort_policy == "session":
        logger.error(
            "Session abort: killing agent %s due to tool error (scope=%s)",
            session_id,
            AbortScope.SESSION,
        )
        child.kill()
        return True

    logger.info("Tool abort contained at scope=%s for session %s", tool_abort_policy, session_id)
    return False


def _monitor_logs(
    log_path: Path,
    session_id: str,
    child: subprocess.Popen[bytes],
    workdir: Path,
    *,
    tool_abort_policy: str = "session",
) -> None:
    """Scan the agent log for tool errors and apply the per-tool abort policy.

    Three policy levels control what happens when a tool failure is detected:

    * ``"contain"`` -- Write a ``TOOL_ABORT`` signal and let the agent decide
      whether to retry or skip; the session process is *not* killed.
    * ``"sibling"`` -- Write a ``TOOL_ABORT`` signal *and* send ``SHUTDOWN`` to
      sibling agents (agents that share the same parent in the abort chain)
      without killing this session.
    * ``"session"`` -- Write a ``TOOL_ABORT`` signal *and* kill this agent
      session immediately (legacy behaviour, the default).

    Args:
        log_path: Path to the agent's log file.
        session_id: This session's ID (used for signal file paths).
        child: The spawned agent subprocess to optionally kill.
        workdir: Project root for plugin manager initialisation.
        tool_abort_policy: One of ``"contain"``, ``"sibling"``, or ``"session"``.
    """
    if not _wait_for_log(log_path):
        return

    from bernstein.core.abort_chain import AbortChain
    from bernstein.plugins.manager import get_plugin_manager

    pm = get_plugin_manager(workdir)
    signals_dir = workdir / ".sdd" / "runtime" / "signals"
    abort_chain = AbortChain(signals_dir=signals_dir)
    policy = _build_abort_policy(tool_abort_policy)

    last_size = 0

    while child.poll() is None:
        try:
            current_size = log_path.stat().st_size
            if current_size > last_size:
                with log_path.open("r", encoding="utf-8", errors="replace") as f:
                    f.seek(last_size)
                    for line in f:
                        m = _BASH_ERROR_RE.search(line)
                        if m and _handle_tool_error(
                            m,
                            session_id=session_id,
                            child=child,
                            tool_abort_policy=tool_abort_policy,
                            pm=pm,
                            abort_chain=abort_chain,
                            policy=policy,
                        ):
                            return
                last_size = current_size
        except Exception as exc:
            logger.debug("Log monitor error: %s", exc)
        time.sleep(0.5)


def main() -> None:
    """Entry point for bernstein-worker."""
    parser = argparse.ArgumentParser(
        description="Bernstein agent worker - wraps CLI agents for process visibility",
    )
    parser.add_argument("--role", required=True, help="Agent role (qa, backend, etc.)")
    parser.add_argument("--session", required=True, help="Session ID")
    parser.add_argument("--pid-dir", required=True, help="Directory for PID metadata files")
    parser.add_argument("--workdir", default=".", help="Project root directory")
    parser.add_argument("--log-path", help="Path to the agent log file")
    parser.add_argument("--model", default="", help="Model name for metadata")
    parser.add_argument(
        "--tool-abort-policy",
        default="session",
        choices=_TOOL_ABORT_POLICIES,
        help=(
            "Per-tool abort scope: 'contain' (write TOOL_ABORT only), "
            "'sibling' (write TOOL_ABORT + abort siblings), "
            "'session' (write TOOL_ABORT + kill this session). "
            "Default: session."
        ),
    )
    parser.add_argument("command", nargs=argparse.REMAINDER, help="CLI command to wrap")
    args = parser.parse_args()

    # Strip leading "--" separator
    cmd = args.command
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]

    if not cmd:
        print("bernstein-worker: no command specified", file=sys.stderr)
        sys.exit(1)

    # Validate session ID to prevent path traversal (session is used in filenames)
    if not _SESSION_ID_RE.fullmatch(args.session):
        print(f"bernstein-worker: invalid session id: {args.session}", file=sys.stderr)
        sys.exit(1)

    # 1. Set process title
    _set_proctitle(f"bernstein: {args.role} [{args.session}]")

    # Opt-in operator observability (spec 2026-05-17).  Emits only the
    # bare command name; fail-closed - never raises into the worker.
    with contextlib.suppress(Exception):
        from bernstein.core.telemetry.wire import emit_command_invoked

        emit_command_invoked(name_only=cmd[0])

    # 2. Install the terminating-signal handler *before* the PID file exists,
    # so the file can never outlive an early SIGTERM.
    #
    # A reaper keys on the ``child_pid`` marker and sends SIGTERM as soon as
    # it appears. The handler used to be installed only after the child spawn;
    # a SIGTERM landing between the PID-file write and that install took the
    # default disposition - terminate immediately, skipping the ``finally``
    # unlink - and leaked the PID file. ``bernstein ps`` then reported a
    # phantom worker and cleanup-order assertions flaked on loaded CI shards
    # (#2341). Installing here closes the window completely: there is no
    # instant at which the PID file exists without a cleanup-capable handler.
    #
    # Neither the PID file nor the child exists yet, so the handler reads both
    # through mutable holders that the code below fills in. Until the child
    # exists the handler just unlinks the PID file (if written) and exits;
    # once the child exists it forwards the signal with the same grace-window
    # guard and lets ``main`` reap the child and run the shared cleanup.
    _pid_file_holder: list[Path] = []
    _child_holder: list[subprocess.Popen[bytes]] = []

    def _terminate(signum: int, _frame: object) -> None:
        _running = _child_holder[0] if _child_holder else None
        if _running is not None:
            # Group-directed signals reach the child directly (shared process
            # group), so re-sending immediately would double-deliver. The
            # second delivery is not benign: when the child's own handler has
            # already run and its interpreter is finalising, the default
            # disposition is restored and the late duplicate kills the child
            # outright, so ``child.wait()`` reports a signal death (``-N``)
            # instead of the handler's exit code. Poll for the child's exit
            # through a short grace window first; forward only if it is still
            # running, i.e. the signal was sent to the worker alone.
            deadline = time.monotonic() + _FORWARD_GRACE_S
            while time.monotonic() < deadline:
                if _running.poll() is not None:
                    break
                time.sleep(_FORWARD_POLL_INTERVAL_S)
            else:
                with contextlib.suppress(OSError):
                    _running.send_signal(signum)
            # Let the normal ``child.wait()`` path in main() reap the child and
            # run the shared cleanup + ``128 + N`` exit translation.
            return
        # No child yet: nothing to forward or wait on. Clean up the PID file
        # (if it was already written) and exit with the conventional 128 + N.
        if _pid_file_holder:
            _pid_file_holder[0].unlink(missing_ok=True)
        sys.exit(128 + signum)

    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGINT, _terminate)

    # 2b. Write PID metadata. ``on_resolved`` publishes the path to the holder
    # *before* the file is created, so even a SIGTERM during the write itself
    # is cleaned up (``unlink(missing_ok=True)`` tolerates the pre-create case).
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
        on_resolved=_pid_file_holder.append,
    )

    # 2c. Touch heartbeat file so the agent starts with a fresh timestamp.
    # Without this, idle recycling can kill agents before their first
    # stream-json event arrives (e.g. Claude Code thinking for 2+ minutes).
    with contextlib.suppress(OSError):
        hb_dir = Path(args.workdir) / ".sdd" / "runtime" / "heartbeats"
        hb_dir.mkdir(parents=True, exist_ok=True)
        (hb_dir / args.session).touch()

    # 3. Spawn child process (inherits our stdout/stderr/stdin).
    #
    # Resolve argv[0] first so a bare program name that only exists as a
    # Windows batch shim (e.g. nvm4w installs codex as codex.cmd) is launched
    # via its absolute path -- and, for .cmd/.bat, through cmd.exe -- rather
    # than failing in CreateProcess. ``cmd[0]`` is preserved for the
    # command-not-found diagnostic below.
    launch_cmd = _resolve_launch_cmd(cmd)
    try:
        # launch_cmd is an argv list (never a shell string) built from the
        # trusted adapter command; shell=False, so there is no shell to inject
        # into. The Windows cmd.exe /c wrapper also passes each arg as a
        # separate list element, not a concatenated command line.
        child = subprocess.Popen(launch_cmd)  # nosemgrep
        # Publish the child so the already-installed terminating-signal
        # handler forwards to it (and lets main() reap it) instead of
        # unlinking + exiting on its own.
        _child_holder.append(child)
    except FileNotFoundError as exc:
        # Typed first-run error: the adapter binary is missing from PATH.
        # Callers running this worker as a subprocess can categorise via
        # the exit code; standalone invocations still see the plain
        # message and a sysexits-compatible code (EX_UNAVAILABLE = 69).
        from bernstein.core.errors import (
            BernsteinFirstRunError,
            ErrorCategory,
        )

        print(f"bernstein-worker: command not found: {cmd[0]}", file=sys.stderr)
        pid_file.unlink(missing_ok=True)
        _ = BernsteinFirstRunError(
            f"adapter binary not found: {cmd[0]}",
            category=ErrorCategory.DEPENDENCY_MISSING,
            context={"adapter": cmd[0]},
        )
        # Keep the legacy ``127`` exit code so external supervisors that
        # already key on it continue to work; sysexits remap is left to
        # the parent CLI guard.
        del exc
        sys.exit(127)
    except PermissionError as exc:
        from bernstein.core.errors import BernsteinFirstRunError, ErrorCategory

        print(f"bernstein-worker: permission denied: {cmd[0]}", file=sys.stderr)
        pid_file.unlink(missing_ok=True)
        _ = BernsteinFirstRunError(
            f"permission denied executing: {cmd[0]}",
            category=ErrorCategory.PERMISSION_DENIED,
            context={"adapter": cmd[0], "path": cmd[0]},
        )
        del exc
        sys.exit(126)

    # Update PID file with child PID
    # Validate pid_file stays within pid_dir to prevent path traversal (S2083)
    with contextlib.suppress(OSError):
        resolved_pid = pid_file.resolve()
        resolved_dir = Path(args.pid_dir).resolve()
        if not resolved_pid.is_relative_to(resolved_dir):
            print("bernstein-worker: pid file escaped pid-dir", file=sys.stderr)
            sys.exit(1)
        info = json.loads(pid_file.read_text(encoding="utf-8"))
        info["child_pid"] = child.pid
        _atomic_write_json(pid_file, info)

    # 4. Start log monitor for hierarchical abort (T442)
    if args.log_path:
        log_path = Path(args.log_path)
        workdir = Path(args.workdir)
        monitor_thread = threading.Thread(
            target=_monitor_logs,
            args=(log_path, args.session, child, workdir),
            kwargs={"tool_abort_policy": args.tool_abort_policy},
            daemon=True,
            name="log-monitor",
        )
        monitor_thread.start()

    # 5. Wait for child, clean up, exit.
    #
    # Terminating signals are already handled by ``_terminate`` (installed
    # before the PID file was even written); now that the child is published
    # the handler forwards to it and returns here so the shared cleanup below
    # runs exactly once. ``child.wait()`` is interruptible: a SIGTERM that
    # arrives here raises nothing - the handler forwards to the child, the
    # child exits, and ``wait`` returns its code.
    try:
        exit_code = child.wait()
    except Exception:
        child.kill()
        exit_code = 1
    finally:
        pid_file.unlink(missing_ok=True)

    # Translate signal-termination into the conventional ``128 + N`` form
    # so external supervisors that key on standard codes see a stable
    # value. ``Popen.wait`` returns ``-N`` when the child was killed by
    # signal N; passing that through ``sys.exit`` clamps it to
    # ``256 - N`` (e.g. SIGTERM -> 241), which supervisors then misread
    # as "unknown failure". ``128 + N`` is the de facto convention used
    # by bash, sh, and every shell-style runner.
    if exit_code < 0:
        exit_code = 128 + (-exit_code)

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
    context: dict[str, Any] = field(default_factory=dict)


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
