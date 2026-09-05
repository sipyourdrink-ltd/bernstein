"""Which repository-relative paths fall outside a set of globs.

Two surfaces need this question answered the same way. A volunteer project's
manifest declares ``allowed_paths`` and a submission's diff must stay inside it
(#3869); an agent credential carries ``allowed_files`` and the merge that
accepts the agent's work must stay inside that (#3781). A patch judged admissible
by one and inadmissible by the other, for the same paths and the same patterns,
is a bug in whichever one you were not looking at.

Why not ``fnmatch``: it has no separator. ``fnmatch("src/a/b.py", "src/*")`` is
true, so a scope meant to admit the files directly under ``src/`` silently
admits the whole tree beneath it. Four places in this repository reach for
``fnmatch`` and one of them already carries a comment about working around
exactly that. Here ``*`` and ``?`` stop at ``/`` and only a whole ``**`` segment
crosses directories.

The pattern language is deliberately small:

===============  ==========================================================
``*``            any run of characters within one path segment, including none
``?``            exactly one character within one path segment
``**``           zero or more whole segments, and only as a complete segment
everything else  itself, including ``[`` and ``]`` - there are no character
                 classes, because a half-open bracket in a stored pattern
                 should not be able to change what the rest of it means
===============  ==========================================================

A pattern is not a prefix. ``src`` admits the path ``src`` and nothing under it;
``src/**`` is how you admit the tree. That is the one rule people expect to work
the other way, so it is stated here and pinned by a test.
"""

from __future__ import annotations

import re
from functools import cache, lru_cache
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

__all__ = [
    "ScopePatternError",
    "normalise_repo_path",
    "paths_outside_scope",
    "pattern_subsumes",
    "validate_repo_relative_pattern",
]


class ScopePatternError(ValueError):
    """A glob that cannot mean "repository-relative" whatever it is read by.

    Raised by :func:`validate_repo_relative_pattern` and carrying only the
    reason, not the field it came from: the two callers name their own field
    differently (``allowed_paths[2]`` in a manifest, ``allowed_files`` on a
    credential) and each wraps this in the error type its own surface already
    raises.
    """


def validate_repo_relative_pattern(pattern: str) -> str:
    """Return ``pattern`` unchanged, or refuse it as not repository-relative.

    Both surfaces that declare a scope validate through here, so a glob
    admitted by one and refused by the other is not reachable. The refusals
    are the forms that could name a file outside the checkout: an empty glob,
    a NUL byte, an absolute path, a drive-qualified path, a home-directory
    reference, and any pattern whose ``..`` segments walk out of the root.

    Applied where a scope is *declared*, never where one is loaded. Records
    written before this existed stay loadable; an uninterpretable pattern is
    handled at match time by admitting nothing (see :func:`paths_outside_scope`),
    which is the safe direction.

    Args:
        pattern: The glob to check, in any spelling a caller might hold it.

    Returns:
        The pattern exactly as passed, so callers can use this inline.

    Raises:
        ScopePatternError: The pattern could name something outside the root.
    """
    if not pattern:
        raise ScopePatternError("empty glob")
    if "\x00" in pattern:
        raise ScopePatternError("contains a NUL byte")

    normalised = pattern.replace("\\", "/")
    if normalised.startswith("/"):
        raise ScopePatternError(f"{pattern!r} is absolute; globs are relative to the repository root")
    if len(normalised) > 1 and normalised[1] == ":":
        raise ScopePatternError(f"{pattern!r} names a drive; globs are relative to the repository root")
    if normalised.startswith("~"):
        raise ScopePatternError(f"{pattern!r} refers to a home directory")

    depth = 0
    for part in PurePosixPath(normalised).parts:
        if part == "..":
            depth -= 1
            if depth < 0:
                raise ScopePatternError(f"{pattern!r} escapes the repository root")
        elif part not in (".", ""):
            depth += 1
    return pattern


def normalise_repo_path(path: str) -> str:
    """Return ``path`` in the spelling this module compares against.

    Backslashes become separators and a leading ``./`` is dropped, so a caller
    reading paths off a Windows filesystem and one reading them from
    ``git diff --name-only`` produce the same string for the same file.
    """
    normalised = path.replace("\\", "/")
    while normalised.startswith("./"):
        normalised = normalised[2:]
    return normalised


@lru_cache(maxsize=512)
def _compiled(pattern: str) -> re.Pattern[str] | None:
    """Return the matcher for ``pattern``, or None when it admits nothing.

    An empty pattern is the only unusable one, and it admits nothing rather
    than everything: a scope that cannot be read must not widen to "no scope",
    which is the direction that turns a stored typo into an open door.
    """
    normalised = normalise_repo_path(pattern)
    if not normalised:
        return None

    segments = _collapse_repeated_stars(normalised.split("/"))
    last = len(segments) - 1
    parts: list[str] = []
    for index, segment in enumerate(segments):
        if segment != "**":
            # The separator belongs to the segment that follows it, unless a
            # `**` already accounted for one.
            if index > 0 and segments[index - 1] != "**":
                parts.append("/")
            parts.append(_segment_regex(segment))
        elif index == last:
            # Trailing: `src/**` covers `src` itself and everything under it.
            parts.append("(?:/.*)?" if parts else ".*")
        elif parts:
            # Middle: `a/**/b` has to cover `a/b`, so the separators the eaten
            # segments carried are consumed here and one is emitted for `b`.
            parts.append("(?:/[^/]+)*/")
        else:
            # Leading: `**/b` covers `b` at any depth, including the root.
            parts.append("(?:[^/]+/)*")

    return re.compile("".join(parts) + r"\Z")


def _collapse_repeated_stars(segments: list[str]) -> list[str]:
    """Fold `a/**/**/b` down to `a/**/b`.

    Two adjacent `**` say nothing the first does not, and each would emit its
    own separator - which is a pattern that matches nothing rather than
    everything, the wrong way for a typo to fail loudly.
    """
    folded: list[str] = []
    for segment in segments:
        if segment == "**" and folded and folded[-1] == "**":
            continue
        folded.append(segment)
    return folded


def _segment_regex(segment: str) -> str:
    """Translate one path segment, where ``*`` and ``?`` stop at a separator."""
    out: list[str] = []
    for ch in segment:
        if ch == "*":
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(ch))
    return "".join(out)


def _admits(path: str, patterns: Sequence[str]) -> bool:
    """True when any pattern admits ``path``."""
    return any((rx := _compiled(p)) is not None and rx.match(path) for p in patterns)


def paths_outside_scope(paths: Iterable[str], patterns: Sequence[str]) -> tuple[str, ...]:
    """Return the paths no pattern admits, in input order, without repeats.

    Args:
        paths: Repository-relative paths, as ``git diff --name-only`` prints
            them. Normalised before comparison.
        patterns: The declared scope. **Empty means no restriction** - every
            identity and every manifest that has never set one carries an empty
            list, so a caller that turned an empty scope into "admit nothing"
            would refuse all existing work.

    Returns:
        The offending paths. Empty when everything is in scope, which is also
        the answer for an empty ``patterns``. Callers name these in the refusal:
        a scope error that does not say which paths broke it reads as a bug to
        the first person who hits it.
    """
    if not patterns:
        return ()

    outside: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        path = normalise_repo_path(raw)
        if not path or path in seen:
            continue
        seen.add(path)
        if not _admits(path, patterns):
            outside.append(path)
    return tuple(outside)


def pattern_subsumes(outer: str, inner: str) -> bool:
    """True when every path ``inner`` admits is also admitted by ``outer``.

    Containment between two patterns, decided against the language above
    rather than by matching sample paths: ``src/**`` subsumes ``src/core/**``
    because no path exists that the second admits and the first does not.

    The relation is not ancestry. ``src`` does not subsume ``src/core`` for the
    same reason ``src`` does not admit the path ``src/core`` - a pattern is not
    a prefix. Nor is it string containment: ``src/*`` does not subsume
    ``src/a/b``, because ``*`` stops at a separator.

    Undecided cases answer ``False``. A pattern that is admitted only by two
    parent patterns *together* (``a/b`` under ``{a/*, b/*}`` is decided, but
    ``a/?`` under ``{a/x, a/y}`` is not) is reported as not subsumed, which is
    the direction that cannot report a narrowing that did not happen. Callers
    comparing whole sets should read :func:`globs_narrow` in
    :mod:`bernstein.core.security.capability_tokens`, which is where the
    narrowing primitives live.

    Args:
        outer: The wider pattern, in any spelling ``normalise_repo_path``
            accepts.
        inner: The pattern that must be contained in it.

    Returns:
        True when ``outer`` admits every path ``inner`` admits.
    """
    outer_segments = _collapse_repeated_stars(normalise_repo_path(outer).split("/"))
    inner_segments = _collapse_repeated_stars(normalise_repo_path(inner).split("/"))
    return _segments_subsume(tuple(outer_segments), tuple(inner_segments))


@lru_cache(maxsize=1024)
def _segments_subsume(outer: tuple[str, ...], inner: tuple[str, ...]) -> bool:
    """Whole-path subsumption, matching ``**`` against runs of inner segments."""

    @cache
    def walk(o: int, i: int) -> bool:
        if o == len(outer):
            # Nothing left to admit with. A remaining inner segment - even a
            # `**`, which can also stand for one or more - names paths this
            # side cannot produce.
            return i == len(inner)
        if outer[o] == "**":
            # Zero segments, or one more of the inner's.
            return walk(o + 1, i) or (i < len(inner) and walk(o, i + 1))
        if i == len(inner):
            return False
        if inner[i] == "**":
            # A single outer segment stands for exactly one segment; `**` may
            # stand for none or for several, so it is not contained by one.
            return False
        return _segment_subsumes(outer[o], inner[i]) and walk(o + 1, i + 1)

    return walk(0, 0)


@lru_cache(maxsize=2048)
def _segment_subsumes(outer: str, inner: str) -> bool:
    """Subsumption within one segment, where ``*`` and ``?`` do not cross ``/``."""

    @cache
    def walk(o: int, i: int) -> bool:
        if o == len(outer):
            # A remaining `*` on the inner side can stand for a character this
            # side has nothing left to admit with, so only an exhausted inner
            # is contained.
            return i == len(inner)
        if outer[o] == "*":
            # Any run within the segment, including none.
            return walk(o + 1, i) or (i < len(inner) and walk(o, i + 1))
        if i == len(inner):
            return False
        if outer[o] == "?":
            # Exactly one character, so an inner `*` - which may stand for none
            # or for several - is not contained by it.
            return inner[i] != "*" and walk(o + 1, i + 1)
        return outer[o] == inner[i] and walk(o + 1, i + 1)

    return walk(0, 0)
