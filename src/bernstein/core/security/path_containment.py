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
#: already uses for run, worktree, task, and ledger ids.
#:
#: Length is deliberately NOT bounded here. :data:`MAX_SEGMENT_BYTES` owns it,
#: measured in encoded bytes, because ``NAME_MAX`` is a byte limit and a
#: character count would let a multi-byte name through.
#:
#: The tail anchor is ``\Z``, not ``$``: in Python ``$`` also matches just
#: before a trailing newline, so ``$`` would accept ``"..\n"`` - which the
#: reserved-segment check below does not catch, since it is not equal to
#: ``".."``. Containment would still hold (a newline is a literal character,
#: not a parent reference), but the id would carry a control character into
#: a directory name and into operator-facing log and ledger listings.
SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.-]+\Z")

#: Segments that match the alphabet but name the current/parent directory.
_RESERVED_SEGMENTS = frozenset({".", ".."})


#: Longest single path component, in encoded bytes. ``NAME_MAX`` is 255 on
#: every filesystem this project supports.
MAX_SEGMENT_BYTES = 255

#: Longest composed path, in encoded bytes. ``PATH_MAX`` is 4096 on Linux;
#: a legal-length segment under an already-deep base can still exceed it.
MAX_PATH_BYTES = 4096


class PathContainmentError(ValueError):
    """Raised for an unsafe path segment or a path that escapes its base.

    Subclasses :class:`ValueError` so existing callers that already guard
    identifier handling with ``except ValueError`` keep working.
    """


class PathTooLongError(PathContainmentError):
    """Raised when an identifier cannot name a file on this filesystem.

    Distinct from its parent on purpose. A containment violation (traversal,
    or a symlinked child pointing out of the tree) is an attack signal and
    must never be swallowed - readers let it propagate. An over-long name is
    a *capacity* failure: the identifier is contained, it simply cannot exist
    on disk, which is indistinguishable from "no such journal". Readers that
    already degrade to an empty result for a missing file may catch this one,
    and only this one.

    Without the bound the filesystem raises ``OSError(ENAMETOOLONG)`` at
    ``open()`` instead, which escapes the ``ValueError`` hierarchy every
    caller guards on and turns a rejected identifier into a crash.
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
        PathTooLongError: If the segment exceeds :data:`MAX_SEGMENT_BYTES`
            once encoded.
    """
    if segment in _RESERVED_SEGMENTS or not SAFE_SEGMENT_RE.match(segment):
        msg = f"unsafe {label} {segment!r}: must match {SAFE_SEGMENT_RE.pattern} and must not be '.' or '..'"
        raise PathContainmentError(msg)
    # Measured in encoded bytes, not characters: NAME_MAX is a byte limit, so
    # a character count would let a multi-byte name through and hand the
    # filesystem a component it cannot store.
    encoded = len(segment.encode("utf-8", errors="surrogatepass"))
    if encoded > MAX_SEGMENT_BYTES:
        msg = f"{label} is {encoded} bytes, over the {MAX_SEGMENT_BYTES}-byte filesystem limit for one path component"
        raise PathTooLongError(msg)
    return segment


def contained_path(base: Path | str, *segments: str, label: str = "identifier") -> Path:
    """Join *segments* under *base* and prove the result stays inside it.

    Args:
        base: The intended containing directory. Trusted by configuration;
            it need not exist yet.
        *segments: Path segments to append, each checked by
            :func:`validate_path_segment`. At least one is required - the
            result must be a strict descendant of *base*, never *base*
            itself.
        label: Noun used in error messages (e.g. ``"mission id"``).

    Returns:
        The normalised, containment-checked path. Callers must use this
        return value for filesystem access - it is the only value proven
        to be inside *base*.

    Raises:
        PathContainmentError: If no segment is given, if a segment is
            unsafe, or if the resolved candidate falls outside the
            resolved base (for example via a symlinked child).
        PathTooLongError: If a segment, or the composed path, exceeds what
            the filesystem can represent.
    """
    if not segments:
        msg = f"contained_path requires at least one {label} segment"
        raise PathContainmentError(msg)
    for segment in segments:
        validate_path_segment(segment, label=label)
    base_real = os.path.realpath(base)
    # ``os.path.join(x, "")`` appends the separator without doubling it on a
    # drive root such as ``C:\``, so the prefix test below stays correct for
    # every base. The trailing separator is what stops a sibling directory
    # like ``<base>-evil`` from passing a bare prefix comparison.
    base_prefix = os.path.join(base_real, "")
    # ``realpath`` resolves symlinks and normalises ``..`` even for a path
    # that does not exist yet, so the containment test sees exactly the
    # location a later open() would reach. Every segment is a plain name, so
    # a contained result is always a strict descendant of the base and a
    # single prefix test is the whole check.
    candidate = os.path.realpath(os.path.join(base_real, *segments))
    if not candidate.startswith(base_prefix):
        # The base is deliberately left out of the message: these errors can
        # surface to an API caller, and the absolute layout is not theirs.
        joined = "/".join(segments)
        msg = f"{label} {joined!r} resolves outside its base directory"
        raise PathContainmentError(msg)
    # A legal-length segment under an already-deep base can still exceed
    # PATH_MAX, which would surface as OSError(ENAMETOOLONG) at open() -
    # outside the ValueError hierarchy callers guard on. Checked after
    # containment so an escape attempt is never reported as a length problem.
    encoded_path = len(candidate.encode("utf-8", errors="surrogatepass"))
    if encoded_path > MAX_PATH_BYTES:
        msg = f"path for {label} is {encoded_path} bytes, over the {MAX_PATH_BYTES}-byte filesystem limit"
        raise PathTooLongError(msg)
    return Path(candidate)


__all__ = [
    "MAX_PATH_BYTES",
    "MAX_SEGMENT_BYTES",
    "SAFE_SEGMENT_RE",
    "PathContainmentError",
    "PathTooLongError",
    "contained_path",
    "validate_path_segment",
]
