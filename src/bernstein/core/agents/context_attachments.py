"""Resolve declared task context files into a recorded attachment set (#3375).

Operators declare "the worker on this task needs these reference files" on
backlog tickets (``context_files:`` in Ticket Format v1 frontmatter) and on
plan files (top-level ``context_files:``). The declaration travels as
``task.metadata["context_files"]`` and the spawner lists the files in the
worker's task-specific CLAUDE.md. This module is the dispatch-time half:
each declared path is resolved in declared order against the worker's
worktree and content-addressed, so the run record can answer "which
reference material did this worker see, at which content" offline.

Entry shape (kept compatible with the #3366 context-manifest entries so the
read-side manifest can adopt it):

* ``path`` - the declared path, verbatim.
* ``order`` - zero-based declared position.
* ``sha256`` - ``sha256:<hex>`` of the file bytes when the path resolves,
  ``""`` otherwise.
* ``reason_code`` - ``""`` when resolved; otherwise one of
  :data:`REASON_MISSING`, :data:`REASON_IS_DIRECTORY`,
  :data:`REASON_UNREADABLE`, :data:`REASON_OUTSIDE_ROOT`,
  :data:`REASON_INVALID`.

Absence is explicit: an unresolvable path keeps its position in the list
with a reason code instead of being skipped, the same discipline #3366
defines for ``unmanifested`` manifest entries. Resolution never raises for
a bad path - a declared-but-broken file must surface in the record, not
abort the spawn.

The resolved set is stamped onto the worker's :class:`AgentSession` and
recorded in the run journal as a :data:`CONTEXT_FILES_ATTACHED_EVENT` event
next to ``agent_spawned``, the same mechanism the spawn path already uses
for per-session run state.
"""

from __future__ import annotations

import hashlib
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from pathlib import Path

    from bernstein.core.models import Task

__all__ = [
    "CONTEXT_FILES_ATTACHED_EVENT",
    "REASON_INVALID",
    "REASON_IS_DIRECTORY",
    "REASON_MISSING",
    "REASON_OUTSIDE_ROOT",
    "REASON_UNREADABLE",
    "collect_declared_context_files",
    "resolve_context_attachments",
    "verify_context_attachments",
]

#: Journal event recorded next to ``agent_spawned`` when a worker's tasks
#: declare context files. Carries the resolved entry list, so the run record
#: pins the attachment set at its content addresses.
CONTEXT_FILES_ATTACHED_EVENT = "context.files_attached"

#: The declared path does not exist under the worktree root.
REASON_MISSING = "missing"
#: The declared path resolves to a directory, not a file.
REASON_IS_DIRECTORY = "is_directory"
#: The declared path exists but its bytes could not be read.
REASON_UNREADABLE = "unreadable"
#: The declared path escapes the worktree root (absolute path outside it,
#: ``..`` traversal, or a symlink pointing out of the tree).
REASON_OUTSIDE_ROOT = "outside_root"
#: The declared path cannot be represented as a filesystem path at all
#: (e.g. an embedded NUL byte): normalization refused it before any
#: containment or existence check could run.
REASON_INVALID = "invalid"


def collect_declared_context_files(tasks: Iterable[Task]) -> list[str]:
    """Return the declared context files for a task batch, deduplicated.

    Reads ``task.metadata["context_files"]`` from every task, preserving
    declared order across the batch; the first occurrence of a path wins.
    Non-list metadata values and non-string entries are ignored.
    """
    declared: list[str] = []
    for task in tasks:
        raw = task.metadata.get("context_files") if isinstance(task.metadata, dict) else None
        if not isinstance(raw, list):
            continue
        for entry in raw:
            if isinstance(entry, str) and entry not in declared:
                declared.append(entry)
    return declared


def _resolve_one(root: Path, declared: str) -> tuple[str, str]:
    """Resolve one declared path; return ``(sha256, reason_code)``.

    Exactly one of the two is non-empty. Containment mirrors
    :func:`bernstein.core.security.path_containment.contained_path`'s realpath
    check: the candidate is normalised with symlinks followed and must stay
    under the normalised root. The per-segment identifier allowlist is not
    applied here - declared context paths are operator-authored file paths,
    not externally-influenced identifiers - so names the allowlist would
    refuse (spaces, unicode) still resolve; only an actual escape is refused.
    """
    try:
        root_real = os.path.realpath(root)
        root_prefix = os.path.join(root_real, "")
        candidate = os.path.realpath(os.path.join(root_real, declared))
    except (TypeError, ValueError, OSError):
        # ``os.path.realpath`` raises ValueError on an embedded NUL byte
        # (and TypeError/OSError on other input the filesystem cannot
        # represent). This function is total by contract: a malformed
        # declaration surfaces as a recorded absence in its position, not
        # as an exception that would abort the spawn - or the verify pass.
        return "", REASON_INVALID
    if not candidate.startswith(root_prefix):
        return "", REASON_OUTSIDE_ROOT
    if os.path.isdir(candidate):
        return "", REASON_IS_DIRECTORY
    if not os.path.isfile(candidate):
        return "", REASON_MISSING
    try:
        with open(candidate, "rb") as fh:
            digest = hashlib.file_digest(fh, "sha256").hexdigest()
    except OSError:
        return "", REASON_UNREADABLE
    return f"sha256:{digest}", ""


def resolve_context_attachments(*, root: Path, declared: Sequence[str]) -> list[dict[str, object]]:
    """Resolve declared context paths against *root*, in declared order.

    Args:
        root: The worker's worktree root - the tree the worker actually
            reads the files from.
        declared: Declared context file paths, in declared order.

    Returns:
        One entry per declared path, in declared order - the entry count
        always equals ``len(declared)``. Resolvable paths carry
        ``sha256:<hex>`` of their bytes; unresolvable paths keep their
        position and carry a reason code instead.
    """
    entries: list[dict[str, object]] = []
    for order, path in enumerate(declared):
        sha256, reason_code = _resolve_one(root, path)
        entries.append(
            {
                "path": path,
                "order": order,
                "sha256": sha256,
                "reason_code": reason_code,
            }
        )
    return entries


def verify_context_attachments(*, root: Path, entries: Sequence[Mapping[str, object]]) -> list[str]:
    """Recompute recorded digests from the files under *root* and compare.

    The round-trip check for a recorded attachment set: re-resolve every
    entry's path exactly as :func:`resolve_context_attachments` did and
    compare both the digest and the reason code.

    Returns:
        A list of human-readable mismatch descriptions, one per diverging
        entry, naming the first divergence per entry. Empty means every
        recorded entry matches the current bytes (and every recorded
        absence is still absent for the same reason).
    """
    mismatches: list[str] = []
    for entry in entries:
        path = str(entry.get("path", ""))
        recorded_sha = str(entry.get("sha256", ""))
        recorded_reason = str(entry.get("reason_code", ""))
        actual_sha, actual_reason = _resolve_one(root, path)
        if actual_sha != recorded_sha:
            mismatches.append(f"{path}: recorded {recorded_sha or '<none>'}, recomputed {actual_sha or '<none>'}")
        elif actual_reason != recorded_reason:
            mismatches.append(
                f"{path}: recorded reason {recorded_reason or '<none>'}, recomputed {actual_reason or '<none>'}"
            )
    return mismatches
