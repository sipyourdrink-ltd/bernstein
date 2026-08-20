"""Verification and hashing helpers for tiered context compaction.

Provides content hashing over pre-compaction regions and referenced artifacts,
plus verification to detect post-compaction drift between summarized context
and underlying disk state.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from bernstein.core.lineage.spine import content_hash_of

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from bernstein.core.memory.compaction.tiers import TierResult
    from bernstein.core.observability.traces import TraceStep

#: Sentinel content hash recorded when a referenced artifact does not exist on disk.
ABSENT_HASH: str = "absent"


def compute_source_content_hash(text: str) -> str:
    """Return the SHA-256 content hash of UTF-8 encoded ``text``."""
    return content_hash_of(text.encode("utf-8"))


def compute_referenced_content_hashes(
    paths: Sequence[str] | Iterable[str],
    *,
    precomputed: Mapping[str, str] | None = None,
    root_dir: Path | str | None = None,
) -> dict[str, str]:
    """Compute or collect content hashes for referenced artifact paths.

    For each path:
    - If already present in ``precomputed``, the precomputed hash is kept.
    - If the file exists on disk, ``content_hash_of`` is computed over its raw bytes.
    - If the file does not exist, ``ABSENT_HASH`` (``"absent"``) is recorded so
      non-existence is explicitly captured rather than silently dropped.

    Args:
        paths: Sequence of file or artifact paths referenced by the compacted region.
        precomputed: Optional mapping of path to precomputed content hash.
        root_dir: Optional base directory to resolve relative paths against.

    Returns:
        Mapping of artifact path to content hash (or ``ABSENT_HASH``).
    """
    result: dict[str, str] = dict(precomputed or {})
    base = Path(root_dir) if root_dir is not None else None

    for path_str in paths:
        if path_str in result:
            continue
        p = (base / path_str) if base is not None else Path(path_str)
        if p.is_file():
            try:
                result[path_str] = content_hash_of(p.read_bytes())
            except OSError:
                result[path_str] = ABSENT_HASH
        else:
            result[path_str] = ABSENT_HASH

    return result


@dataclass(frozen=True)
class ArtifactDivergence:
    """Discrepancy between an artifact's recorded hash and its current hash.

    Attributes:
        path: Path to the artifact.
        expected_hash: Hash recorded at compaction time (or ``"absent"``).
        actual_hash: Current hash computed from the filesystem (or ``"absent"``).
    """

    path: str
    expected_hash: str
    actual_hash: str

    @property
    def is_divergent(self) -> bool:
        """Whether the expected and actual hashes mismatch."""
        return self.expected_hash != self.actual_hash

    def __str__(self) -> str:
        return f"{self.path}: expected {self.expected_hash}, got {self.actual_hash}"


@dataclass(frozen=True)
class CompactionVerificationResult:
    """Outcome of verifying referenced artifacts for a compacted step or result.

    Attributes:
        valid: True if all referenced artifacts match their recorded hashes.
        divergences: Tuple of divergences found (empty when valid).
        checked_count: Total number of referenced artifacts checked.
    """

    valid: bool
    divergences: tuple[ArtifactDivergence, ...] = ()
    checked_count: int = 0

    def report(self) -> str:
        """Return a human-readable summary of the verification outcome."""
        if self.valid:
            return f"Verified {self.checked_count} referenced artifact(s): all match recorded hashes."
        div_lines = "\n".join(f"  - {d}" for d in self.divergences)
        return (
            f"Divergence detected across {len(self.divergences)}/{self.checked_count} "
            f"referenced artifact(s):\n{div_lines}"
        )


def verify_compaction_references(
    references: Mapping[str, str],
    *,
    root_dir: Path | str | None = None,
) -> CompactionVerificationResult:
    """Check if referenced artifacts still hash to their compaction-time hashes.

    Args:
        references: Mapping of artifact path to expected content hash.
        root_dir: Optional root directory to resolve relative paths against.

    Returns:
        A :class:`CompactionVerificationResult` reporting whether hashes match
        and detailing any divergences.
    """
    if not references:
        return CompactionVerificationResult(valid=True, divergences=(), checked_count=0)

    base = Path(root_dir) if root_dir is not None else None
    divergences: list[ArtifactDivergence] = []

    for path_str, expected_hash in references.items():
        p = (base / path_str) if base is not None else Path(path_str)
        if p.is_file():
            try:
                actual_hash = content_hash_of(p.read_bytes())
            except OSError:
                actual_hash = ABSENT_HASH
        else:
            actual_hash = ABSENT_HASH

        if actual_hash != expected_hash:
            divergences.append(
                ArtifactDivergence(
                    path=path_str,
                    expected_hash=expected_hash,
                    actual_hash=actual_hash,
                )
            )

    return CompactionVerificationResult(
        valid=len(divergences) == 0,
        divergences=tuple(divergences),
        checked_count=len(references),
    )


def verify_compacted_step(
    step: TraceStep | TierResult | Mapping[str, Any],
    *,
    root_dir: Path | str | None = None,
) -> CompactionVerificationResult:
    """Verify that artifacts referenced by a compacted step have not diverged.

    Accepts a :class:`~bernstein.core.observability.traces.TraceStep`, a
    :class:`~bernstein.core.memory.compaction.tiers.TierResult`, or a raw dict.

    Args:
        step: Step, result, or dict containing compaction referenced hashes.
        root_dir: Optional root directory for resolving file paths.

    Returns:
        A :class:`CompactionVerificationResult`.
    """
    ref_hashes: Mapping[str, str] = {}
    if hasattr(step, "compaction_referenced_content_hashes"):
        ref_hashes = cast("Mapping[str, str]", step.compaction_referenced_content_hashes)
    elif hasattr(step, "referenced_content_hashes"):
        ref_hashes = cast("Mapping[str, str]", step.referenced_content_hashes)
    elif isinstance(step, dict):
        raw = step.get(
            "compaction_referenced_content_hashes",
            step.get("referenced_content_hashes", {}),
        )
        if isinstance(raw, (dict, Mapping)):
            ref_hashes = cast("Mapping[str, str]", raw)
    else:
        ref_hashes = {}

    return verify_compaction_references(ref_hashes, root_dir=root_dir)
