"""Containment barrier for the MCP shutdown-signal path.

``bernstein_shutdown_orchestrator`` takes a ``workdir`` from the caller and turns it into a
``.sdd/runtime/signals/SHUTDOWN`` write. With no barrier that is an
arbitrary directory-creation and file-write primitive reachable over MCP: a
crafted workdir addresses any directory on the machine, ``mkdir(parents=True)``
builds the tree there, and the write stops an unrelated Bernstein project.

Two checks, neither sufficient on its own:

1. **Existing project root.** The resolved workdir must already contain a
   ``.sdd`` directory. The tool can only stop a Bernstein project that is
   already on disk; it never materialises a tree at a path it was handed.
2. **Realpath containment.** The signal path is rebuilt through
   :func:`~bernstein.core.security.path_containment.contained_path` - the
   same barrier ``run_journal_path`` puts in front of the run journal -
   so a ``.sdd``, ``runtime``, ``signals``, or ``SHUTDOWN`` entry that is a
   symlink out of the project cannot redirect the write.

Resolving the workdir is not the barrier by itself. ``Path.resolve`` and
``os.path.realpath`` follow symlinks but do not fold case, so on a
case-insensitive filesystem a resolved path is normalised, not canonical: a
case variant of a project root resolves to a different string for the same
directory. Containment, not the resolve, is what decides whether the write
lands inside the named root, and it is evaluated against the root as
resolved from that same call, so a case variant is measured against itself.

Naming a root is allowed; escaping it is not. An absolute path to another
Bernstein project on the same machine is a legitimate stop target - the
barrier is against a workdir that reaches outside the root it names.

Both checks read the filesystem, and resolving a path stats one entry per
component, so the workdir is screened for shape before either runs. The
remote HTTP surface serves this tool straight from the JSON-RPC arguments
with no tool schema in front of it, which leaves the caller holding the
length: an over-long workdir would otherwise turn one request into an
unbounded run of blocking stats on the serving event loop. A workdir past
:data:`~bernstein.core.security.path_containment.MAX_PATH_BYTES` cannot
address a directory on this filesystem, so there is nothing to learn by
walking it and it is refused up front.

Every refusal is a :class:`ShutdownSignalPathError`. The resolve itself can
fail on an input the filesystem cannot represent - a NUL byte raises
``ValueError`` from the underlying ``lstat`` - and both handlers guard on
the typed error, so an untyped failure would escape as an unhandled error
instead of the structured tool response.
"""

from __future__ import annotations

import os
from pathlib import Path

from bernstein.core.security.path_containment import (
    MAX_PATH_BYTES,
    PathContainmentError,
    contained_path,
)

#: Marker directory that makes a workdir a Bernstein project root.
SDD_DIR_NAME = ".sdd"

#: Fixed segments from the project root down to the signal file. Constants,
#: not caller data - the caller only names the root - but they are still
#: routed through the barrier because any of them can be a symlink on disk.
SHUTDOWN_SEGMENTS = (SDD_DIR_NAME, "runtime", "signals", "SHUTDOWN")


class ShutdownSignalPathError(PathContainmentError):
    """Raised when a workdir cannot safely name a shutdown-signal path.

    Subclasses :class:`~bernstein.core.security.path_containment.PathContainmentError`
    (and so :class:`ValueError`), matching how ``JournalPathError`` reports a
    refused run id, so both MCP surfaces render it with the same structured
    error shape.
    """


def _workdir_text(workdir: str | Path) -> str:
    """Return *workdir* as text, refusing what cannot name a path.

    Screens shape only, with no filesystem call, so an input that can never
    address a directory costs one length check instead of one stat per path
    component.

    Args:
        workdir: The caller-supplied workdir. Untrusted.

    Returns:
        The workdir as a string.

    Raises:
        ShutdownSignalPathError: The workdir is not textual, or is longer
            than a path may be on this filesystem.
    """
    try:
        raw = os.fspath(workdir)
    except TypeError as exc:
        msg = f"workdir must name a path, not {type(workdir).__name__}"
        raise ShutdownSignalPathError(msg) from exc
    if isinstance(raw, bytes):
        msg = "workdir must name a path as text, not bytes"
        raise ShutdownSignalPathError(msg)
    # Measured in encoded bytes, not characters, for the same reason
    # ``path_containment`` measures that way: the filesystem limit is a byte
    # limit. The input is not echoed back here - only its size - because an
    # over-long workdir is exactly the input worth keeping out of the log.
    encoded = len(raw.encode("utf-8", errors="surrogatepass"))
    if encoded > MAX_PATH_BYTES:
        msg = f"workdir is {encoded} bytes, over the {MAX_PATH_BYTES}-byte filesystem limit for a path"
        raise ShutdownSignalPathError(msg)
    return raw


def shutdown_signal_path(workdir: str | Path) -> Path:
    """Return the contained ``SHUTDOWN`` path for an existing project root.

    Args:
        workdir: Project root named by the caller. Untrusted.

    Returns:
        The normalised, containment-checked path of the ``SHUTDOWN`` signal
        file. Callers must write through this return value: it is the only
        path proven to live inside the resolved root.

    Raises:
        ShutdownSignalPathError: The workdir cannot name a path on this
            filesystem, the resolved workdir holds no ``.sdd`` directory, or
            the signal path resolves outside that root.
    """
    raw = _workdir_text(workdir)
    try:
        root = Path(raw).resolve()
    except (OSError, ValueError) as exc:
        msg = f"workdir does not name a directory this filesystem can address: {raw!r}"
        raise ShutdownSignalPathError(msg) from exc
    if not (root / SDD_DIR_NAME).is_dir():
        msg = f"workdir is not an existing Bernstein project root (no {SDD_DIR_NAME} directory): {raw!r}"
        raise ShutdownSignalPathError(msg)
    try:
        return contained_path(root, *SHUTDOWN_SEGMENTS, label="workdir")
    except PathContainmentError as exc:
        msg = f"shutdown signal path resolves outside the project root named by workdir: {raw!r}"
        raise ShutdownSignalPathError(msg) from exc


__all__ = [
    "SDD_DIR_NAME",
    "SHUTDOWN_SEGMENTS",
    "ShutdownSignalPathError",
    "shutdown_signal_path",
]
