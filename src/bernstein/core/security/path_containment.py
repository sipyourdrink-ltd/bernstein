"""Filesystem containment barrier for externally-influenced identifiers.

Run ids, task ids, ledger ids, and mission ids reach the process from the
dashboard API, the MCP surface, and the CLI, and several of them name a
directory or a file under ``.sdd``. Joining such an identifier onto a base
directory without a barrier lets ``..`` segments, an absolute path, or a
symlinked child address files outside the tree the caller intended
(``py/path-injection``).

This module is the single place that turns "an identifier the caller does
not control" into "a path proven to live under a base directory". Both
halves of the barrier are load-bearing and neither is sufficient alone:

1. **Allowlist.** Every segment must match :data:`SAFE_SEGMENT_RE` and must
   not be ``.`` or ``..``. This rejects separators, traversal, NUL bytes,
   and absolute paths before any filesystem call happens.
2. **Realpath containment.** The joined candidate is normalised with
   :func:`os.path.realpath` (which follows symlinks) and required to stay
   under the normalised base. This catches the case the allowlist cannot:
   a well-named child that is itself a symlink pointing out of the tree.

:func:`contained_path` returns the *normalised, checked* path, and callers
build their filesystem access from that return value rather than from the
raw join, so no unchecked string survives to reach a filesystem sink.

The base directory is trusted by configuration (it is derived from the
install's ``.sdd`` root, never from request data); the identifier is not.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

#: A safe path segment: the git-ref-safe alphabet the persistence layer
#: already uses for run, worktree, task, and ledger ids. Anchored on both
#: ends, so a separator or a traversal sequence cannot match. The 255-byte
#: cap is the ``NAME_MAX`` every supported filesystem enforces anyway, so
#: it turns a later ``ENAMETOOLONG`` into a typed error rather than taking
#: away a name that used to work.
SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,255}$")

#: Segments that match the alphabet but name the current/parent directory.
_RESERVED_SEGMENTS = frozenset({".", ".."})


class PathContainmentError(ValueError):
    """Raised for an unsafe path segment or a path that escapes its base.

    Subclasses :class:`ValueError` so existing callers that already guard
    identifier handling with ``except ValueError`` keep working.
    """


def validate_path_segment(segment: str, *, label: str = "identifier") -> str:
    """Return *segment* unchanged when it is a safe single path segment.

    Args:
        segment: The externally-influenced identifier to check.
        label: Noun used in the error message (e.g. ``"run id"``).

    Returns:
        The segment, unchanged, when it is safe.

    Raises:
        PathContainmentError: If the segment is empty, is ``.`` or ``..``,
            or contains anything outside :data:`SAFE_SEGMENT_RE`.
    """
    if segment in _RESERVED_SEGMENTS or not SAFE_SEGMENT_RE.match(segment):
        msg = f"unsafe {label} {segment!r}: must match {SAFE_SEGMENT_RE.pattern} and must not be '.' or '..'"
        raise PathContainmentError(msg)
    return segment


def contained_path(base: Path | str, *segments: str, label: str = "identifier") -> Path:
    """Join *segments* under *base* and prove the result stays inside it.

    Args:
        base: The intended containing directory. Trusted by configuration;
            it need not exist yet.
        *segments: Path segments to append, each checked by
            :func:`validate_path_segment`.
        label: Noun used in error messages (e.g. ``"mission id"``).

    Returns:
        The normalised, containment-checked path. Callers must use this
        return value for filesystem access - it is the only value proven
        to be inside *base*.

    Raises:
        PathContainmentError: If a segment is unsafe, or if the resolved
            candidate falls outside the resolved base (for example via a
            symlinked child).
    """
    for segment in segments:
        validate_path_segment(segment, label=label)
    base_real = os.path.realpath(base)
    # ``realpath`` resolves symlinks and normalises ``..`` even for a path
    # that does not exist yet, so the containment test below sees exactly
    # the location a later open() would reach.
    candidate = os.path.realpath(os.path.join(base_real, *segments))
    if candidate != base_real and not candidate.startswith(base_real + os.sep):
        joined = "/".join(segments)
        msg = f"{label} {joined!r} resolves outside {base_real}"
        raise PathContainmentError(msg)
    return Path(candidate)


__all__ = [
    "SAFE_SEGMENT_RE",
    "PathContainmentError",
    "contained_path",
    "validate_path_segment",
]
