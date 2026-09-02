"""Content-addressed manifest of the files a task declares as its context.

Issue #3366. The run journal already records what an agent *did*; what it was
*shown* is left implicit. When two runs of the same task diverge, replay can
prove the tool calls differed but cannot attribute the divergence to context
drift, because the context set was never an artifact.

This module derives that artifact. :func:`derive_context_manifest` reads a
task's declared path set -- ``Task.owned_files`` -- and returns an ordered,
deduplicated :class:`ContextManifest`: every resolvable file content-addressed
as ``sha256:<hex>`` of its bytes, and every path that does not resolve recorded
as an ``unmanifested`` entry carrying a reason code and still occupying its
position in the list. Absence is explicit, never silent, and the entry count
never shrinks below the number of distinct declared paths.

The manifest digest follows the canonical-JSON discipline already in the tree
(``ContextCapsule.canonical_bytes``): the digest is a function of the declared
path set and the bytes behind it, not of the order the filesystem was walked in
or of how a path happened to be spelled. Two derivations over the same tree are
byte-identical; a single byte changed in one declared file moves the digest, and
:func:`first_manifest_divergence` names the entry that moved and both hashes.

Containment is not advisory here. A declared path is externally influenced (it
arrives on a task from the API, the CLI, or an issue body), so every path goes
through :func:`~bernstein.core.security.path_containment.contained_subpath`
before it is opened. A path that escapes the repository root is recorded as
``outside_root`` and its bytes are never read.

This module is pure: it reads files and returns a value. Nothing anchors the
digest in a run record or on a receipt yet, so no adapter behaviour depends on
it.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bernstein.core.security.path_containment import (
    PathContainmentError,
    PathTooLongError,
    contained_subpath,
    validate_relative_path,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

__all__ = [
    "CONTEXT_MANIFEST_VERSION",
    "REASON_CODES",
    "REASON_INVALID_PATH",
    "REASON_MISSING",
    "REASON_NOT_A_FILE",
    "REASON_OUTSIDE_ROOT",
    "REASON_UNREADABLE",
    "ContextManifest",
    "ContextManifestEntry",
    "ManifestDivergence",
    "derive_context_manifest",
    "first_manifest_divergence",
]

#: Wire-format version stamped into every manifest preimage.
CONTEXT_MANIFEST_VERSION = 1

#: The declared path names nothing on disk.
REASON_MISSING = "missing"

#: The declared path resolves to a directory or another non-regular file.
REASON_NOT_A_FILE = "not_a_file"

#: The declared path resolves to a regular file whose bytes could not be read.
REASON_UNREADABLE = "unreadable"

#: The declared path resolves outside the repository root (traversal, an
#: absolute path, or an ordinary-looking component that is a symlink out of the
#: tree). The bytes behind such a path are never read.
REASON_OUTSIDE_ROOT = "outside_root"

#: The declared path is not a usable repository-relative path at all: empty,
#: carrying a NUL byte, naming the root itself, or too long for the filesystem.
REASON_INVALID_PATH = "invalid_path"

#: Every reason code a deriver can record. Closed set: a reader that does not
#: recognise a code is reading a manifest from a newer version.
REASON_CODES = frozenset(
    {
        REASON_MISSING,
        REASON_NOT_A_FILE,
        REASON_UNREADABLE,
        REASON_OUTSIDE_ROOT,
        REASON_INVALID_PATH,
    }
)

#: Read granularity for content addressing. A declared file can be large; the
#: digest must not require holding it in memory.
_CHUNK_BYTES = 1 << 20

#: Normalised spellings that name the repository root rather than a file in it.
_ROOT_SPELLINGS = frozenset({"", "."})


@dataclass(frozen=True, slots=True)
class ContextManifestEntry:
    """One declared path, either content-addressed or explicitly unmanifested.

    Frozen + slots so the byte form is canonical.

    Attributes:
        path: The declared path in normalised repository-relative POSIX form.
            Normalisation is what makes the digest a function of the file set
            rather than of the spelling: ``./src/a.py`` and ``src/a.py`` are one
            entry.
        digest: ``sha256:<hex>`` of the file's bytes, or the empty string when
            the entry is unmanifested.
        unmanifested: True when the deriver could not resolve the path to bytes.
        reason: A member of :data:`REASON_CODES` when *unmanifested*, otherwise
            the empty string.
    """

    path: str
    digest: str
    unmanifested: bool
    reason: str

    def to_canonical_dict(self) -> dict[str, Any]:
        """Return the JCS-canonical mapping for this entry."""
        return {
            "path": self.path,
            "digest": self.digest,
            "unmanifested": self.unmanifested,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ContextManifest:
    """The ordered, content-addressed set of files a task declares as context.

    Attributes:
        v: Wire-format version.
        entries: Entries in declared order, deduplicated on the normalised path.
    """

    v: int
    entries: tuple[ContextManifestEntry, ...]

    def to_canonical_dict(self) -> dict[str, Any]:
        """Return the JCS-canonical mapping (sorted keys, lists not tuples)."""
        return {
            "v": self.v,
            "entries": [entry.to_canonical_dict() for entry in self.entries],
        }

    def to_dict(self) -> dict[str, Any]:
        """Alias for on-disk storage."""
        return self.to_canonical_dict()

    def canonical_bytes(self) -> bytes:
        """RFC 8785-style canonical bytes of the manifest."""
        return json.dumps(
            self.to_canonical_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")

    def manifest_digest(self) -> str:
        """``sha256:`` content hash of the canonical manifest bytes."""
        return "sha256:" + hashlib.sha256(self.canonical_bytes()).hexdigest()

    @property
    def unmanifested(self) -> tuple[ContextManifestEntry, ...]:
        """The entries the deriver could not resolve, in declared order."""
        return tuple(entry for entry in self.entries if entry.unmanifested)


@dataclass(frozen=True, slots=True)
class ManifestDivergence:
    """The first position at which two manifests disagree.

    Attributes:
        index: Zero-based position of the first differing entry.
        path: The declared path at that position, taken from whichever side has
            an entry there.
        left: The entry on the left-hand manifest, or None when that manifest is
            shorter.
        right: The entry on the right-hand manifest, or None when that manifest
            is shorter.
    """

    index: int
    path: str
    left: ContextManifestEntry | None
    right: ContextManifestEntry | None


def _normalise_declared(raw: str) -> str:
    """Fold a declared path to repository-relative POSIX form.

    Both separators are folded, not just this platform's: a task is written on
    one host and derived on another, so ``src\\a.py`` has to normalise the same
    way everywhere.

    Args:
        raw: The declared path exactly as it appears on the task.

    Returns:
        The normalised path, or *raw* unchanged when it carries a shape
        (emptiness, a NUL byte) that the containment barrier must see verbatim
        in order to reject it.
    """
    if not raw or "\x00" in raw:
        return raw
    return posixpath.normpath(raw.replace("\\", "/"))


def _resolve(repo_root: Path, declared: str) -> tuple[Path | None, str]:
    """Resolve one normalised declared path under *repo_root*.

    Args:
        repo_root: The repository root the path must stay inside.
        declared: The normalised declared path.

    Returns:
        ``(path, "")`` when the path resolves to a readable regular file inside
        the root, otherwise ``(None, reason)`` with a member of
        :data:`REASON_CODES`.
    """
    if declared in _ROOT_SPELLINGS:
        return None, REASON_INVALID_PATH
    try:
        validate_relative_path(declared, label="declared path")
    except PathContainmentError:
        return None, REASON_INVALID_PATH
    try:
        resolved = contained_subpath(repo_root, declared, label="declared path")
    except PathTooLongError:
        # A capacity failure, not an escape: the path is contained, it simply
        # cannot name a file on this filesystem.
        return None, REASON_INVALID_PATH
    except PathContainmentError:
        return None, REASON_OUTSIDE_ROOT
    if not resolved.exists():
        return None, REASON_MISSING
    if not resolved.is_file():
        return None, REASON_NOT_A_FILE
    return resolved, ""


def _digest_file(path: Path) -> str | None:
    """Return ``sha256:<hex>`` of *path*'s bytes, or None when unreadable."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(_CHUNK_BYTES):
                digest.update(chunk)
    except OSError:
        return None
    return "sha256:" + digest.hexdigest()


def _entry_for(repo_root: Path, declared: str) -> ContextManifestEntry:
    """Build the manifest entry for one normalised declared path."""
    resolved, reason = _resolve(repo_root, declared)
    if resolved is None:
        return ContextManifestEntry(path=declared, digest="", unmanifested=True, reason=reason)
    digest = _digest_file(resolved)
    if digest is None:
        return ContextManifestEntry(path=declared, digest="", unmanifested=True, reason=REASON_UNREADABLE)
    return ContextManifestEntry(path=declared, digest=digest, unmanifested=False, reason="")


def derive_context_manifest(*, repo_root: Path | str, declared_paths: Sequence[str] | Iterable[str]) -> ContextManifest:
    """Derive the content-addressed context manifest for a task's declared paths.

    Args:
        repo_root: The repository root every declared path is resolved under.
            Trusted by configuration; the declared paths are not.
        declared_paths: The task's declared path set, in declared order --
            ``Task.owned_files`` at the call site.

    Returns:
        A :class:`ContextManifest` whose entries follow the declared order with
        duplicate spellings of the same path collapsed to one entry. Every
        declared path produces exactly one entry: a resolvable file is
        content-addressed, and anything else is recorded ``unmanifested`` with
        its reason code rather than dropped.
    """
    root = Path(repo_root)
    seen: set[str] = set()
    entries: list[ContextManifestEntry] = []
    for raw in declared_paths:
        declared = _normalise_declared(raw)
        if declared in seen:
            continue
        seen.add(declared)
        entries.append(_entry_for(root, declared))
    return ContextManifest(v=CONTEXT_MANIFEST_VERSION, entries=tuple(entries))


def first_manifest_divergence(left: ContextManifest, right: ContextManifest) -> ManifestDivergence | None:
    """Return the first position at which two manifests disagree.

    Positional, not set-based: the declared order is part of what the manifest
    asserts, so a reordered context set diverges at the first moved entry rather
    than comparing equal.

    Args:
        left: One manifest.
        right: The manifest to compare it against.

    Returns:
        The first :class:`ManifestDivergence`, or None when the two manifests
        carry identical entries in identical order.
    """
    for index in range(max(len(left.entries), len(right.entries))):
        lhs = left.entries[index] if index < len(left.entries) else None
        rhs = right.entries[index] if index < len(right.entries) else None
        if lhs == rhs:
            continue
        path = lhs.path if lhs is not None else (rhs.path if rhs is not None else "")
        return ManifestDivergence(index=index, path=path, left=lhs, right=rhs)
    return None
