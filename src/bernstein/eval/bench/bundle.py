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
    from collections.abc import Mapping
    from pathlib import Path

# ---------------------------------------------------------------------------
# Per-task result embedded in a bundle
# ---------------------------------------------------------------------------


#: Default penalty for a wrong answer under expected-value scoring (#5567).
#: A bundle that stores this value is hashed exactly as it was before the
#: field existed, so published bundle hashes do not move.
_DEFAULT_LAMBDA_PENALTY = 1.0


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
    # Whether the task ended with a declared abstention.
    abstained: bool = False
    abstention_reason: str = ""
    # Declared confidence probability in [0.0, 1.0].
    confidence: float = 1.0
    # The task was not evaluated at all -- excluded from every denominator,
    # mirroring ``InstanceStatus == "skipped"`` in benchmarks/swe_bench/metrics.py.
    # A skipped task is not a wrong answer and must not be scored as one.
    skipped: bool = False

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
        d: dict[str, Any] = {
            "task_id": self.task_id,
            "task_hash": self.task_hash,
            "receipt": self.receipt,
            # Persist the hash that was computed at emit time so the verifier
            # can compare a fresh recompute against it.
            "receipt_hash": self.stored_receipt_hash,
            "passed": self.passed,
            "score": self.score,
            "harness_output": self.harness_output,
            "abstained": self.abstained,
            "abstention_reason": self.abstention_reason,
            "confidence": self.confidence,
        }
        # Omitted when False so every bundle written before this field existed
        # hashes exactly as it did (same rule as ``lambda_penalty``).
        if self.skipped:
            d["skipped"] = True
        return d


# ---------------------------------------------------------------------------
# Harness fingerprint
# ---------------------------------------------------------------------------

FINGERPRINT_SCHEMA_VERSION = 1


def harness_fingerprint(scheduler_config: Mapping[str, Any]) -> str:
    """Canonical fingerprint of the harness settings that shaped a run.

    sha256 over the canonical JSON (sorted keys, no whitespace) of the
    full ``scheduler_config`` mapping — the single settings carrier every
    runner passes to ``adapter.run_task``: decomposition config, prompt
    template, tool allowlist, effort/thinking budget, retry policy,
    timeouts, sandbox type, and any key a caller adds.

    The whole mapping is hashed rather than a named-key allowlist, so a
    setting nobody thought to list still changes the fingerprint instead
    of silently drifting two runs onto one identity.  Nothing outside the
    mapping participates: wall-clock time, paths, and run identity are
    not harness settings, and a fingerprint that included them would
    never match across runs.
    """
    canonical = json.dumps(
        {"schema": FINGERPRINT_SCHEMA_VERSION, "settings": scheduler_config},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


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
    holdout_hash: str = ""
    # Canonical fingerprint of the harness settings (scheduler_config).
    # Derived at construction when not supplied; from_dict passes the
    # stored value so a tampered one survives load for compare to catch,
    # and a pre-#5568 bundle derives it on load instead of failing.
    harness_fingerprint: str = ""

    # Penalty parameter for incorrect answers (wrong = -lambda).
    lambda_penalty: float = _DEFAULT_LAMBDA_PENALTY

    def __post_init__(self) -> None:
        # If caller didn't supply a fingerprint, derive it now — same
        # emit-time contract as TaskResult.stored_receipt_hash.
        if not self.harness_fingerprint:
            self.harness_fingerprint = harness_fingerprint(self.scheduler_config)

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

    @property
    def abstained_count(self) -> int:
        return sum(1 for r in self.task_results if r.abstained and not r.skipped)

    @property
    def skipped_count(self) -> int:
        return sum(1 for r in self.task_results if r.skipped)

    @property
    def evaluated_count(self) -> int:
        """Tasks that were actually run: everything but the skipped ones."""
        return len(self.task_results) - self.skipped_count

    @property
    def resolved_count(self) -> int:
        return sum(1 for r in self.task_results if r.passed and not r.abstained and not r.skipped)

    @property
    def wrong_count(self) -> int:
        return sum(1 for r in self.task_results if not r.passed and not r.abstained and not r.skipped)

    @property
    def attempted_count(self) -> int:
        return self.evaluated_count - self.abstained_count

    @property
    def resolve_rate(self) -> float:
        """Resolve rate: resolved / attempted (abstentions excluded from denominator)."""
        if self.attempted_count <= 0:
            return 0.0
        return self.resolved_count / self.attempted_count

    @property
    def abstain_rate(self) -> float:
        """Abstain rate: abstained / evaluated (skipped tasks excluded)."""
        if self.evaluated_count <= 0:
            return 0.0
        return self.abstained_count / self.evaluated_count

    @property
    def confident_error_rate(self) -> float:
        """Confident-error rate: wrong / (wrong + resolved)."""
        denominator = self.wrong_count + self.resolved_count
        if denominator <= 0:
            return 0.0
        return self.wrong_count / denominator

    @property
    def expected_value(self) -> float:
        """Expected value under lambda penalty: (resolved * 1.0 + abstained * 0.0 + wrong * -lambda) / total."""
        if self.evaluated_count <= 0:
            return 0.0
        total_ev = self.resolved_count * 1.0 + self.abstained_count * 0.0 + self.wrong_count * (-self.lambda_penalty)
        return total_ev / self.evaluated_count

    @property
    def brier_score(self) -> float:
        """Brier score over predicted confidence vs binary outcome.

        Skipped tasks carry no outcome to be wrong about, so they are left
        out of the mean rather than scored against a fabricated one.
        """
        squared_errors = [
            (r.confidence - (1.0 if r.passed and not r.abstained else 0.0)) ** 2
            for r in self.task_results
            if not r.skipped
        ]
        if not squared_errors:
            return 0.0
        return sum(squared_errors) / len(squared_errors)

    # ------------------------------------------------------------------
    # Content hash (covers everything *except* the signature field)
    # ------------------------------------------------------------------

    def bundle_hash(self) -> str:
        if self._bundle_hash is None:
            self._bundle_hash = self._compute_hash()
        return self._bundle_hash

    def _compute_hash(self) -> str:
        payload_dict: dict[str, Any] = {
            "suite_hash": self.suite_hash,
            "suite_version": self.suite_version,
            "submitted_at": self.submitted_at,
            "scheduler_config": self.scheduler_config,
            "task_results": [r.to_dict() for r in self.task_results],
        }
        if self.holdout_hash:
            payload_dict["holdout_hash"] = self.holdout_hash
        # lambda reorders `bench compare`, so a bundle that carries a
        # non-default one has to commit to it: two bundles with identical
        # task results but different lambda rank differently and must not
        # share a hash. Included only when it is not the default, so every
        # bundle written before #5567 keeps the hash it was published with.
        if self.lambda_penalty != _DEFAULT_LAMBDA_PENALTY:
            payload_dict["lambda_penalty"] = self.lambda_penalty
        payload = json.dumps(
            payload_dict,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(payload).hexdigest()

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "bundle_hash": self.bundle_hash(),
            "suite_hash": self.suite_hash,
            "suite_version": self.suite_version,
            "submitted_at": self.submitted_at,
            "scheduler_config": self.scheduler_config,
            "harness_fingerprint": self.harness_fingerprint,
            "overall_score": self.overall_score,
            "pass_rate": self.pass_rate,
            "resolve_rate": self.resolve_rate,
            "abstain_rate": self.abstain_rate,
            "confident_error_rate": self.confident_error_rate,
            "lambda_penalty": self.lambda_penalty,
            "expected_value": self.expected_value,
            "brier_score": self.brier_score,
            "task_results": [r.to_dict() for r in self.task_results],
            "signature": self.signature,
            "signer_fingerprint": self.signer_fingerprint,
        }
        if self.holdout_hash:
            d["holdout_hash"] = self.holdout_hash
        return d

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path) -> SubmissionBundle:
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> SubmissionBundle:
        """Rebuild a bundle from its serialised form, checking the stored hash.

        Split out of :meth:`load` so a bundle embedded inside another artefact
        (a drift observation, say) is rebuilt through exactly the same
        integrity guard as one read from its own file.
        """
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
                abstained=r.get("abstained", False),
                abstention_reason=r.get("abstention_reason", ""),
                confidence=r.get("confidence", 1.0),
                skipped=r.get("skipped", False),
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
            holdout_hash=raw.get("holdout_hash", ""),
            harness_fingerprint=raw.get("harness_fingerprint", ""),
            lambda_penalty=raw.get("lambda_penalty", 1.0),
        )
        # Integrity guard: recompute hash and compare.
        if bundle.bundle_hash() != raw["bundle_hash"]:
            raise ValueError(
                f"Bundle hash mismatch: stored {raw['bundle_hash']!r} "
                f"!= recomputed {bundle.bundle_hash()!r}. "
                "The bundle file may have been tampered with."
            )
        return bundle
