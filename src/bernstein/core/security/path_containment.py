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

#: Component separator on either platform. A relative path is stored on one
#: host and consumed on another, so both spellings are split on regardless of
#: which platform is doing the checking.
_SEPARATOR_RE = re.compile(r"[\\/]")

#: A leading Windows drive designator (``C:``), absolute or drive-relative.
#: ``ntpath.isabs`` accepts ``C:x`` as relative, but it is relative to the
#: drive's working directory, not to any base this module is given.
_DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:")


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


def slug_identifier(name: str, *, label: str = "identifier") -> str:
    """Fold an externally-influenced identifier into a filename-safe slug.

    Some receipts embed a *human-readable* adapter name (``Qwen CLI``,
    ``Claude Code``) that contains spaces, while the persistence layer wants
    a single filename component. Rather than reject such names outright, this
    folds the name to the :data:`SAFE_SEGMENT_RE` alphabet so the caller can
    keep writing a file.

    The fold is deliberately not a path. Any character that could change
    where the file lands -- a separator, a NUL byte, or anything that would
    make the slug start with a dot -- is refused *before* the fold, so a
    hostile name cannot be silently relocated into something that looks safe.

    Args:
        name: The identifier to fold. May contain spaces and punctuation,
            but never a path component operator.
        label: Noun used in error messages (e.g. ``"adapter name"``).

    Returns:
        The lowercase slug: every run of non-``[a-z0-9]`` collapsed to a
        single ``-``, with leading/trailing ``-`` stripped. Safe as a single
        path segment under :func:`contained_path`.

    Raises:
        PathContainmentError: If *name* is empty, carries a separator/NUL/``..``
            traversal shape, or folds to an empty slug.
        PathTooLongError: If the slug exceeds :data:`MAX_SEGMENT_BYTES` once
            encoded.
    """
    if not name:
        raise PathContainmentError(f"invalid {label}: {name!r} (empty)")
    if "\x00" in name or "/" in name or "\\" in name:
        raise PathContainmentError(f"invalid {label}: {name!r} (traversal is refused)")
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not slug:
        raise PathContainmentError(f"invalid {label}: {name!r} (folds to an empty slug)")
    return validate_path_segment(slug, label=label)


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


def validate_relative_path(candidate: str, *, label: str = "path") -> str:
    """Return *candidate* unchanged when it names a path below a base directory.

    This is the string-only half of the barrier, so request-validation layers
    can apply the same rule as :func:`contained_subpath` without touching the
    filesystem. It is deliberately weaker than
    :func:`validate_path_segment`: a project-relative path is allowed to have
    several components and to use characters that are illegal in an opaque
    identifier (spaces, ``+``, non-ASCII names all occur in real repositories).
    What it must not do is select a location the base does not contain.

    Both separators are examined, not just this platform's. A stored value is
    written on one host and consumed on another, so ``..\\..\\x`` has to be
    refused on POSIX as well - it is a parent reference wherever it is read.

    Args:
        candidate: The externally-influenced relative path to check.
        label: Noun used in the error message (e.g. ``"owned file"``).

    Returns:
        The candidate, unchanged, when it is a usable relative path.

    Raises:
        PathContainmentError: If the candidate is empty, absolute (POSIX
            root, Windows drive, or UNC share), carries a ``..`` component,
            or contains a NUL byte.
        PathTooLongError: If any component exceeds
            :data:`MAX_SEGMENT_BYTES` once encoded.
    """
    if not candidate:
        msg = f"unsafe {label}: must not be empty"
        raise PathContainmentError(msg)
    if "\x00" in candidate:
        msg = f"unsafe {label}: must not contain a NUL byte"
        raise PathContainmentError(msg)
    # A leading separator is a POSIX absolute path or a UNC share; a leading
    # drive letter is absolute or drive-relative on Windows. Neither is a
    # path *under* a base, and ``Path.__truediv__`` discards the base for
    # both, so both are refused before the join happens.
    if candidate[0] in "/\\" or _DRIVE_PREFIX_RE.match(candidate):
        msg = f"unsafe {label} {candidate!r}: must be relative to its base directory"
        raise PathContainmentError(msg)
    named = 0
    for component in _SEPARATOR_RE.split(candidate):
        # A ``.`` component, or the empty one a doubled separator produces,
        # is redundant rather than unsafe: ``./src/x.py`` is an ordinary way
        # to spell a relative path and normalisation drops it. What it must
        # not do is be the whole value - see the strict-descendant check
        # below. ``..`` is the one that changes where the path points, and
        # is refused wherever it appears.
        if component == "..":
            msg = f"unsafe {label} {candidate!r}: must not contain a '..' component"
            raise PathContainmentError(msg)
        if component in ("", "."):
            continue
        named += 1
        encoded = len(component.encode("utf-8", errors="surrogatepass"))
        if encoded > MAX_SEGMENT_BYTES:
            msg = (
                f"{label} component is {encoded} bytes, over the "
                f"{MAX_SEGMENT_BYTES}-byte filesystem limit for one path component"
            )
            raise PathTooLongError(msg)
    # The result must be a strict descendant of the base, never the base
    # itself, so a value that names no component at all ("." , "./", "//")
    # is refused even though every component in it was individually safe.
    if named == 0:
        msg = f"unsafe {label} {candidate!r}: must name a path below its base directory"
        raise PathContainmentError(msg)
    return candidate


def contained_subpath(base: Path | str, candidate: str, *, label: str = "path") -> Path:
    """Join *candidate* under *base* and prove the result stays inside it.

    The multi-component counterpart to :func:`contained_path`, for callers
    whose input is a project-relative path (``src/pkg/mod.py``) rather than a
    single opaque identifier. Both halves of the barrier still apply and
    neither is sufficient alone: :func:`validate_relative_path` rejects the
    shapes that can be seen in the string, and the realpath comparison
    catches the one it cannot - an ordinary-looking component that is itself
    a symlink out of the tree.

    Args:
        base: The intended containing directory. Trusted by configuration;
            it need not exist yet.
        candidate: The relative path to append. Must be a strict descendant
            of *base*, never *base* itself.
        label: Noun used in error messages (e.g. ``"owned file"``).

    Returns:
        The normalised, containment-checked path. Callers must use this
        return value for filesystem access - it is the only value proven
        to be inside *base*.

    Raises:
        PathContainmentError: If the candidate is not a usable relative path,
            or if it resolves outside the resolved base.
        PathTooLongError: If a component, or the composed path, exceeds what
            the filesystem can represent.
    """
    validate_relative_path(candidate, label=label)
    base_real = os.path.realpath(base)
    # ``os.path.join(x, "")`` appends the separator without doubling it on a
    # drive root such as ``C:\``, so the prefix test below stays correct for
    # every base. The trailing separator is what stops a sibling directory
    # like ``<base>-evil`` from passing a bare prefix comparison.
    base_prefix = os.path.join(base_real, "")
    # ``realpath`` resolves symlinks and normalises ``.``/``..`` even for a
    # path that does not exist yet, so the containment test sees exactly the
    # location a later open() would reach.
    resolved = os.path.realpath(os.path.join(base_real, candidate))
    if not resolved.startswith(base_prefix):
        # The base is deliberately left out of the message: these errors can
        # surface to an API caller, and the absolute layout is not theirs.
        msg = f"{label} {candidate!r} resolves outside its base directory"
        raise PathContainmentError(msg)
    # A legal-length component under an already-deep base can still exceed
    # PATH_MAX, which would surface as OSError(ENAMETOOLONG) at open() -
    # outside the ValueError hierarchy callers guard on. Checked after
    # containment so an escape attempt is never reported as a length problem.
    encoded_path = len(resolved.encode("utf-8", errors="surrogatepass"))
    if encoded_path > MAX_PATH_BYTES:
        msg = f"path for {label} is {encoded_path} bytes, over the {MAX_PATH_BYTES}-byte filesystem limit"
        raise PathTooLongError(msg)
    return Path(resolved)


__all__ = [
    "MAX_PATH_BYTES",
    "MAX_SEGMENT_BYTES",
    "SAFE_SEGMENT_RE",
    "PathContainmentError",
    "PathTooLongError",
    "contained_path",
    "contained_subpath",
    "validate_path_segment",
    "validate_relative_path",
]
