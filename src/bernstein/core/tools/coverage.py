"""Tool coverage records and corpus digest computation (issue #3769)."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


def compute_corpus_digest(paths: Sequence[str | Path]) -> str:
    """Compute deterministic SHA-256 digest over a sequence of paths.

    Paths are converted to strings, sorted lexicographically, and joined by newline.
    """
    normalized = sorted(str(p) for p in paths)
    raw = "\n".join(normalized).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True)
class ToolCoverageRecord:
    """Coverage record describing the corpus and scope a tool invocation inspected.

    Attributes:
        file_count: Number of files / items covered.
        corpus_digest: Deterministic digest over the inspected corpus.
        coverage: Coverage completeness ("complete" or "partial").
        truncated: Whether the search/walk was truncated before normal termination.
        truncation_reason: Reason for truncation (e.g. "timeout", "limit_reached", "error"), or None.
        exit_status: Tool exit code or status string.
        exit_checked: Whether the exit status was verified.
    """

    file_count: int = 0
    corpus_digest: str = ""
    coverage: str = "complete"  # "complete" | "partial"
    truncated: bool = False
    truncation_reason: str | None = None
    exit_status: str | int | None = 0
    exit_checked: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolCoverageRecord:
        return cls(
            file_count=int(data.get("file_count", 0)),
            corpus_digest=str(data.get("corpus_digest", "")),
            coverage=str(data.get("coverage", "complete")),
            truncated=bool(data.get("truncated", False)),
            truncation_reason=data.get("truncation_reason"),
            exit_status=data.get("exit_status", 0),
            exit_checked=bool(data.get("exit_checked", True)),
        )
