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
  builtin layer**. Every path argument is resolved against the run workdir;
  absolute paths and any ``..`` escape are rejected, checked on the real
  (symlink-resolved) target so a symlinked parent cannot redirect the write.
  These three are always available under ``tool_source: "builtin"``.
* ``run_command`` is a **restricted process-exec primitive**, not a
  workdir-sandboxed tool. It runs a child process, and a child process can
  read and write anywhere the runner process can reach. The builtin layer
  applies defense-in-depth only: ``argv`` is a list run with ``shell=False``
  (no shell string, no interpolation), and ``argv[0]`` must be a bare command
  name - absolute paths, path separators, and known shell/interpreters
  (``sh``, ``bash``, ``python``, ``env``, ...) are rejected, and the survivor
  is resolved on ``PATH`` and run by its resolved absolute path. TRUE
  filesystem confinement for ``run_command`` is the configured OS sandbox
  (``docker``/``e2b``/``modal``), NOT this builtin. Accordingly it is exposed
  only when the run has an OS sandbox provider configured, or the operator
  explicitly opts in with ``BERNSTEIN_BUILTIN_ALLOW_RUN_COMMAND=1``. Under the
  bare local/worktree path with no opt-in, ``run_command`` is withheld while
  the three file tools remain available.

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

import os
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

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

# Shell/interpreter command names rejected as ``argv[0]`` for ``run_command``.
# A bare interpreter name resolves to a real interpreter binary that would
# re-open the door this builtin is trying to keep shut (``sh -c ...`` runs an
# arbitrary shell string; ``python -c ...`` runs arbitrary code). These are
# rejected before PATH resolution so the model cannot smuggle a shell in as a
# bare name either.
_BLOCKED_INTERPRETERS: frozenset[str] = frozenset(
    {
        "sh",
        "bash",
        "dash",
        "zsh",
        "ksh",
        "fish",
        "env",
        "python",
        "python3",
        "perl",
        "ruby",
        "node",
        "nodejs",
    }
)

# Environment opt-in that exposes ``run_command`` even without an OS sandbox
# provider. See :func:`run_command_available`.
_ALLOW_RUN_COMMAND_ENV: str = "BERNSTEIN_BUILTIN_ALLOW_RUN_COMMAND"

# Sandbox providers that give ``run_command`` real OS-level filesystem
# confinement. The bare local/worktree path (``unix_local``) does NOT, so
# ``run_command`` is not exposed under it unless the operator opts in.
_OS_SANDBOX_PROVIDERS: frozenset[str] = frozenset({"docker", "e2b", "modal"})


class CommandNotAllowedError(ValueError):
    """Raised when ``run_command`` receives an ``argv[0]`` it will not run.

    ``run_command`` is a restricted process-exec primitive: ``argv[0]`` must
    be a bare command name (no path separator, not an absolute path), must not
    be a shell/interpreter, and must resolve on the process ``PATH``. This is
    a defense-in-depth restriction at the builtin layer; true filesystem
    confinement is the configured OS sandbox, not this check.
    """


def resolve_command(name: str) -> str:
    """Resolve a bare command *name* to an absolute executable path.

    The model may only pass a bare command name. This function rejects any
    ``name`` that is an absolute path, contains a path separator (``/`` or the
    platform ``os.sep``), or names a known shell/interpreter, then resolves the
    survivor via :func:`shutil.which` against the process ``PATH`` and returns
    the resolved absolute path. Passing an already-absolute path such as
    ``/bin/sh`` is therefore rejected before it can run.

    Args:
        name: Candidate ``argv[0]`` from a tool call.

    Returns:
        The resolved absolute path to the executable.

    Raises:
        CommandNotAllowedError: *name* is empty, absolute, contains a path
            separator, is a blocked interpreter, or does not resolve on PATH.
    """
    if not name:
        msg = "empty command"
        raise CommandNotAllowedError(msg)
    candidate = Path(name)
    if candidate.is_absolute():
        msg = f"argv[0] must be a bare command name, not an absolute path: {name!r}"
        raise CommandNotAllowedError(msg)
    if "/" in name or os.sep in name or (os.altsep and os.altsep in name):
        msg = f"argv[0] must be a bare command name without a path separator: {name!r}"
        raise CommandNotAllowedError(msg)
    if name in _BLOCKED_INTERPRETERS:
        msg = f"argv[0] names a shell/interpreter, which is not allowed: {name!r}"
        raise CommandNotAllowedError(msg)
    resolved = shutil.which(name)
    if resolved is None:
        msg = f"argv[0] does not resolve to an executable on PATH: {name!r}"
        raise CommandNotAllowedError(msg)
    return resolved


def run_command_available(sandbox_provider: str | None) -> bool:
    """Return whether ``run_command`` should be exposed for this run.

    ``run_command`` is a process-exec primitive: its filesystem confinement is
    the OS sandbox, not the builtin layer. So it is exposed only when the run
    has a real OS sandbox provider configured, or when the operator explicitly
    opts in via the ``BERNSTEIN_BUILTIN_ALLOW_RUN_COMMAND=1`` environment
    variable. Under the bare local/worktree path with no opt-in it is withheld;
    ``read_file``/``write_file``/``list_dir`` remain available and confined.

    Args:
        sandbox_provider: The manifest sandbox provider, e.g. ``unix_local``,
            ``docker``, ``e2b``, ``modal``.

    Returns:
        ``True`` when ``run_command`` may be registered, ``False`` otherwise.
    """
    if sandbox_provider in _OS_SANDBOX_PROVIDERS:
        return True
    return os.environ.get(_ALLOW_RUN_COMMAND_ENV) == "1"


class WorkdirEscapeError(ValueError):
    """Raised when a path argument would leave the run workdir.

    Covers absolute paths and ``..`` traversal. The message names the
    offending path but never the resolved absolute target outside the
    workdir, so error text handed back to the model does not leak host
    layout.
    """


def resolve_in_workdir(workdir: Path, relpath: str) -> Path:
    """Resolve *relpath* against *workdir*, rejecting escapes.

    The returned path is the real (symlink-resolved) location the caller
    may touch. It is guaranteed to sit inside the real workdir.

    Rejection rules, both raising :class:`WorkdirEscapeError`:

    * ``relpath`` is absolute (``/etc/passwd``, ``C:\\...``). Builtins only
      ever address paths relative to the workdir.
    * The resolved real path is not the workdir itself and is not contained
      by the real workdir - this catches ``..`` traversal and symlinks that
      point outside, because containment is checked on the realpath, not on
      the lexical string.

    Args:
        workdir: The run workdir the sandbox is confined to.
        relpath: Candidate path from a tool argument.

    Returns:
        The resolved absolute :class:`Path` inside the workdir.

    Raises:
        WorkdirEscapeError: The path is absolute or escapes the workdir.
    """
    candidate = Path(relpath)
    if candidate.is_absolute():
        msg = f"absolute paths are not allowed: {relpath!r}"
        raise WorkdirEscapeError(msg)

    real_workdir = Path(workdir).resolve()
    # Resolve the joined path. ``strict=False`` so a not-yet-existing target
    # (e.g. write_file creating a new file) still resolves its parent chain;
    # ``..`` segments collapse during resolution, so an escape shows up as a
    # real path outside ``real_workdir`` below.
    resolved = (real_workdir / candidate).resolve()

    if resolved != real_workdir and real_workdir not in resolved.parents:
        msg = f"path escapes the run workdir: {relpath!r}"
        raise WorkdirEscapeError(msg)
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


def run_command_in_workdir(
    workdir: Path,
    argv: Sequence[str],
    *,
    emit: Callable[[dict[str, Any]], None],
) -> str:
    """Run *argv* inside the workdir with ``shell=False``, recording the call.

    ``argv`` is a list passed straight to :func:`subprocess.run` with
    ``shell=False``: there is no shell string and no shell interpolation,
    so metacharacters (``;``, ``|``, ``$(...)``, ``&&``) in an argument are
    handed to the program literally rather than interpreted by a shell. The
    child runs with ``cwd`` set to the resolved workdir.

    ``argv[0]`` is restricted to a bare command name resolved via
    :func:`resolve_command`: absolute paths, path separators, and known
    shell/interpreters are rejected, and the survivor is resolved on ``PATH``
    and run by its resolved absolute path. This is a defense-in-depth measure
    at the builtin layer; ``run_command`` remains a process-exec primitive
    whose filesystem confinement is the configured OS sandbox, not this check.
    """
    argv_list = list(argv)
    emit(
        {
            "type": "tool_call",
            "name": "run_command",
            "args": {"argv": argv_list},
            "tool_source": "builtin",
        }
    )
    if not argv_list:
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
    real_workdir = Path(workdir).resolve()
    exec_argv = [resolved_command, *argv_list[1:]]
    try:
        # argv list + shell=False: no shell string, no interpolation.
        completed = subprocess.run(
            exec_argv,
            cwd=real_workdir,
            capture_output=True,
            text=True,
            shell=False,
            timeout=_RUN_COMMAND_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
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
    stdout = completed.stdout[:_MAX_CMD_OUTPUT_CHARS]
    stderr = completed.stderr[:_MAX_CMD_OUTPUT_CHARS]
    emit(
        {
            "type": "tool_result",
            "name": "run_command",
            "tool_source": "builtin",
            "status": "ok",
            "exit_code": completed.returncode,
        }
    )
    return f"exit_code={completed.returncode}\nstdout:\n{stdout}\nstderr:\n{stderr}"


def selected_builtin_names(sandbox_provider: str | None) -> tuple[str, ...]:
    """Return the builtin tool names active for this run.

    ``read_file``/``write_file``/``list_dir`` are always present. ``run_command``
    is included only when :func:`run_command_available` is satisfied (an OS
    sandbox provider or the explicit opt-in). Exported so the runner can log
    exactly which set is active.
    """
    names = ["read_file", "write_file", "list_dir"]
    if run_command_available(sandbox_provider):
        names.append("run_command")
    return tuple(names)


def build_builtin_tools(
    workdir: Path,
    emit: Callable[[dict[str, Any]], None],
    *,
    sandbox_provider: str | None = None,
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
    *sandbox_provider* (an OS sandbox provider, or the explicit
    ``BERNSTEIN_BUILTIN_ALLOW_RUN_COMMAND=1`` opt-in).

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

    if run_command_available(sandbox_provider):

        @function_tool
        def run_command(argv: list[str]) -> str:
            """Run a bare-name command inside the run workdir (no shell)."""
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
