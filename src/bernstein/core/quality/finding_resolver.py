"""Finding reference existence verifier (quality gate).

Extracts finding reference-like spans from agent-produced artefact reports
(Markdown with YAML frontmatter or JSON sidecar) and verifies that every
reference resolves to a real finding artifact in the task store. Missing or
altered finding receipts are returned as ``unresolved`` so the gate can block
the merge.

Design notes
------------

* Public surface: :func:`verify_finding_references` plus the
  :class:`FindingReferenceReport` dataclass. Both are import-stable.
* Offline mode skips every task store call -- only locally available findings
  are treated as resolvable. This keeps the gate usable in air-gapped CI.
* Hot-path: when the gate is not opted into via
  ``bernstein.yaml :: quality.verify_finding_references: true`` the module is
  not imported by :mod:`bernstein.core.quality.gate_pipeline` and costs zero.
* No third-party HTTP libraries. The verifier uses local task store queries.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from pathlib import Path

from bernstein.core.evidence.run_artifacts import (
    ARTIFACT_TYPE_FINDING,
    read_artifact_rows,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Span extraction regexes
# ---------------------------------------------------------------------------

# Finding reference pattern matches "[FINDING:task-id:key:version]" or similar
# formats that might appear in report sidecars or comments.
_FINDING_REF_RE: Final[re.Pattern[str]] = re.compile(
    r"""(?x)
    \[
    FINDING:
    ([A-Za-z0-9_.:-]+)  # task_id
    :
    ([A-Za-z0-9][A-Za-z0-9_.-]{0,127})  # key
    :
    (\d+)               # version
    \]
    """,
)

# Alternative format: "finding:task-id:key" (latest version)
_FINDING_REF_LATEST_RE: Final[re.Pattern[str]] = re.compile(
    r"""(?x)
    \[
    FINDING:
    ([A-Za-z0-9_.:-]+)  # task_id
    :
    ([A-Za-z0-9][A-Za-z0-9_.-]{0,127})  # key
    \]
    """,
)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FindingReference:
    """A single finding reference extracted from an artefact report sidecar.

    Attributes:
        task_id: The task ID where the finding should be located.
        key: The artifact key for the finding.
        version: The version number (None means latest).
        value: The verbatim reference text (e.g., "[FINDING:task-123:finding:1]").
        offset: Byte offset of the reference in the original artefact text.
    """

    task_id: str
    key: str
    version: int | None
    value: str
    offset: int


@dataclass(frozen=True)
class FindingReferenceReport:
    """Result of running the verifier against a single artefact report.

    Attributes:
        total: Total number of finding reference spans extracted.
        resolved: References that resolved to existing, matching finding artifacts.
        unresolved: References that did not resolve (missing, altered, or wrong version).
    """

    total: int
    resolved: tuple[FindingReference, ...] = field(default_factory=tuple)
    unresolved: tuple[FindingReference, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        """Return True when no finding reference is unresolved."""
        return not self.unresolved

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable rendering of the report."""
        return {
            "total": self.total,
            "ok": self.ok,
            "resolved": [_finding_reference_to_dict(r) for r in self.resolved],
            "unresolved": [_finding_reference_to_dict(r) for r in self.unresolved],
        }


def _finding_reference_to_dict(r: FindingReference) -> dict[str, object]:
    return {
        "task_id": r.task_id,
        "key": r.key,
        "version": r.version,
        "value": r.value,
        "offset": r.offset,
    }


# ---------------------------------------------------------------------------
# Span extraction
# ---------------------------------------------------------------------------


def extract_finding_references(text: str) -> list[FindingReference]:
    """Extract every finding reference-like span from *text*.

    The order of references in the returned list matches their order of
    appearance in *text*. Overlapping matches are deduplicated with preference
    given to the more specific format (with version).

    Args:
        text: Free-form artefact text (typically a report body with sidecar).

    Returns:
        List of :class:`FindingReference` records.
    """
    references: list[FindingReference] = []
    occupied: list[tuple[int, int]] = []

    def _claim(start: int, end: int) -> bool:
        for o_start, o_end in occupied:
            if start < o_end and end > o_start:
                return False
        occupied.append((start, end))
        return True

    # Process version-specific references first (more specific)
    for match in _FINDING_REF_RE.finditer(text):
        task_id = match.group(1)
        key = match.group(2)
        version = int(match.group(3))
        value = match.group(0)  # Full match including brackets
        if _claim(match.start(), match.end()):
            references.append(
                FindingReference(
                    task_id=task_id,
                    key=key,
                    version=version,
                    value=value,
                    offset=match.start(),
                )
            )

    # Process latest-version references
    for match in _FINDING_REF_LATEST_RE.finditer(text):
        task_id = match.group(1)
        key = match.group(2)
        value = match.group(0)  # Full match including brackets
        # Check if this overlaps with any already claimed span
        if _claim(match.start(), match.end()):
            references.append(
                FindingReference(
                    task_id=task_id,
                    key=key,
                    version=None,  # None means latest version
                    value=value,
                    offset=match.start(),
                )
            )

    references.sort(key=lambda r: r.offset)
    return references


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def _resolve_finding_reference(
    reference: FindingReference,
    sdd_dir: Path,
    *,
    offline: bool = False,
) -> str:
    """Resolve a finding reference against the task store.

    Returns:
        One of ``"resolved"``, ``"unresolved"``, or ``"skipped"``.
    """
    if offline:
        # In offline mode, we cannot resolve references - treat as skipped
        return "skipped"

    try:
        # Read all artifact rows for the referenced task
        rows = read_artifact_rows(sdd_dir, reference.task_id)
        if not rows:
            return "unresolved"

        # Filter rows by key and artifact type (finding)
        matching_rows = [row for row in rows if row.key == reference.key and row.artifact_type == ARTIFACT_TYPE_FINDING]

        if not matching_rows:
            return "unresolved"

        # If version specified, find that exact version
        if reference.version is not None:
            versioned_rows = [row for row in matching_rows if row.version == reference.version]
            if not versioned_rows:
                return "unresolved"
        # If we got here, the finding exists
        return "resolved"

    except Exception as exc:  # defensive: any error means we can't resolve
        logger.warning(
            "finding reference resolution failed for %s: %s",
            reference.value,
            exc,
        )
        return "unresolved"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def verify_finding_references(
    text: str,
    sdd_dir: Path,
    *,
    offline: bool = False,
) -> FindingReferenceReport:
    """Verify every finding reference in *text* against the task store.

    Args:
        text: Free-form artefact text (typically a report body). Pass the raw
            markdown / plaintext body; the verifier handles its own extraction.
        sdd_dir: The ``.sdd`` directory of the run containing the task store.
        offline: When True, task store queries are skipped. All references
            resolve as ``skipped`` (never failed).

    Returns:
        :class:`FindingReferenceReport`.
    """
    references = extract_finding_references(text)

    resolved: list[FindingReference] = []
    unresolved: list[FindingReference] = []

    for reference in references:
        bucket = _resolve_finding_reference(reference, sdd_dir, offline=offline)
        if bucket == "resolved":
            resolved.append(reference)
        elif bucket == "unresolved":
            unresolved.append(reference)
        # skipped references are ignored (not counted as failures)

    return FindingReferenceReport(
        total=len(references),
        resolved=tuple(resolved),
        unresolved=tuple(unresolved),
    )


# ---------------------------------------------------------------------------
# Gate-pipeline adapter
# ---------------------------------------------------------------------------


def gate_verify_finding_references(
    artefact_text: str,
    sdd_dir: Path,
    *,
    offline: bool = False,
) -> tuple[bool, str]:
    """Adapter used by the quality gate pipeline.

    Returns:
        Tuple ``(passed, details)`` where ``passed`` is True iff no
        reference went unresolved, and ``details`` is a one-line summary
        suitable for inclusion in a :class:`GateResult`.
    """
    report = verify_finding_references(
        artefact_text,
        sdd_dir,
        offline=offline,
    )
    if report.ok:
        return True, (
            f"finding_reference_verifier: {len(report.resolved)}/{report.total} resolved"
            f" ({len([r for r in report.resolved if r.version is None])} latest, "
            f"{len([r for r in report.resolved if r.version is not None])} versioned)"
        )
    unresolved_summary = ", ".join(f"{r.value}" for r in report.unresolved[:5])
    if len(report.unresolved) > 5:
        unresolved_summary += f" (+{len(report.unresolved) - 5} more)"
    return False, f"finding_reference_verifier: {len(report.unresolved)} unresolved -- {unresolved_summary}"


__all__ = [
    "FindingReference",
    "FindingReferenceReport",
    "extract_finding_references",
    "gate_verify_finding_references",
    "verify_finding_references",
]
