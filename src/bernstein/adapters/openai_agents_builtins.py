"""Opt-in builtin tools for the OpenAI Agents runner.

Bernstein's default path hands the agent tools through the MCP gateway, so
every tool call is brokered, audited, and replayable through that surface.
Some runs, however, execute without an MCP gateway reachable (an isolated
worktree with no bridge, a minimal offline environment). Without any tools
such a run cannot touch the filesystem or shell at all, which is a gap in
our own surface: the operator asked the agent to do work in a workdir but
the agent has no sanctioned, audited way to act on it.

This module closes that gap with a small set of builtins that are selected
**only** when the runner manifest sets ``tool_source: "builtin"``. The
MCP-gateway path stays the default; builtins never become the default.

The builtins split into two confinement tiers - do not conflate them:

* ``read_file`` / ``write_file`` / ``list_dir`` are **workdir-confined at the
  builtin layer**. A path argument may be relative (resolved against the run
  workdir) or absolute; either way it is normalized (symlinks resolved) and
  then checked for **containment** inside the real workdir - an absolute
  path is allowed *iff* it resolves inside the workdir root (this is what
  lets worker/manager heartbeat writes use the runner-issued absolute path,
  e.g. ``/workspace/.sdd/runtime/heartbeats/<id>.json``, without every
  absolute-path write being rejected outright). ``..`` traversal and any
  absolute path that resolves outside the workdir are still rejected. These
  three are always available under ``tool_source: "builtin"``.
* ``run_command`` is a **restricted process-exec primitive**, not a
  workdir-sandboxed tool. It runs a child process, and a child process can
  read and write anywhere the runner process can reach. It accepts EITHER a
  single command **string** or an **argv list**:

  - A string is executed as ``["/bin/bash", "-lc", <string>]`` - a real
    shell, so variable substitution (``$FOO``), command substitution
    (``$(...)``), pipes, redirects, and background jobs (``cmd & disown``)
    all work exactly as the model expects when it writes ordinary shell
    snippets (this is required for Bernstein's own manager-role prompt,
    which instructs agents to authenticate task-API calls with
    ``Authorization: Bearer $(cat <token-file>)``/``$BERNSTEIN_AUTH_TOKEN`` -
    a no-shell argv exec cannot expand either). ``argv[0]`` here is always
    the literal ``/bin/bash`` invoked directly by this module - it never
    passes through :func:`resolve_command`, so it is **not** subject to the
    ``argv``-list policy below by construction.
  - A list is still run with ``shell=False`` - no shell string, no
    interpolation - and ``argv[0]`` must be a bare command name resolved via
    :func:`resolve_command`: absolute paths and path separators are
    rejected (the model must pass a bare name, not a path), the survivor is
    checked against a short denylist of **genuinely dangerous** operations
    (privilege escalation, filesystem/partition destruction, system power
    state - see ``_BLOCKED_COMMANDS``), and then resolved on ``PATH`` and run
    by its resolved absolute path. Standard toolchain interpreters and
    runners (``bash``, ``sh``, ``python``, ``python3``, ``pytest``, ``node``,
    ``npm``, ``pip``, ``git``, ...) are ordinary bare commands here and are
    **not** denylisted - blocking them provided no real containment once the
    shell-string form above exists unguarded (a model that wants a shell can
    already get a full one via the string form), and blocking them made
    every Python/pytest-driven task structurally unsolvable for a
    ``run_command``-only worker (see the D2 MiniMax KILL-NOTE).

  TRUE filesystem confinement for ``run_command`` is the configured OS
  sandbox (``docker``/``e2b``/``modal``), NOT this builtin - the shell string
  form is exactly as confined (or unconfined) as the list form; the shell
  itself does not grant the child process any capability an argv list
  couldn't already reach (arbitrary binary execution) once ``run_command``
  is exposed at all. Accordingly ``run_command`` (in either form) is exposed
  only when the run has an OS sandbox provider configured, or the operator
  explicitly opts in with ``BERNSTEIN_BUILTIN_ALLOW_RUN_COMMAND=1``. Under the
  bare local/worktree path with no opt-in, ``run_command`` is withheld while
  the three file tools remain available.

  Every allow/deny decision made by :func:`resolve_command` and
  :func:`resolve_in_workdir` is logged at ``INFO`` with the exact command/path
  and the rule that fired, so a run's audit log alone explains every policy
  decision without needing to reproduce the run.

Every call is recorded to the runner's event stream (the same line-delimited
JSON surface the MCP path uses) via an injected event sink, with the tool
name, arguments, and outcome. A run without an MCP gateway therefore stays
auditable and replayable.

The SDK's ``@function_tool`` decorator is applied lazily in
:func:`build_builtin_tools` so this module - and the pure helpers below -
stay importable and unit-testable without the optional ``openai-agents``
package installed.
"""

from __future__ import annotations

import hashlib
import itertools
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

logger = logging.getLogger(__name__)

# Output byte caps so a tool call cannot flood the event stream or return an
# unbounded payload to the model. Deterministic and process-independent.
_MAX_READ_BYTES: int = 1_000_000
_MAX_CMD_OUTPUT_CHARS: int = 100_000
# Wall-clock ceiling for ``run_command`` so a runaway child cannot hang the
# runner. The runner's own timeout still bounds the whole session; this is a
# per-call floor of protection.
_RUN_COMMAND_TIMEOUT_S: float = 120.0

# Names of the builtins, exported so the runner can log which set is active.
BUILTIN_TOOL_NAMES: tuple[str, ...] = (
    "read_file",
    "write_file",
    "list_dir",
    "run_command",
)

# Bare command names rejected outright as ``argv[0]`` for the argv-LIST form
# of ``run_command``. This is intentionally short: standard toolchain
# interpreters/runners (bash, sh, python, python3, pytest, node, npm, pip,
# git, ...) are NOT here - blocking them provided no real containment (the
# shell-string form of run_command already gives the model an unrestricted
# ``/bin/bash -lc`` by design, so denylisting the same interpreters as bare
# argv[0] names in the list form was security theater that only cost every
# Python/pytest-driven worker task its full turn budget; see the D2 MiniMax
# KILL-NOTE). What remains here targets operations that are dangerous
# regardless of which process invokes them: privilege escalation, partition/
# filesystem destruction, and host power state.
_BLOCKED_COMMANDS: frozenset[str] = frozenset(
    {
        "sudo",
        "doas",
        "su",
        "shutdown",
        "reboot",
        "halt",
        "poweroff",
        "init",
        "mkfs",
        "fdisk",
        "parted",
        "dd",
        "visudo",
    }
)

# Environment opt-in that exposes ``run_command`` even without an OS sandbox
# provider. See :func:`run_command_available`.
_ALLOW_RUN_COMMAND_ENV: str = "BERNSTEIN_BUILTIN_ALLOW_RUN_COMMAND"

# Shell used to execute the single-string form of ``run_command``. ``-l``
# (login) picks up the same PATH/profile a human shell session would have;
# ``-c`` takes the command string as its sole positional argument.
_SHELL_ARGV: tuple[str, str] = ("/bin/bash", "-lc")

# Sandbox providers that give ``run_command`` real OS-level filesystem
# confinement. The bare local/worktree path (``unix_local``) does NOT, so
# ``run_command`` is not exposed under it unless the operator opts in.
_OS_SANDBOX_PROVIDERS: frozenset[str] = frozenset({"docker", "e2b", "modal"})


class CommandNotAllowedError(ValueError):
    """Raised when ``run_command`` receives an ``argv[0]`` it will not run.

    ``run_command`` (argv-list form) is a restricted process-exec primitive:
    ``argv[0]`` must be a bare command name (no path separator, not an
    absolute path), must not name one of a short list of genuinely dangerous
    operations (see ``_BLOCKED_COMMANDS``), and must resolve on the process
    ``PATH``. Standard toolchain interpreters (bash, sh, python, python3,
    pytest, node, ...) are ordinary bare commands and are not rejected here.
    This is a defense-in-depth restriction at the builtin layer; true
    filesystem confinement is the configured OS sandbox, not this check.
    """


def resolve_command(name: str) -> str:
    """Resolve a bare command *name* to an absolute executable path.

    The model may only pass a bare command name (no absolute path, no path
    separator) for the argv-LIST form of ``run_command``. This function
    rejects such malformed names, rejects a short denylist of genuinely
    dangerous operations (``_BLOCKED_COMMANDS`` - privilege escalation,
    filesystem/partition destruction, host power state), then resolves the
    survivor via :func:`shutil.which` against the process ``PATH`` and
    returns the resolved absolute path. Standard toolchain interpreters and
    runners (``bash``, ``sh``, ``python``, ``python3``, ``pytest``, ``node``,
    ``npm``, ``pip``, ``git``, ...) are deliberately NOT denylisted - see the
    module docstring for why blocking them was both ineffective (the
    shell-string form of ``run_command`` already grants an unrestricted
    shell) and actively harmful (it made Python/pytest-driven tasks
    unsolvable for a ``run_command``-only worker).

    Every decision - allow or deny - is logged at ``INFO`` with the exact
    ``argv[0]`` and the rule that fired, so the audit log alone explains why
    a command was let through or refused.

    Args:
        name: Candidate ``argv[0]`` from a tool call.

    Returns:
        The resolved absolute path to the executable.

    Raises:
        CommandNotAllowedError: *name* is empty, absolute, contains a path
            separator, names a blocked (dangerous) command, or does not
            resolve on PATH.
    """
    if not name:
        logger.info("run_command policy: DENY rule=empty_argv0 argv0=%r", name)
        msg = "empty command"
        raise CommandNotAllowedError(msg)
    candidate = Path(name)
    if candidate.is_absolute():
        logger.info("run_command policy: DENY rule=absolute_path argv0=%r", name)
        msg = f"argv[0] must be a bare command name, not an absolute path: {name!r}"
        raise CommandNotAllowedError(msg)
    if "/" in name or os.sep in name or (os.altsep and os.altsep in name):
        logger.info("run_command policy: DENY rule=path_separator argv0=%r", name)
        msg = f"argv[0] must be a bare command name without a path separator: {name!r}"
        raise CommandNotAllowedError(msg)
    if name in _BLOCKED_COMMANDS:
        logger.info("run_command policy: DENY rule=blocked_dangerous_command argv0=%r", name)
        msg = f"argv[0] names a command this policy blocks as dangerous: {name!r}"
        raise CommandNotAllowedError(msg)
    resolved = shutil.which(name)
    if resolved is None:
        logger.info("run_command policy: DENY rule=unresolvable_on_path argv0=%r", name)
        msg = f"argv[0] does not resolve to an executable on PATH: {name!r}"
        raise CommandNotAllowedError(msg)
    logger.info("run_command policy: ALLOW rule=bare_command_resolved argv0=%r resolved=%r", name, resolved)
    return resolved


def run_command_available(
    sandbox_provider: str | None,
    *,
    allow_run_command: bool | None = None,
) -> bool:
    """Return whether ``run_command`` should be exposed for this run.

    ``run_command`` is a process-exec primitive: its filesystem confinement is
    the OS sandbox, not the builtin layer. So it is exposed only when the run
    has a real OS sandbox provider configured, or when the operator explicitly
    opts in. The opt-in travels in the runner MANIFEST (``allow_run_command``,
    resolved by the spawn side) with the ``BERNSTEIN_BUILTIN_ALLOW_RUN_COMMAND=1``
    environment variable as a fallback for direct runner invocations - the
    spawner filters the subprocess environment (env_isolation), so a parent-env
    opt-in never survives into an adapter-spawned runner on its own.
    Under the bare local/worktree path with no opt-in it is withheld;
    ``read_file``/``write_file``/``list_dir`` remain available and confined.

    Args:
        sandbox_provider: The manifest sandbox provider, e.g. ``unix_local``,
            ``docker``, ``e2b``, ``modal``.
        allow_run_command: Manifest-carried opt-in decision. ``True``/``False``
            wins over the environment fallback; ``None`` means the manifest
            did not carry the field (direct invocation) and the env applies.

    Returns:
        ``True`` when ``run_command`` may be registered, ``False`` otherwise.
    """
    if sandbox_provider in _OS_SANDBOX_PROVIDERS:
        return True
    if allow_run_command is not None:
        return allow_run_command
    return os.environ.get(_ALLOW_RUN_COMMAND_ENV) == "1"


class WorkdirEscapeError(ValueError):
    """Raised when a path argument would leave the run workdir.

    Covers ``..`` traversal and absolute paths that resolve outside the
    workdir. The message names the offending path but never the resolved
    absolute target outside the workdir, so error text handed back to the
    model does not leak host layout.
    """


def resolve_in_workdir(workdir: Path, relpath: str) -> Path:
    """Resolve *relpath* against *workdir*, rejecting escapes.

    *relpath* may be relative (joined onto *workdir*) or absolute. Either
    way, the returned path is the real (symlink-resolved) location the
    caller may touch, and it is guaranteed to sit inside the real workdir -
    an absolute path is allowed **iff** it normalizes into the workdir. This
    is what lets runner-issued absolute paths (e.g. worker/manager heartbeat
    writes to ``/workspace/.sdd/runtime/heartbeats/<id>.json``) succeed
    without a blanket ban on every absolute path; escapes are still rejected
    by the same containment check that already caught ``..`` traversal.

    Rejection rule, raising :class:`WorkdirEscapeError`: the resolved real
    path is not the workdir itself and is not contained by the real workdir.
    This catches ``..`` traversal, an absolute path outside the workdir root,
    and symlinks that point outside, because containment is checked on the
    realpath, not on the lexical string.

    Args:
        workdir: The run workdir the sandbox is confined to.
        relpath: Candidate path from a tool argument - relative or absolute.

    Returns:
        The resolved absolute :class:`Path` inside the workdir.

    Raises:
        WorkdirEscapeError: The path escapes the workdir.
    """
    candidate = Path(relpath)
    real_workdir = Path(workdir).resolve()

    # ``strict=False`` so a not-yet-existing target (e.g. write_file creating
    # a new file) still resolves its parent chain; ``..`` segments collapse
    # during resolution, so an escape shows up as a real path outside
    # ``real_workdir`` below - for both the relative and the absolute case.
    if candidate.is_absolute():
        resolved = candidate.resolve()
        rule = "absolute_path"
    else:
        resolved = (real_workdir / candidate).resolve()
        rule = "relative_path"

    if resolved != real_workdir and real_workdir not in resolved.parents:
        logger.info(
            "resolve_in_workdir policy: DENY rule=%s path=%r resolved=%s workdir=%s",
            rule,
            relpath,
            resolved,
            real_workdir,
        )
        msg = f"path escapes the run workdir: {relpath!r}"
        raise WorkdirEscapeError(msg)
    logger.info(
        "resolve_in_workdir policy: ALLOW rule=%s path=%r resolved=%s",
        rule,
        relpath,
        resolved,
    )
    return resolved


def read_file_in_workdir(
    workdir: Path,
    path: str,
    *,
    emit: Callable[[dict[str, Any]], None],
) -> str:
    """Read a workdir-relative text file, recording the call.

    Emits a ``tool_call`` event before the read and a ``tool_result``
    event after, so the operation is auditable even with no MCP gateway.
    """
    emit({"type": "tool_call", "name": "read_file", "args": {"path": path}, "tool_source": "builtin"})
    try:
        target = resolve_in_workdir(workdir, path)
        data = target.read_bytes()[:_MAX_READ_BYTES]
        text = data.decode("utf-8", errors="replace")
    except (WorkdirEscapeError, OSError) as exc:
        emit(
            {
                "type": "tool_result",
                "name": "read_file",
                "tool_source": "builtin",
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return f"error: {exc}"
    emit(
        {
            "type": "tool_result",
            "name": "read_file",
            "tool_source": "builtin",
            "status": "ok",
            "bytes": len(data),
        }
    )
    return text


def write_file_in_workdir(
    workdir: Path,
    path: str,
    content: str,
    *,
    emit: Callable[[dict[str, Any]], None],
) -> str:
    """Write *content* to a workdir-relative file, recording the call.

    Parent directories inside the workdir are created as needed. The
    resolved target is re-checked for containment so a symlinked parent
    cannot redirect the write outside the workdir.
    """
    emit(
        {
            "type": "tool_call",
            "name": "write_file",
            "args": {"path": path, "bytes": len(content.encode("utf-8"))},
            "tool_source": "builtin",
        }
    )
    try:
        target = resolve_in_workdir(workdir, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Re-check after mkdir: the parent chain now exists, so resolve again
        # and confirm containment before writing.
        resolved_parent = target.parent.resolve()
        real_workdir = Path(workdir).resolve()
        if resolved_parent != real_workdir and real_workdir not in resolved_parent.parents:
            raise WorkdirEscapeError(f"path escapes the run workdir: {path!r}")
        target.write_text(content, encoding="utf-8")
    except (WorkdirEscapeError, OSError) as exc:
        emit(
            {
                "type": "tool_result",
                "name": "write_file",
                "tool_source": "builtin",
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return f"error: {exc}"
    emit(
        {
            "type": "tool_result",
            "name": "write_file",
            "tool_source": "builtin",
            "status": "ok",
            "bytes": len(content.encode("utf-8")),
        }
    )
    return f"wrote {len(content.encode('utf-8'))} bytes to {path}"


def list_dir_in_workdir(
    workdir: Path,
    path: str = ".",
    *,
    emit: Callable[[dict[str, Any]], None],
) -> str:
    """List entries of a workdir-relative directory, recording the call."""
    emit({"type": "tool_call", "name": "list_dir", "args": {"path": path}, "tool_source": "builtin"})
    try:
        target = resolve_in_workdir(workdir, path)
        entries = sorted(p.name + ("/" if p.is_dir() else "") for p in target.iterdir())
    except (WorkdirEscapeError, OSError) as exc:
        emit(
            {
                "type": "tool_result",
                "name": "list_dir",
                "tool_source": "builtin",
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return f"error: {exc}"
    emit(
        {
            "type": "tool_result",
            "name": "list_dir",
            "tool_source": "builtin",
            "status": "ok",
            "count": len(entries),
        }
    )
    return "\n".join(entries)


# ---------------------------------------------------------------------------
# run_command duplicate-invocation guard (D2 attempt-4 defect 2, 2026-07-02)
# ---------------------------------------------------------------------------
# Observed failure: on the openai_agents path against a chat-completions
# translation endpoint (meridian), the assistant message can arrive with the
# SAME tool_call duplicated - the SDK then faithfully invokes ``run_command``
# twice, concurrently, for one logical model call (the manager's
# ``bash create_tasks.sh`` ran twice and double-POSTed duplicate task pairs,
# feeding the 15-minute claim-conflict deadlock). The builtin layer itself
# has always executed exactly once per invocation; this guard makes execution
# exactly-once per logical call even when the invocation is delivered twice.
#
# Mechanism: every invocation gets a monotonic id and a sha256 command hash
# (both logged at INFO forever, so any double-delivery is visible in logs).
# If an identical command (same hash) is either still in flight or finished
# within ``_DEDUPE_WINDOW_S`` seconds, the duplicate does NOT execute - it
# waits for / reuses the original's result and logs a WARNING with both
# invocation ids. Duplicates from a duplicated tool_calls array arrive
# back-to-back (milliseconds apart, often overlapping); a legitimate model
# retry of the same command requires a full LLM round trip and lands well
# outside the window. Tunable via ``BERNSTEIN_RUN_COMMAND_DEDUPE_WINDOW_S``
# (seconds; ``0`` disables the guard entirely).
_DEDUPE_WINDOW_ENV = "BERNSTEIN_RUN_COMMAND_DEDUPE_WINDOW_S"
_DEDUPE_WINDOW_DEFAULT_S = 2.0

_invocation_ids = itertools.count(1)
_dedupe_lock = threading.Lock()


class _InvocationRecord:
    """In-flight/most-recent execution record for one command hash."""

    __slots__ = ("done", "finished_at", "invocation_id", "result")

    def __init__(self, invocation_id: int) -> None:
        self.invocation_id = invocation_id
        self.done = threading.Event()
        self.finished_at: float = 0.0
        self.result: str = ""


_recent_invocations: dict[str, _InvocationRecord] = {}


def _dedupe_window_s() -> float:
    """Return the duplicate-suppression window in seconds (0 disables)."""
    raw = os.environ.get(_DEDUPE_WINDOW_ENV)
    if raw is None:
        return _DEDUPE_WINDOW_DEFAULT_S
    try:
        return max(0.0, float(raw))
    except ValueError:
        logger.warning(
            "run_command: invalid %s=%r - using default %.1fs",
            _DEDUPE_WINDOW_ENV,
            raw,
            _DEDUPE_WINDOW_DEFAULT_S,
        )
        return _DEDUPE_WINDOW_DEFAULT_S


def _command_hash(workdir: Path, exec_argv: list[str]) -> str:
    """Stable hash identifying one logical command in one workdir."""
    payload = "\x00".join([str(workdir), *exec_argv])
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()[:16]


def _claim_or_join_invocation(
    command_hash: str,
    invocation_id: int,
    window_s: float,
) -> tuple[_InvocationRecord, bool]:
    """Register this invocation, or join an identical recent/in-flight one.

    Returns ``(record, is_duplicate)``. When ``is_duplicate`` is ``True`` the
    caller must NOT execute; it should wait on ``record.done`` and reuse
    ``record.result``.
    """
    with _dedupe_lock:
        prev = _recent_invocations.get(command_hash)
        if window_s > 0 and prev is not None:
            in_flight = not prev.done.is_set()
            fresh = prev.done.is_set() and (time.monotonic() - prev.finished_at) <= window_s
            if in_flight or fresh:
                return prev, True
        record = _InvocationRecord(invocation_id)
        _recent_invocations[command_hash] = record
        return record, False


def _run_captured(
    exec_argv: list[str],
    *,
    cwd: Path,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    """Run *exec_argv*, capturing stdout/stderr via temp files, not pipes.

    ``subprocess.run(capture_output=True)`` backs stdout/stderr with OS
    pipes. A shell-string command that backgrounds a job (``cmd & disown``,
    e.g. a heartbeat loop: ``while true; do date > hb; sleep 30; done &``)
    spawns a grandchild that inherits those pipe write-ends. A pipe only
    reaches EOF once *every* fd referencing its write end is closed - so
    ``subprocess.run``'s internal ``communicate()`` blocks reading from the
    pipe for the full lifetime of the backgrounded grandchild, even though
    the direct child (the shell) exited immediately after its foreground
    statements. ``disown`` only detaches job-control/SIGHUP tracking, not
    stdio, so it does not fix this.

    File-backed capture has no such wait: ``Popen.wait()`` (which is what
    this function ultimately drives) returns as soon as the *direct* child
    exits, regardless of what a backgrounded grandchild still holds open,
    because there is no read-until-EOF loop over a file the way there is
    over a pipe - we simply seek back and read whatever bytes exist once the
    direct child is done. ``stdin=DEVNULL`` additionally ensures a
    backgrounded child never blocks waiting on stdin input it will never
    receive.
    """
    with tempfile.TemporaryFile() as out_f, tempfile.TemporaryFile() as err_f:
        raw = subprocess.run(
            exec_argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=out_f,
            stderr=err_f,
            shell=False,
            timeout=timeout,
            check=False,
        )
        out_f.seek(0)
        err_f.seek(0)
        stdout = out_f.read().decode("utf-8", errors="replace")
        stderr = err_f.read().decode("utf-8", errors="replace")
    return subprocess.CompletedProcess(exec_argv, raw.returncode, stdout, stderr)


def run_command_in_workdir(
    workdir: Path,
    argv: Sequence[str] | str,
    *,
    emit: Callable[[dict[str, Any]], None],
) -> str:
    """Run *argv* inside the workdir, recording the call.

    Two calling forms, dispatched on the type of *argv*:

    * A **string** is executed as ``["/bin/bash", "-lc", argv]`` - a real
      shell. Variable substitution (``$FOO``), command substitution
      (``$(...)``), pipes, redirects, and background jobs (``cmd &
      disown``) all work as they would in an interactive shell. This is the
      form the manager role's task-server auth snippets need (e.g.
      ``TOKEN=$(cat <token-file>) && curl ... -H "Authorization: Bearer
      $TOKEN"``).
    * A **list** is passed straight to a subprocess call with ``shell=False``:
      there is no shell string and no shell interpolation, so metacharacters
      (``;``, ``|``, ``$(...)``, ``&&``) in an argument are handed to the
      program literally rather than interpreted by a shell. ``argv[0]`` is
      restricted to a bare command name resolved via :func:`resolve_command`:
      absolute paths and path separators are rejected, and a short denylist
      of genuinely dangerous operations (``_BLOCKED_COMMANDS``) is checked -
      standard toolchain interpreters (bash, sh, python, python3, pytest,
      node, ...) are ordinary bare commands and are **not** denylisted. The
      survivor is resolved on ``PATH`` and run by its resolved absolute path.

    Both forms capture stdout/stderr via temp files rather than OS pipes
    (see :func:`_run_captured`), so a shell-string command that backgrounds
    a job (``cmd & disown``) does not block the call for the job's lifetime.

    The child runs with ``cwd`` set to the resolved workdir in both forms.
    ``run_command`` remains a process-exec primitive whose true filesystem
    confinement is the configured OS sandbox, not this function.
    """
    invocation_id = next(_invocation_ids)
    is_shell_form = isinstance(argv, str)
    emit(
        {
            "type": "tool_call",
            "name": "run_command",
            "args": {"command": argv} if is_shell_form else {"argv": list(argv)},
            "tool_source": "builtin",
            "exec_form": "shell_string" if is_shell_form else "argv_list",
            "invocation_id": invocation_id,
        }
    )

    if is_shell_form:
        command_str = argv
        if not command_str.strip():
            logger.info(
                "run_command: exec_form=shell_string cwd=%s exit_code=n/a status=error error=%r",
                workdir,
                "empty command string",
            )
            emit(
                {
                    "type": "tool_result",
                    "name": "run_command",
                    "tool_source": "builtin",
                    "status": "error",
                    "error": "empty command string",
                }
            )
            return "error: empty command string"
        exec_argv: list[str] = [*_SHELL_ARGV, command_str]
        logged_form = f"shell_string command={command_str!r}"
    else:
        argv_list = list(argv)
        if not argv_list:
            logger.info(
                "run_command: exec_form=argv_list cwd=%s exit_code=n/a status=error error=%r",
                workdir,
                "empty argv",
            )
            emit(
                {
                    "type": "tool_result",
                    "name": "run_command",
                    "tool_source": "builtin",
                    "status": "error",
                    "error": "empty argv",
                }
            )
            return "error: empty argv"
        try:
            resolved_command = resolve_command(argv_list[0])
        except CommandNotAllowedError as exc:
            logger.info(
                "run_command: exec_form=argv_list cwd=%s argv=%r exit_code=n/a status=error error=%s: %s",
                workdir,
                argv_list,
                type(exc).__name__,
                exc,
            )
            emit(
                {
                    "type": "tool_result",
                    "name": "run_command",
                    "tool_source": "builtin",
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            return f"error: {exc}"
        # argv list + shell=False: no shell string, no interpolation.
        exec_argv = [resolved_command, *argv_list[1:]]
        logged_form = f"argv_list exec_argv={exec_argv!r}"

    real_workdir = Path(workdir).resolve()

    # Duplicate-invocation guard (see module comment above _InvocationRecord):
    # one INFO line per invocation with a monotonic id + command hash, so a
    # double-delivered tool call is diagnosable from logs alone, forever.
    command_hash = _command_hash(real_workdir, exec_argv)
    window_s = _dedupe_window_s()
    logger.info(
        "run_command invocation: id=%d command_hash=%s %s cwd=%s",
        invocation_id,
        command_hash,
        logged_form,
        real_workdir,
    )
    record, is_duplicate = _claim_or_join_invocation(command_hash, invocation_id, window_s)
    if is_duplicate:
        logger.warning(
            "run_command DUPLICATE-INVOCATION suppressed: id=%d is a duplicate of "
            "id=%d (command_hash=%s, window=%.1fs) - executing once, reusing the "
            "original result (double-delivered tool_call, see D2 defect 2)",
            invocation_id,
            record.invocation_id,
            command_hash,
            window_s,
        )
        # Bounded wait: the original is subject to _RUN_COMMAND_TIMEOUT_S, so
        # this can never hang the duplicate past that ceiling plus slack.
        finished = record.done.wait(timeout=_RUN_COMMAND_TIMEOUT_S + 30.0)
        emit(
            {
                "type": "tool_result",
                "name": "run_command",
                "tool_source": "builtin",
                "status": "ok" if finished else "error",
                "deduped": True,
                "invocation_id": invocation_id,
                "duplicate_of": record.invocation_id,
            }
        )
        if not finished:
            return "error: duplicate invocation timed out waiting for the original execution"
        return record.result

    def _finish(result: str) -> str:
        record.result = result
        record.finished_at = time.monotonic()
        record.done.set()
        return result

    try:
        completed = _run_captured(
            exec_argv,
            cwd=real_workdir,
            timeout=_RUN_COMMAND_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        logger.info(
            "run_command: %s cwd=%s exit_code=n/a status=error error=timeout after %ss stderr_tail=%r",
            logged_form,
            real_workdir,
            _RUN_COMMAND_TIMEOUT_S,
            (exc.stderr or "")[-2000:] if isinstance(exc.stderr, str) else exc.stderr,
        )
        emit(
            {
                "type": "tool_result",
                "name": "run_command",
                "tool_source": "builtin",
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "invocation_id": invocation_id,
            }
        )
        return _finish(f"error: {exc}")
    except (OSError, subprocess.SubprocessError) as exc:
        logger.info(
            "run_command: %s cwd=%s exit_code=n/a status=error error=%s: %s",
            logged_form,
            real_workdir,
            type(exc).__name__,
            exc,
        )
        emit(
            {
                "type": "tool_result",
                "name": "run_command",
                "tool_source": "builtin",
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "invocation_id": invocation_id,
            }
        )
        return _finish(f"error: {exc}")

    stdout = completed.stdout[:_MAX_CMD_OUTPUT_CHARS]
    stderr = completed.stderr[:_MAX_CMD_OUTPUT_CHARS]
    if completed.returncode == 0:
        logger.info(
            "run_command: %s cwd=%s exit_code=%d status=ok",
            logged_form,
            real_workdir,
            completed.returncode,
        )
    else:
        # Never truncate what we log to the reader's eye beyond the same cap
        # already applied to the tool-facing stderr - full stderr tail on
        # nonzero exit is the whole point of this log line.
        logger.info(
            "run_command: %s cwd=%s exit_code=%d status=nonzero stderr_tail=%r",
            logged_form,
            real_workdir,
            completed.returncode,
            stderr[-2000:],
        )
    emit(
        {
            "type": "tool_result",
            "name": "run_command",
            "tool_source": "builtin",
            "status": "ok",
            "exit_code": completed.returncode,
            "invocation_id": invocation_id,
        }
    )
    return _finish(f"exit_code={completed.returncode}\nstdout:\n{stdout}\nstderr:\n{stderr}")


def selected_builtin_names(
    sandbox_provider: str | None,
    *,
    allow_run_command: bool | None = None,
) -> tuple[str, ...]:
    """Return the builtin tool names active for this run.

    ``read_file``/``write_file``/``list_dir`` are always present. ``run_command``
    is included only when :func:`run_command_available` is satisfied (an OS
    sandbox provider, the manifest opt-in, or the env fallback). Exported so
    the runner can log exactly which set is active.
    """
    names = ["read_file", "write_file", "list_dir"]
    if run_command_available(sandbox_provider, allow_run_command=allow_run_command):
        names.append("run_command")
    return tuple(names)


def build_builtin_tools(
    workdir: Path,
    emit: Callable[[dict[str, Any]], None],
    *,
    sandbox_provider: str | None = None,
    allow_run_command: bool | None = None,
) -> list[Any]:
    """Return the workdir-confined builtins wrapped as SDK ``@function_tool``.

    The SDK is imported lazily so this module stays importable without the
    optional ``openai-agents`` package. Each tool closes over *workdir* and
    *emit*, so the SDK-facing signatures expose only the model-supplied
    arguments while the sandbox root and audit sink stay bound here.

    ``read_file``/``write_file``/``list_dir`` are always returned and stay
    workdir-confined at the builtin layer. ``run_command`` is a process-exec
    primitive whose filesystem confinement is the configured OS sandbox, so it
    is returned **only** when :func:`run_command_available` is satisfied for
    *sandbox_provider* (an OS sandbox provider, the manifest-carried
    *allow_run_command* opt-in, or the ``BERNSTEIN_BUILTIN_ALLOW_RUN_COMMAND=1``
    env fallback for direct invocations).

    Args:
        workdir: Run workdir every path is confined to.
        emit: Event sink; receives one ``tool_call`` and one
            ``tool_result`` mapping per call for the audit stream.
        sandbox_provider: The run's sandbox provider, used to gate
            ``run_command``. ``None`` behaves like the bare local path.

    Returns:
        A list of SDK tool objects ready to hand to ``Agent(tools=...)``.

    Raises:
        ImportError: The ``openai-agents`` SDK is not installed.
    """
    from agents import function_tool  # type: ignore[import-not-found]

    @function_tool
    def read_file(path: str) -> str:
        """Read a UTF-8 text file relative to the run workdir."""
        return read_file_in_workdir(workdir, path, emit=emit)

    @function_tool
    def write_file(path: str, content: str) -> str:
        """Write UTF-8 text to a file relative to the run workdir."""
        return write_file_in_workdir(workdir, path, content, emit=emit)

    @function_tool
    def list_dir(path: str = ".") -> str:
        """List entries of a directory relative to the run workdir."""
        return list_dir_in_workdir(workdir, path, emit=emit)

    tools: list[Any] = [read_file, write_file, list_dir]

    if run_command_available(sandbox_provider, allow_run_command=allow_run_command):

        @function_tool
        def run_command(argv: str | list[str]) -> str:
            """Run a command inside the run workdir.

            Pass a single command STRING to get a real shell
            (``/bin/bash -lc <string>``): variable substitution (``$FOO``),
            command substitution (``$(...)``), pipes, redirects, and
            background jobs (``cmd & disown``) all work, e.g.
            ``"TOKEN=$(cat path/to/token) && curl -H \"Authorization: Bearer $TOKEN\" ..."``.

            Pass an argv LIST (e.g. ``["echo", "hello"]``) to run without a
            shell: no interpolation, metacharacters in an argument are
            passed to the program literally, and the first element must be
            a bare command name resolvable on PATH (no absolute paths, no
            shell/interpreter names).
            """
            return run_command_in_workdir(workdir, argv, emit=emit)

        tools.append(run_command)

    return tools


__all__ = [
    "BUILTIN_TOOL_NAMES",
    "CommandNotAllowedError",
    "WorkdirEscapeError",
    "build_builtin_tools",
    "list_dir_in_workdir",
    "read_file_in_workdir",
    "resolve_command",
    "resolve_in_workdir",
    "run_command_available",
    "run_command_in_workdir",
    "selected_builtin_names",
    "write_file_in_workdir",
]
