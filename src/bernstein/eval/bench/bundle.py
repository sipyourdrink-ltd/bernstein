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

#: ``receipt["status"]`` for a task a budget refused before it ran.
#:
#: The marker lives here, beside the receipt it belongs to, because it is
#: read by three places that have to agree -- the verifier, which scores a
#: refusal instead of replaying it; the comparison, which counts refusals so
#: a budget-cut run cannot read as a cheaper complete one; and the CLI
#: summary. They disagreed: the first two looked at ``receipt.status`` and
#: the third at ``harness_output["refusal"]``, so a receipt carrying one and
#: not the other verified clean while going uncounted.
#:
#: ``status`` is the canonical field of the two. It is the semantic marker
#: and it is inside the receipt, which the stored receipt hash covers.
#:
#: Read it through :func:`is_refusal` rather than comparing to it: a shared
#: constant still leaves four call sites free to ask a different question of
#: it, which is how they came apart the first time.
REFUSED_STATUS: str = "refused"


def is_refusal(receipt: Mapping[str, Any]) -> bool:
    """Whether *receipt* records a task a budget refused before it ran."""
    return receipt.get("status") == REFUSED_STATUS


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
    # Resource metrics (#5464). Bound into the bundle hash through to_dict,
    # but only when set: a bundle written before these fields existed has
    # none of them, and its stored hash was computed without them, so
    # emitting zeros on reload would fail the very hash check that guards
    # it against tampering. Same rule as SubmissionBundle.holdout_hash.
    #
    # ``None`` means "the harness did not report this", which is not the same
    # claim as ``0``. A bundle is evidence, so an unreported cost must not be
    # readable as "this task was free" — and a genuinely reported ``0`` must
    # still count as reported, which a truthiness test cannot express.
    tokens: int | None = None
    cost_usd: float | None = None
    duration_seconds: float | None = None

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

    def has_resource_metrics(self) -> bool:
        """True when any resource metric was *reported* for this task.

        Presence, not truthiness: a harness that genuinely reported zero
        tokens has reported a metric, and a task with nothing reported has
        not. The old truthiness test conflated the two, so a real zero was
        indistinguishable from silence.
        """
        return any(m is not None for m in (self.tokens, self.cost_usd, self.duration_seconds))

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
        }
        # Each metric is emitted only if it was reported. Emitting a key whose
        # value is null would put "unknown" and "zero" in the same shape for
        # any consumer that coerces, so absence carries the distinction.
        if self.tokens is not None:
            d["tokens"] = self.tokens
        if self.cost_usd is not None:
            d["cost_usd"] = self.cost_usd
        if self.duration_seconds is not None:
            d["duration_seconds"] = self.duration_seconds
        return d


# ---------------------------------------------------------------------------
# Harness fingerprint
# ---------------------------------------------------------------------------

FINGERPRINT_SCHEMA_VERSION = 1

#: Schema version of the submission bundle itself (#5464).
#:
#: 1 is the implicit version of every bundle written before this field
#: existed; it is never written, only inferred on load. 2 adds the optional
#: per-task resource metrics (``tokens`` / ``cost_usd`` / ``duration_seconds``),
#: which are omitted rather than zero-filled when the harness did not report
#: them — a reader must know which of the two shapes it is holding before it
#: can tell "no cost recorded" from "cost was zero".
BUNDLE_SCHEMA_VERSION = 2


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
    # Lambda value for scoring trade-off (if applicable, otherwise default).
    lambda_value: float = 0.5
    # Schema version of this bundle (#5464). Newly built bundles declare the
    # current version; `from_dict` infers 1 for a bundle written before the
    # field existed, so the hash of an old bundle still recomputes to its
    # stored value — same backward-compatibility rule as `holdout_hash`.
    schema_version: int = BUNDLE_SCHEMA_VERSION

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

    def refused_results(self) -> list[TaskResult]:
        """Tasks a budget refused before they ran.

        The one place the answer is computed, so the CLI summary, the
        comparison's refusal counts and any later reader cannot drift into
        asking it differently.
        """
        return [r for r in self.task_results if is_refusal(r.receipt)]

    @property
    def pass_rate(self) -> float:
        if not self.task_results:
            return 0.0
        return sum(1 for r in self.task_results if r.passed) / len(self.task_results)

    # The totals sum what was reported and ignore what was not. An unreported
    # metric contributes nothing rather than a zero, so a suite where half the
    # tasks reported no cost sums the half that did instead of quietly
    # averaging the other half down.
    @property
    def total_tokens(self) -> int:
        return sum(r.tokens for r in self.task_results if r.tokens is not None)

    @property
    def total_cost_usd(self) -> float:
        return sum(r.cost_usd for r in self.task_results if r.cost_usd is not None)

    @property
    def total_duration_seconds(self) -> float:
        return sum(r.duration_seconds for r in self.task_results if r.duration_seconds is not None)

    @property
    def resolve_rate(self) -> float:
        """Proportion of tasks that passed among those that were attempted (not abstained)."""
        if not self.task_results:
            return 0.0
        # In the context of SubmissionBundle, we consider a task "passed" if it succeeded
        # For now, we'll use the passed field as equivalent to resolved
        # Attempted = total tasks (since we don't have explicit abstention tracking in TaskResult yet)
        # This is a placeholder implementation - in a real system, TaskResult would need to track abstention
        passed_count = sum(1 for r in self.task_results if r.passed)
        total_count = len(self.task_results)
        return passed_count / total_count if total_count > 0 else 0.0

    @property
    def abstain_rate(self) -> float:
        """Proportion of tasks that were abstained (declined to answer)."""
        if not self.task_results:
            return 0.0
        # Placeholder - in a real system, we'd need to track abstentions in TaskResult
        # For now, return 0.0 as we don't have abstention data
        return 0.0

    @property
    def confident_error_rate(self) -> float:
        """Proportion of tasks that were confidently wrong among those attempted."""
        if not self.task_results:
            return 0.0
        # Placeholder - in a real system, we'd need to distinguish between different failure types
        # For now, we'll treat all non-passed tasks as confident errors (simplification)
        passed_count = sum(1 for r in self.task_results if r.passed)
        total_count = len(self.task_results)
        if total_count == 0:
            return 0.0
        return (total_count - passed_count) / total_count

    # ------------------------------------------------------------------
    # Content hash. Covers the suite identity, the submission time, the
    # scheduler config, every task record (resource metrics included, when
    # set) and holdout_hash when set. It deliberately leaves out the
    # signature and signer_fingerprint, and every field that is recomputed
    # from the task records on read -- overall_score, pass_rate, the three
    # total_* sums, harness_fingerprint -- so a writer that did not emit
    # those keys and one that does hash a bundle identically.
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
        # Bound into the hash only from version 2 onward. A version-1 bundle
        # never carried the key, and its stored hash was computed without it,
        # so adding it unconditionally would fail every pre-existing bundle's
        # integrity check on reload.
        if self.schema_version > 1:
            payload_dict["schema_version"] = self.schema_version
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
            "total_tokens": self.total_tokens,
            "total_cost_usd": self.total_cost_usd,
            "total_duration_seconds": self.total_duration_seconds,
            "lambda_value": self.lambda_value,
            "resolve_rate": self.resolve_rate,
            "abstain_rate": self.abstain_rate,
            "confident_error_rate": self.confident_error_rate,
            "task_results": [r.to_dict() for r in self.task_results],
            "signature": self.signature,
            "signer_fingerprint": self.signer_fingerprint,
        }
        if self.holdout_hash:
            d["holdout_hash"] = self.holdout_hash
        if self.schema_version > 1:
            d["schema_version"] = self.schema_version
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
                # Absent stays absent. Restoring a missing metric as 0 would
                # invent the reading on reload and change the recomputed hash
                # against the stored one.
                tokens=None if r.get("tokens") is None else int(r["tokens"]),
                cost_usd=None if r.get("cost_usd") is None else float(r["cost_usd"]),
                duration_seconds=(None if r.get("duration_seconds") is None else float(r["duration_seconds"])),
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
            lambda_value=raw.get("lambda_value", 0.5),
            # A bundle with no declared version is a version-1 bundle. Do not
            # default it to the current version: that would add the key to the
            # recomputed hash payload and break its stored hash.
            schema_version=int(raw.get("schema_version", 1)),
        )
        # Integrity guard: recompute hash and compare.
        if bundle.bundle_hash() != raw["bundle_hash"]:
            raise ValueError(
                f"Bundle hash mismatch: stored {raw['bundle_hash']!r} "
                f"!= recomputed {bundle.bundle_hash()!r}. "
                "The bundle file may have been tampered with."
            )
        return bundle
