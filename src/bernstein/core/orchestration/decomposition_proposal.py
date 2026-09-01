"""Decomposition proposal artifact schema for retry exhaustion.

This module defines the DecompositionProposal dataclass that captures the
state of repeated task failures on the same issue, triggering a proposal
to decompose the issue into smaller sub-issues.

The proposal includes a content-addressed identifier (SHA256 of canonical
JSON) to enable deduplication and auditability.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from bernstein.core.tasks.models import Task


@dataclass(frozen=True)
class DecompositionProposal:
    """Immutable proposal to decompose an issue after repeated task failures.

    Attributes:
        issue_number: GitHub issue number.
        repo: Repository in "owner/name" format.
        evidence_digests: Ordered tuple of SHA256 hashes of failed verification evidence.
        attempts: List of attempt dictionaries, each containing:
            - attempt_number: int
            - model: str
            - effort: str
            - failure_reason: str
            - terminal_reason: str
        content_address: SHA256 hash of the canonical JSON bytes of the above fields,
            prefixed with "sha256:".
    """

    issue_number: int
    repo: str
    evidence_digests: tuple[str, ...]
    attempts: list[dict[str, Any]]
    content_address: str = field(init=False)

    def __post_init__(self) -> None:
        """Compute content_address from the canonical bytes."""
        # content_hash() already returns "sha256:..." so don't re-prefix.
        object.__setattr__(self, "content_address", self.content_hash())

    def canonical_bytes(self) -> bytes:
        """Return deterministic canonical JSON bytes (sorted keys, no whitespace, UTF-8).

        Returns:
            Bytes of the canonical JSON representation.
        """
        # Build a dictionary with the fields in a fixed order for canonical JSON.
        # We sort the keys to ensure deterministic ordering.
        data: dict[str, Any] = {
            "attempts": self.attempts,
            "evidence_digests": self.evidence_digests,
            "issue_number": self.issue_number,
            "repo": self.repo,
        }
        # Use the shared canonical JSON function from artifacts.py.
        from bernstein.core.tasks.artifacts import _canonical_json_bytes

        return _canonical_json_bytes(data)

    def content_hash(self) -> str:
        """Return the SHA256 hex digest of the canonical bytes, prefixed with 'sha256:'.

        Returns:
            String in the format "sha256:<hex_digest>".
        """
        from bernstein.core.tasks.artifacts import content_hash

        return content_hash(self.canonical_bytes())

    @classmethod
    def from_task(cls, task: Task, failure_evidence: str) -> DecompositionProposal:
        """Create a DecompositionProposal from a failed task and its evidence.

        Args:
            task: The Task instance that failed.
            failure_evidence: A string describing the failure evidence (e.g., logs,
                test output, or verification failure). This will be hashed to
                produce an evidence digest.

        Returns:
            A new DecompositionProposal instance with the task's context and
            a single attempt entry.
        """
        # Extract issue_number and repo from task metadata.
        issue_number = task.metadata.get("issue_number")
        if issue_number is None:
            raise ValueError("Task metadata must contain 'issue_number' for decomposition proposals")

        repo = task.metadata.get("repo")
        if repo is None:
            # Fallback: try to infer from task.labels or other context? For now, require it.
            raise ValueError("Task metadata must contain 'repo' for decomposition proposals")

        # Compute evidence digest as SHA256 of the failure evidence string.
        evidence_digest = hashlib.sha256(failure_evidence.encode("utf-8")).hexdigest()

        # Build the attempts list with the current attempt.
        attempts = [
            {
                "attempt_number": task.retry_count + 1,  # retry_count is 0-indexed, so attempt 1 is first try
                "model": task.metadata.get("model", task.model or "unknown"),
                "effort": task.metadata.get("effort", task.effort or "unknown"),
                "failure_reason": task.result_summary or "",
                "terminal_reason": task.terminal_reason or "",
            }
        ]

        return cls(
            issue_number=issue_number,
            repo=repo,
            evidence_digests=(evidence_digest,),
            attempts=attempts,
        )

    def to_comment_body(self) -> str:
        """Generate a markdown comment body for posting to the GitHub issue.

        Returns:
            A string containing markdown formatted as:
            - Issue reference
            - Table of attempts
            - Evidence digests
        """
        lines = [
            f"## Decomposition Proposal for Issue #{self.issue_number}",
            "",
            "This issue has exceeded the retry limit for associated tasks. "
            "Consider decomposing it into smaller, more manageable sub-issues.",
            "",
            "### Attempts",
            "",
            "| Attempt | Model | Effort | Failure Reason | Terminal Reason |",
            "|---------|-------|--------|----------------|-----------------|",
        ]

        for idx, attempt in enumerate(self.attempts, start=1):
            lines.append(
                f"| {idx} "
                f"| {attempt.get('model', 'unknown')} "
                f"| {attempt.get('effort', 'unknown')} "
                f"| {attempt.get('failure_reason', '').replace('|', '\\|')} "
                f"| {attempt.get('terminal_reason', '').replace('|', '\\|')} |"
            )

        lines.extend(
            [
                "",
                "### Evidence Digests (SHA256)",
                "",
            ]
        )
        for digest in self.evidence_digests:
            lines.append(f"- `{digest}`")

        lines.extend(
            [
                "",
                f"*Proposal ID: {self.content_address}*",
            ]
        )

        return "\n".join(lines)
