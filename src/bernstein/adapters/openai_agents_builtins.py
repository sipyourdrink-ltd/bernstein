"""Opt-in workdir-sandboxed builtin tools for the OpenAI Agents runner.

Bernstein's default path hands the agent tools through the MCP gateway, so
every tool call is brokered, audited, and replayable through that surface.
Some runs, however, execute without an MCP gateway reachable (an isolated
worktree with no bridge, a minimal offline environment). Without any tools
such a run cannot touch the filesystem or shell at all, which is a gap in
our own surface: the operator asked the agent to do work in a workdir but
the agent has no sanctioned, audited way to act on it.

This module closes that gap with four small builtins - ``read_file``,
``write_file``, ``list_dir``, ``run_command`` - that are selected **only**
when the runner manifest sets ``tool_source: "builtin"``. The MCP-gateway
path stays the default; builtins never become the default.

Three safety properties are enforced here and covered by tests:

1. Every path argument is resolved against the run workdir. Absolute paths
   and any ``..`` escape are rejected: the real (symlink-resolved) target
   must stay inside the real workdir.
2. ``run_command`` takes an argv **list** and runs it with ``shell=False``.
   There is no shell string and no shell interpolation, so a metacharacter
   in an argument is passed to the program literally.
3. Every call is recorded to the runner's event stream (the same
   line-delimited JSON surface the MCP path uses) via an injected event
   sink, with the tool name, arguments, and outcome. A run without an MCP
   gateway therefore stays auditable and replayable.

The SDK's ``@function_tool`` decorator is applied lazily in
:func:`build_builtin_tools` so this module - and the pure helpers below -
stay importable and unit-testable without the optional ``openai-agents``
package installed.
"""

from __future__ import annotations

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
    """
    argv_list = [str(a) for a in argv]
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
    real_workdir = Path(workdir).resolve()
    try:
        # argv list + shell=False: no shell string, no interpolation.
        completed = subprocess.run(
            argv_list,
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


def build_builtin_tools(
    workdir: Path,
    emit: Callable[[dict[str, Any]], None],
) -> list[Any]:
    """Return the four builtins wrapped as SDK ``@function_tool`` objects.

    The SDK is imported lazily so this module stays importable without the
    optional ``openai-agents`` package. Each tool closes over *workdir* and
    *emit*, so the SDK-facing signatures expose only the model-supplied
    arguments while the sandbox root and audit sink stay bound here.

    Args:
        workdir: Run workdir every path is confined to.
        emit: Event sink; receives one ``tool_call`` and one
            ``tool_result`` mapping per call for the audit stream.

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

    @function_tool
    def run_command(argv: list[str]) -> str:
        """Run an argv list inside the run workdir (no shell, no interpolation)."""
        return run_command_in_workdir(workdir, argv, emit=emit)

    return [read_file, write_file, list_dir, run_command]


__all__ = [
    "BUILTIN_TOOL_NAMES",
    "WorkdirEscapeError",
    "build_builtin_tools",
    "list_dir_in_workdir",
    "read_file_in_workdir",
    "resolve_in_workdir",
    "run_command_in_workdir",
    "write_file_in_workdir",
]
