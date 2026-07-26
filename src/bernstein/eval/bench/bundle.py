"""
bernstein-bench: submission bundle.

A bundle is the artefact that carries its own proof.  It binds:

    {suite_hash, per_task_receipts, scores, scheduler_config}

and is signed off the install identity (Ed25519 / agent_card_signer path).
The score only means something because the receipt exists to replay it.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Per-task result embedded in a bundle
# ---------------------------------------------------------------------------


@dataclass
class TaskResult:
    """
    One task's contribution to a submission bundle.

    ``receipt`` is the journal head + spine head today (or the
    ``bernstein verify run`` receipt when that companion surface ships).
    It is the replay substrate — without it the score has no meaning.

    ``stored_receipt_hash`` is the SHA-256 of the receipt bytes *as they were
    when the bundle was emitted*.  The verifier checks the current receipt
    bytes against this stored hash, so a byte-flip in the receipt is caught
    even when the verdict field is left unchanged.
    """

    task_id: str
    task_hash: str
    # The replayable run receipt: journal head hash + spine head hash.
    receipt: dict[str, Any]
    # Verdict from harness.py multiplicative scoring.
    passed: bool
    score: float  # [0.0, 1.0]
    # Raw harness output for debugging.
    harness_output: dict[str, Any] = field(default_factory=dict)
    # SHA-256 of the receipt bytes at emit time.  Populated by the runner at
    # construction time and restored verbatim from the JSON at load time.
    # The verifier recomputes this from the live receipt and compares.
    stored_receipt_hash: str = ""

    def __post_init__(self) -> None:
        # If caller didn't supply stored_receipt_hash, derive it now.
        if not self.stored_receipt_hash:
            self.stored_receipt_hash = self._compute_receipt_hash(self.receipt)

    @staticmethod
    def _compute_receipt_hash(receipt: dict[str, Any]) -> str:
        """Deterministic hash of receipt bytes (canonical JSON)."""
        canonical = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(canonical).hexdigest()

    def receipt_hash(self) -> str:
        """Return the *stored* receipt hash (set at emit time, not recomputed)."""
        return self.stored_receipt_hash

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_hash": self.task_hash,
            "receipt": self.receipt,
            # Persist the hash that was computed at emit time so the verifier
            # can compare a fresh recompute against it.
            "receipt_hash": self.stored_receipt_hash,
            "passed": self.passed,
            "score": self.score,
            "harness_output": self.harness_output,
        }


# ---------------------------------------------------------------------------
# Bundle
# ---------------------------------------------------------------------------


@dataclass
class SubmissionBundle:
    """
    The primary artefact of a ``bernstein bench run`` invocation.

    Score is recomputable by anyone from the replayable run receipts it
    embeds.  The leaderboard is a projection of verified bundles.
    """

    suite_hash: str
    suite_version: str
    task_results: list[TaskResult]
    scheduler_config: dict[str, Any]
    submitted_at: float = field(default_factory=time.time)
    # Ed25519 detached JWS signature filled by the signer (empty until signed).
    signature: str = ""
    # Install identity fingerprint (public-key fingerprint of the signer).
    signer_fingerprint: str = ""

    # Computed lazily.
    _bundle_hash: str | None = field(default=None, init=False, repr=False, compare=False)

    # ------------------------------------------------------------------
    # Derived metrics
    # ------------------------------------------------------------------

    @property
    def overall_score(self) -> float:
        if not self.task_results:
            return 0.0
        return sum(r.score for r in self.task_results) / len(self.task_results)

    @property
    def pass_rate(self) -> float:
        if not self.task_results:
            return 0.0
        return sum(1 for r in self.task_results if r.passed) / len(self.task_results)

    # ------------------------------------------------------------------
    # Content hash (covers everything *except* the signature field)
    # ------------------------------------------------------------------

    def bundle_hash(self) -> str:
        if self._bundle_hash is None:
            self._bundle_hash = self._compute_hash()
        return self._bundle_hash

    def _compute_hash(self) -> str:
        payload = json.dumps(
            {
                "suite_hash": self.suite_hash,
                "suite_version": self.suite_version,
                "submitted_at": self.submitted_at,
                "scheduler_config": self.scheduler_config,
                "task_results": [r.to_dict() for r in self.task_results],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(payload).hexdigest()

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "bundle_hash": self.bundle_hash(),
            "suite_hash": self.suite_hash,
            "suite_version": self.suite_version,
            "submitted_at": self.submitted_at,
            "scheduler_config": self.scheduler_config,
            "overall_score": self.overall_score,
            "pass_rate": self.pass_rate,
            "task_results": [r.to_dict() for r in self.task_results],
            "signature": self.signature,
            "signer_fingerprint": self.signer_fingerprint,
        }

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path) -> SubmissionBundle:
        raw = json.loads(path.read_text(encoding="utf-8"))
        task_results = [
            TaskResult(
                task_id=r["task_id"],
                task_hash=r["task_hash"],
                receipt=r["receipt"],
                passed=r["passed"],
                score=r["score"],
                harness_output=r.get("harness_output", {}),
                # Restore the hash that was stored at emit time — do NOT let
                # __post_init__ recompute it from the current receipt bytes.
                stored_receipt_hash=r["receipt_hash"],
            )
            for r in raw["task_results"]
        ]
        bundle = cls(
            suite_hash=raw["suite_hash"],
            suite_version=raw["suite_version"],
            task_results=task_results,
            scheduler_config=raw["scheduler_config"],
            submitted_at=raw["submitted_at"],
            signature=raw.get("signature", ""),
            signer_fingerprint=raw.get("signer_fingerprint", ""),
        )
        # Integrity guard: recompute hash and compare.
        if bundle.bundle_hash() != raw["bundle_hash"]:
            raise ValueError(
                f"Bundle hash mismatch: stored {raw['bundle_hash']!r} "
                f"!= recomputed {bundle.bundle_hash()!r}. "
                "The bundle file may have been tampered with."
            )
        return bundle
