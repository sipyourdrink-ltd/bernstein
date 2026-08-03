"""
bernstein-bench: deterministic pass^k reliability floor (issue #2933).

Best-of-N reporting overstates reliability: a task that passes once in
eight attempts and a task that passes every time both read as "passed".
An operator deciding whether to let an agent run unattended needs the
floor — does the task pass *every* time under the same conditions? — not
the ceiling.

Because Bernstein's scheduler is deterministic (no LLM in the coordination
loop, replayable journal), coordination can be held fixed across repeated
attempts, so an all-of-k metric measures the genuinely stochastic element
(model sampling) instead of coordination luck:

* ``pass@1`` — fraction of tasks where **at least one** of the ``k``
  attempts passed.  The ceiling; what best-of-N reporting shows.
* ``pass^k`` — fraction of tasks where **all** ``k`` attempts passed.
  The floor, and the headline number.

Estimator note
--------------
With per-attempt success probability ``p`` and ``n`` recorded attempts of
which ``c`` passed, the unbiased estimator of the all-of-k probability
``p^k`` is ``C(c, k) / C(n, k)`` (the chance that ``k`` attempts drawn
without replacement all passed).  This runner records exactly ``n = k``
attempts per task, where that estimator degenerates to the indicator
"all ``k`` attempts passed" — which is what the receipt seals.  It is a
point estimate, not a confidence bound: with small ``k`` a flaky task can
still show a clean floor by luck.  ``pass^k <= pass@1`` always holds; a gap
between the two is the signature of flaky tasks.

Artefact-as-proof
-----------------
The primary artefact is the :class:`ReliabilityReceipt`.  It embeds all
``k`` per-attempt run receipts for every task, so a verifier can replay
every attempt offline and recompute the floor.  A cherry-picked or
fabricated floor fails verification because the replays contradict the
sealed verdicts; stripping attempt receipts (or whole failing tasks) makes
the floor unverifiable rather than silently higher.
"""

from __future__ import annotations

import base64
import hashlib
import hmac as hmac_mod
import json
import time
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol

from bernstein.eval.bench.bundle import TaskResult

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from bernstein.eval.bench.runner import ReplayAdapter
    from bernstein.eval.bench.suite import BenchSuite, BenchTask

# JWS ``typ`` binding install-identity signatures to this artefact so a
# signature minted for another surface cannot be replayed as a reliability
# receipt signature.
_RELIABILITY_JWS_TYP = "bernstein-reliability-receipt+jws"

# ---------------------------------------------------------------------------
# Coordination projection
# ---------------------------------------------------------------------------

# Event fields that carry timing or chain bookkeeping rather than
# coordination.  Mirrors the coordination/timing field split the replay
# journal uses (``_NON_DETERMINISTIC_FIELDS`` in
# ``bernstein.core.replay.journal``); kept as an independent constant so
# the bench substrate stays import-light.
_TIMING_EVENT_FIELDS = frozenset({"ts", "elapsed_s", "index", "prev_hash", "payload_hash", "event_hash"})

# Top-level receipt fields expected to vary per attempt: the run identity,
# and the content-hash heads that commit to the model output embedded in
# the events.
_ATTEMPT_VARIANT_RECEIPT_FIELDS = frozenset({"run_id", "journal_head", "spine_head"})

# Event kinds carrying model output — the only payloads allowed to differ
# across fixed-coordination attempts.
_MODEL_OUTPUT_KIND_PREFIX = "model."

# The declared stochastic payload fields of the bench event vocabulary:
# the only fields of a ``model.*`` event that may vary across
# fixed-coordination attempts.  Every OTHER field of a model event is
# treated as coordination (fail-closed): metadata an adapter records
# alongside the sample — routing, tool selection, scheduler state — must
# be byte-identical across attempts, and divergence there fails admission
# instead of being silently erased from the projection.
_MODEL_SAMPLE_PAYLOAD_FIELDS = frozenset({"sample"})

_MODEL_EVENT_DROPPED_FIELDS = _TIMING_EVENT_FIELDS | _MODEL_SAMPLE_PAYLOAD_FIELDS


def _canonical_bytes(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def validate_run_receipt(receipt: dict[str, Any]) -> str:
    """
    Structural check of a run receipt's event schema.

    Returns an empty string when well-formed, else a description of the
    first problem.  The reliability surfaces refuse to hash arbitrary
    shapes into a coordination identity: the runner raises at emit time,
    and the verifier / ``reliability_check`` report ``MALFORMED_RECEIPT``
    instead of proceeding.
    """
    events = receipt.get("events")
    if not isinstance(events, list):
        return f"receipt 'events' must be a list, got {type(events).__name__}"
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            return f"events[{index}] must be an object, got {type(event).__name__}"
        kind = event.get("kind")
        if not isinstance(kind, str) or not kind:
            return f"events[{index}].kind must be a non-empty string, got {kind!r}"
        seq = event.get("seq")
        if isinstance(seq, bool) or not isinstance(seq, int):
            return f"events[{index}].seq must be an integer, got {seq!r}"
    return ""


def coordination_projection(receipt: dict[str, Any]) -> dict[str, Any]:
    """
    Project a run receipt down to its coordination-only view.

    Dropped: per-attempt run identity, the content-hash heads that commit
    to model output, timing fields on every event, and the *declared
    stochastic payload fields* of model-output events
    (:data:`_MODEL_SAMPLE_PAYLOAD_FIELDS`).  Kept: everything else —
    every event's position and kind, the full content of every
    coordination event, and any metadata an adapter records inside a
    model event beyond the declared sample fields (fail-closed: unknown
    fields default to coordination).

    Two fixed-coordination attempts must have byte-identical projections;
    only the declared model-output payloads may differ.

    Callers that admit untrusted receipts must run
    :func:`validate_run_receipt` first; this function does not coerce or
    validate event field types.
    """
    projected: dict[str, Any] = {
        key: value for key, value in receipt.items() if key not in _ATTEMPT_VARIANT_RECEIPT_FIELDS and key != "events"
    }
    events: list[dict[str, Any]] = []
    for event in receipt.get("events", []):
        kind = event.get("kind")
        dropped = _TIMING_EVENT_FIELDS
        if isinstance(kind, str) and kind.startswith(_MODEL_OUTPUT_KIND_PREFIX):
            # The schedule slot and any coordination metadata stay; only
            # the declared sampled content is allowed to vary.
            dropped = _MODEL_EVENT_DROPPED_FIELDS
        events.append({k: v for k, v in event.items() if k not in dropped})
    projected["events"] = events
    return projected


def coordination_hash(receipt: dict[str, Any]) -> str:
    """SHA-256 of the canonical coordination projection bytes."""
    return hashlib.sha256(_canonical_bytes(coordination_projection(receipt))).hexdigest()


def first_divergent_coordination_field(left: dict[str, Any], right: dict[str, Any]) -> str:
    """
    Name the first path where two receipts' coordination projections differ.

    Returns an empty string when the projections are identical.
    """
    return _first_divergence(coordination_projection(left), coordination_projection(right), "") or ""


def _first_divergence(left: Any, right: Any, path: str) -> str | None:
    if isinstance(left, dict) and isinstance(right, dict):
        for key in sorted(set(left) | set(right)):
            child = f"{path}.{key}" if path else str(key)
            if key not in left or key not in right:
                return f"{child} (present on one side only)"
            found = _first_divergence(left[key], right[key], child)
            if found:
                return found
        return None
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            return f"{path} (length {len(left)} != {len(right)})"
        for index, (a, b) in enumerate(zip(left, right, strict=True)):
            found = _first_divergence(a, b, f"{path}[{index}]")
            if found:
                return found
        return None
    if left != right:
        return f"{path} ({left!r} != {right!r})"
    return None


# ---------------------------------------------------------------------------
# Per-task reliability result
# ---------------------------------------------------------------------------


@dataclass
class TaskReliabilityResult:
    """
    One task's ``k`` fixed-coordination attempts.

    Each attempt is a :class:`TaskResult` carrying its own replayable run
    receipt and emit-time receipt hash — the same artefact-as-proof shape a
    submission bundle uses, repeated ``k`` times.
    """

    task_id: str
    task_hash: str
    attempts: list[TaskResult]
    # Coordination hash shared by every attempt at emit time; empty when
    # the attempts diverged and no single coordination identity exists.
    coordination_hash: str = ""
    coordination_identical: bool = True

    @property
    def attempts_passed(self) -> int:
        return sum(1 for attempt in self.attempts if attempt.passed)

    @property
    def passed_any(self) -> bool:
        """At least one attempt passed — this task's pass@1 contribution."""
        return any(attempt.passed for attempt in self.attempts)

    @property
    def passed_all(self) -> bool:
        """Every attempt passed — this task's pass^k contribution."""
        return bool(self.attempts) and all(attempt.passed for attempt in self.attempts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_hash": self.task_hash,
            "coordination_hash": self.coordination_hash,
            "coordination_identical": self.coordination_identical,
            "attempts": [attempt.to_dict() for attempt in self.attempts],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> TaskReliabilityResult:
        attempts = [
            TaskResult(
                task_id=a["task_id"],
                task_hash=a["task_hash"],
                receipt=a["receipt"],
                passed=a["passed"],
                score=a["score"],
                harness_output=a.get("harness_output", {}),
                # Restore the emit-time hash verbatim — never recompute it
                # from the current receipt bytes at load time.
                stored_receipt_hash=a["receipt_hash"],
            )
            for a in raw["attempts"]
        ]
        return cls(
            task_id=raw["task_id"],
            task_hash=raw["task_hash"],
            attempts=attempts,
            coordination_hash=raw.get("coordination_hash", ""),
            coordination_identical=raw.get("coordination_identical", True),
        )


# ---------------------------------------------------------------------------
# Reliability receipt
# ---------------------------------------------------------------------------


@dataclass
class ReliabilityReceipt:
    """
    The primary artefact of a ``--reliability k`` run.

    Binds ``{suite_hash, k, per-attempt run receipts, pass@1, pass^k,
    scheduler_config}`` and is signed off the install identity.  The sealed
    aggregates are claims; the verifier re-derives both from the embedded
    attempt receipts and rejects the receipt when they differ.
    """

    suite_hash: str
    suite_version: str
    k: int
    scheduler_config: dict[str, Any]
    task_results: list[TaskReliabilityResult]
    # Sealed aggregates, computed at emit time from the attempt verdicts.
    pass_at_1: float
    pass_caret_k: float
    # True iff every task's k attempts shared one coordination identity.
    coordination_ok: bool
    emitted_at: float = field(default_factory=time.time)
    # Filled by the signer (empty until signed).
    signature: str = ""
    signer_fingerprint: str = ""

    _receipt_hash: str | None = field(default=None, init=False, repr=False, compare=False)

    # ------------------------------------------------------------------
    # Content hash (covers everything except the signature fields)
    # ------------------------------------------------------------------

    def receipt_hash(self) -> str:
        if self._receipt_hash is None:
            self._receipt_hash = self._compute_hash()
        return self._receipt_hash

    def _compute_hash(self) -> str:
        payload = _canonical_bytes(
            {
                "suite_hash": self.suite_hash,
                "suite_version": self.suite_version,
                "k": self.k,
                "scheduler_config": self.scheduler_config,
                "emitted_at": self.emitted_at,
                "pass_at_1": self.pass_at_1,
                "pass_caret_k": self.pass_caret_k,
                "coordination_ok": self.coordination_ok,
                "task_results": [tr.to_dict() for tr in self.task_results],
            }
        )
        return hashlib.sha256(payload).hexdigest()

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_hash": self.receipt_hash(),
            "suite_hash": self.suite_hash,
            "suite_version": self.suite_version,
            "k": self.k,
            "scheduler_config": self.scheduler_config,
            "emitted_at": self.emitted_at,
            "pass_at_1": self.pass_at_1,
            "pass_caret_k": self.pass_caret_k,
            "coordination_ok": self.coordination_ok,
            "task_results": [tr.to_dict() for tr in self.task_results],
            "signature": self.signature,
            "signer_fingerprint": self.signer_fingerprint,
        }

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> ReliabilityReceipt:
        raw = json.loads(path.read_text(encoding="utf-8"))
        receipt = cls(
            suite_hash=raw["suite_hash"],
            suite_version=raw["suite_version"],
            k=raw["k"],
            scheduler_config=raw["scheduler_config"],
            task_results=[TaskReliabilityResult.from_dict(tr) for tr in raw["task_results"]],
            pass_at_1=raw["pass_at_1"],
            pass_caret_k=raw["pass_caret_k"],
            coordination_ok=raw["coordination_ok"],
            emitted_at=raw["emitted_at"],
            signature=raw.get("signature", ""),
            signer_fingerprint=raw.get("signer_fingerprint", ""),
        )
        # Load-time integrity guard.  This catches accidental corruption
        # only: an adversary can recompute the outer hash after tampering,
        # which is exactly why the verifier never trusts it and re-derives
        # everything from the embedded attempt receipts.
        if receipt.receipt_hash() != raw["receipt_hash"]:
            raise ValueError(
                f"Reliability receipt hash mismatch: stored {raw['receipt_hash']!r} "
                f"!= recomputed {receipt.receipt_hash()!r}. "
                "The receipt file may have been corrupted."
            )
        return receipt


# ---------------------------------------------------------------------------
# Signing (mirrors signer.py: stub for tests, install identity in production)
# ---------------------------------------------------------------------------


class ReliabilitySignerProtocol(Protocol):
    """Anything that can sign a reliability receipt."""

    def sign(self, receipt: ReliabilityReceipt) -> ReliabilityReceipt:
        """Return a *new* receipt with signature fields populated."""
        ...


class StubReliabilitySigner:
    """
    Deterministic stub: HMAC-SHA256 over the receipt hash with a fixed test
    key.  Never use in production — the key is public and provides no
    security.  Mirrors ``signer.StubSigner``.
    """

    _TEST_KEY = b"bernstein-bench-reliability-stub-signer-v1"

    @classmethod
    def fingerprint(cls) -> str:
        return hashlib.sha256(cls._TEST_KEY).hexdigest()[:16] + "-stub"

    @classmethod
    def expected_signature(cls, receipt: ReliabilityReceipt) -> str:
        raw = hmac_mod.new(cls._TEST_KEY, receipt.receipt_hash().encode(), hashlib.sha256).digest()
        return base64.b64encode(raw).decode()

    def sign(self, receipt: ReliabilityReceipt) -> ReliabilityReceipt:
        return replace(
            receipt,
            signature=self.expected_signature(receipt),
            signer_fingerprint=self.fingerprint(),
        )


class InstallIdentityReliabilitySigner:
    """
    Production signer: detached Ed25519 JWS over the receipt hash, keyed by
    the install identity (``AgentCardKeystore``) and fingerprinted with the
    same keyid the install publishes at ``/.well-known/agent.json/keys``.

    Explicit key material can be injected for hermetic tests; without it the
    install keystore is used (generated by ``bernstein init`` / on first
    signing).  Signing fails loudly when no key material is available — a
    receipt is never silently downgraded to the stub key on the production
    path.
    """

    def __init__(
        self,
        private_key_pem: bytes | None = None,
        public_key_pem: bytes | None = None,
    ) -> None:
        if (private_key_pem is None) != (public_key_pem is None):
            raise ValueError("Provide both private_key_pem and public_key_pem, or neither.")
        self._private_key_pem = private_key_pem
        self._public_key_pem = public_key_pem

    def _key_material(self) -> tuple[bytes, bytes]:
        if self._private_key_pem is not None and self._public_key_pem is not None:
            return self._private_key_pem, self._public_key_pem
        from bernstein.core.identity.http_signing import default_keystore

        return default_keystore().load_or_generate()

    def fingerprint(self) -> str:
        """The install-identity keyid this signer stamps into receipts."""
        from bernstein.core.identity.http_signing import install_identity_keyid

        _, public_pem = self._key_material()
        return install_identity_keyid(public_pem)

    def public_key_pem(self) -> bytes:
        """SPKI PEM of the verifying key (for building a trusted-key map)."""
        _, public_pem = self._key_material()
        return public_pem

    def sign(self, receipt: ReliabilityReceipt) -> ReliabilityReceipt:
        from bernstein.core.identity.http_signing import install_identity_keyid
        from bernstein.core.security.agent_card_signer import (
            sign_detached_jws_over_canonical,
        )

        private_pem, public_pem = self._key_material()
        kid = install_identity_keyid(public_pem)
        signature = sign_detached_jws_over_canonical(
            receipt.receipt_hash().encode(),
            private_pem,
            typ=_RELIABILITY_JWS_TYP,
            kid=kid,
        )
        return replace(receipt, signature=signature, signer_fingerprint=kid)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


@dataclass
class ReliabilityRunner:
    """
    Run every suite task ``k`` times under fixed coordination and seal the
    pass@1 / pass^k aggregates into a :class:`ReliabilityReceipt`.

    The scheduler config passed to the adapter is byte-identical for every
    attempt; the runner records each attempt's coordination hash and flags
    any task whose attempts do not share one coordination identity — a
    floor computed over divergent coordination measures scheduler noise,
    not model sampling, and the verifier rejects it.
    """

    suite: BenchSuite
    adapter: ReplayAdapter
    scheduler_config: dict[str, Any]
    k: int

    def run(self) -> ReliabilityReceipt:
        if self.k < 1:
            raise ValueError(f"k must be >= 1, got {self.k}")

        task_results: list[TaskReliabilityResult] = []
        for task in self.suite.tasks:
            attempts: list[TaskResult] = []
            coordination_hashes: list[str] = []
            for attempt_index in range(self.k):
                receipt = self.adapter.run_task(task, self.scheduler_config)
                schema_problem = validate_run_receipt(receipt)
                if schema_problem:
                    raise ValueError(
                        f"Adapter produced a malformed run receipt for task "
                        f"{task.id!r} attempt {attempt_index}: {schema_problem}"
                    )
                passed, score, harness_output = self.adapter.score_task(task, receipt)
                attempts.append(
                    TaskResult(
                        task_id=task.id,
                        task_hash=task.content_hash(),
                        receipt=receipt,
                        passed=passed,
                        score=score,
                        harness_output=harness_output,
                    )
                )
                coordination_hashes.append(coordination_hash(receipt))
            identical = len(set(coordination_hashes)) == 1
            task_results.append(
                TaskReliabilityResult(
                    task_id=task.id,
                    task_hash=task.content_hash(),
                    attempts=attempts,
                    coordination_hash=coordination_hashes[0] if identical else "",
                    coordination_identical=identical,
                )
            )

        pass_at_1, pass_caret_k = _aggregate(task_results)
        return ReliabilityReceipt(
            suite_hash=self.suite.suite_hash,
            suite_version=self.suite.version,
            k=self.k,
            scheduler_config=self.scheduler_config,
            task_results=task_results,
            pass_at_1=pass_at_1,
            pass_caret_k=pass_caret_k,
            coordination_ok=all(tr.coordination_identical for tr in task_results),
        )


def _aggregate(task_results: list[TaskReliabilityResult]) -> tuple[float, float]:
    """Return ``(pass_at_1, pass_caret_k)`` over *task_results*."""
    if not task_results:
        return 0.0, 0.0
    total = len(task_results)
    any_count = sum(1 for tr in task_results if tr.passed_any)
    all_count = sum(1 for tr in task_results if tr.passed_all)
    return any_count / total, all_count / total


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


class ReliabilityVerificationStatus(Enum):
    """Mirrors ``verifier.VerificationStatus`` plus reliability-specific codes."""

    MATCH = "MATCH"
    DIVERGED = "DIVERGED"
    MISSING_RECEIPT = "MISSING_RECEIPT"
    HASH_MISMATCH = "HASH_MISMATCH"
    FABRICATED_SCORE = "FABRICATED_SCORE"
    # An embedded run receipt violates the event schema (non-string kind,
    # non-integer seq, non-object events); it is never hashed into a
    # coordination identity or replayed.
    MALFORMED_RECEIPT = "MALFORMED_RECEIPT"
    # The sealed pass@1 / pass^k aggregates do not match the replayed verdicts.
    FABRICATED_FLOOR = "FABRICATED_FLOOR"
    # The k attempts of a task do not share one coordination identity, so
    # the floor measures coordination noise rather than model sampling.
    COORDINATION_DIVERGED = "COORDINATION_DIVERGED"
    # Signature absent or invalid.
    UNSIGNED = "UNSIGNED"


@dataclass
class TaskReliabilityVerification:
    task_id: str
    status: ReliabilityVerificationStatus
    detail: str = ""
    # Replayed verdicts, one per attempt (None when replay was impossible).
    replayed_verdicts: list[bool] | None = None


@dataclass
class ReliabilityVerificationResult:
    receipt_hash: str
    suite_hash: str
    k: int
    status: ReliabilityVerificationStatus
    task_results: list[TaskReliabilityVerification] = field(default_factory=list)
    detail: str = ""
    # Aggregates recomputed from the replayed verdicts (None when any task
    # failed verification and the recomputation would be meaningless).
    recomputed_pass_at_1: float | None = None
    recomputed_pass_caret_k: float | None = None

    @property
    def passed(self) -> bool:
        return self.status == ReliabilityVerificationStatus.MATCH

    def report(self) -> str:
        lines = [
            f"receipt_hash : {self.receipt_hash}",
            f"suite_hash   : {self.suite_hash}",
            f"k            : {self.k}",
            f"overall      : {self.status.value}",
        ]
        if self.recomputed_pass_at_1 is not None:
            lines.append(f"pass@1       : {self.recomputed_pass_at_1:.4f} (recomputed from replayed attempts)")
        if self.recomputed_pass_caret_k is not None:
            lines.append(f"pass^{self.k:<8}: {self.recomputed_pass_caret_k:.4f} (recomputed from replayed attempts)")
        if self.detail:
            lines.append(f"detail       : {self.detail}")
        lines.append("")
        for tr in self.task_results:
            mark = "✓" if tr.status == ReliabilityVerificationStatus.MATCH else "✗"
            lines.append(f"  {mark} {tr.task_id:<40} {tr.status.value}")
            if tr.detail:
                lines.append(f"    └─ {tr.detail}")
        return "\n".join(lines)


class ReliabilityVerifier:
    """
    Offline verifier for :class:`ReliabilityReceipt` objects.

    Like ``BenchVerifier``, it uses the :class:`ReplayAdapter` protocol's
    ``score_task`` only — never ``run_task`` — so verification needs no
    access to the emitting machine.  Every claim in the receipt is
    re-derived from the embedded attempt receipts:

    1. Suite hash must match the loaded suite.
    2. The signature must verify cryptographically: a stub signature is
       recomputed and compared; an install-identity signature is verified
       as a detached Ed25519 JWS against the trusted public key the
       fingerprint resolves to (*trusted_keys*, keyed by install-identity
       keyid).  An unresolvable fingerprint or a failed verification is
       ``UNSIGNED`` — never ``MATCH``.
    3. Every suite task must appear exactly once with exactly ``k``
       embedded attempt receipts — stripping a flaky attempt or a failing
       task makes the floor unverifiable instead of silently higher.
    4. Per attempt: emit-time receipt hash must match the live receipt
       bytes; the replayed verdict must match the stored verdict.
    5. The ``k`` attempts of each task must share one coordination
       identity (only model-output payloads may differ).
    6. The sealed pass@1 / pass^k aggregates must equal the values
       recomputed from the replayed verdicts.
    """

    def __init__(
        self,
        suite: BenchSuite,
        adapter: ReplayAdapter,
        trusted_keys: Mapping[str, bytes] | None = None,
    ) -> None:
        self._suite = suite
        self._adapter = adapter
        self._trusted_keys: dict[str, bytes] = dict(trusted_keys or {})
        self._task_index: dict[str, BenchTask] = {t.id: t for t in suite.tasks}

    def verify(self, receipt: ReliabilityReceipt) -> ReliabilityVerificationResult:
        # --- 1. Suite hash -------------------------------------------------
        if receipt.suite_hash != self._suite.suite_hash:
            return self._overall(
                receipt,
                ReliabilityVerificationStatus.HASH_MISMATCH,
                detail=(
                    f"Receipt suite_hash {receipt.suite_hash!r} does not match loaded suite {self._suite.suite_hash!r}."
                ),
            )

        # --- 2. Signature --------------------------------------------------
        signature_problem = self._check_signature(receipt)
        if signature_problem:
            return self._overall(receipt, ReliabilityVerificationStatus.UNSIGNED, detail=signature_problem)

        # --- 3. Shape: k, full task coverage, no duplicates ----------------
        if receipt.k < 1:
            return self._overall(
                receipt,
                ReliabilityVerificationStatus.DIVERGED,
                detail=f"k must be >= 1, receipt claims k={receipt.k}.",
            )
        receipt_ids = [tr.task_id for tr in receipt.task_results]
        if len(receipt_ids) != len(set(receipt_ids)):
            return self._overall(
                receipt,
                ReliabilityVerificationStatus.DIVERGED,
                detail="Duplicate task ids in receipt; each suite task must appear exactly once.",
            )
        missing_tasks = sorted(set(self._task_index) - set(receipt_ids))
        if missing_tasks:
            return self._overall(
                receipt,
                ReliabilityVerificationStatus.MISSING_RECEIPT,
                detail=(
                    f"Suite tasks missing from receipt: {', '.join(missing_tasks)}. "
                    "The floor is computed over the full suite or not at all — "
                    "stripping a failing task would inflate it."
                ),
            )

        # --- 4./5. Per-task verification -----------------------------------
        task_results: list[TaskReliabilityVerification] = []
        all_ok = True
        for tr in receipt.task_results:
            tvr = self._verify_task(tr, receipt.k)
            task_results.append(tvr)
            if tvr.status != ReliabilityVerificationStatus.MATCH:
                all_ok = False

        if not all_ok:
            return ReliabilityVerificationResult(
                receipt_hash=receipt.receipt_hash(),
                suite_hash=receipt.suite_hash,
                k=receipt.k,
                status=ReliabilityVerificationStatus.DIVERGED,
                task_results=task_results,
            )

        # --- 6. Aggregate floor recomputation ------------------------------
        recomputed_at_1, recomputed_caret_k = _aggregate(receipt.task_results)
        if recomputed_at_1 != receipt.pass_at_1 or recomputed_caret_k != receipt.pass_caret_k:
            return ReliabilityVerificationResult(
                receipt_hash=receipt.receipt_hash(),
                suite_hash=receipt.suite_hash,
                k=receipt.k,
                status=ReliabilityVerificationStatus.FABRICATED_FLOOR,
                task_results=task_results,
                recomputed_pass_at_1=recomputed_at_1,
                recomputed_pass_caret_k=recomputed_caret_k,
                detail=(
                    f"Sealed aggregates (pass@1={receipt.pass_at_1}, pass^k={receipt.pass_caret_k}) "
                    f"do not match the replayed attempts "
                    f"(pass@1={recomputed_at_1}, pass^k={recomputed_caret_k}). "
                    "The claimed floor was not produced by the embedded runs."
                ),
            )

        if not receipt.coordination_ok:
            # The receipt honestly recorded divergence; the floor is still
            # not admissible because coordination was not held fixed.
            return ReliabilityVerificationResult(
                receipt_hash=receipt.receipt_hash(),
                suite_hash=receipt.suite_hash,
                k=receipt.k,
                status=ReliabilityVerificationStatus.COORDINATION_DIVERGED,
                task_results=task_results,
                recomputed_pass_at_1=recomputed_at_1,
                recomputed_pass_caret_k=recomputed_caret_k,
                detail="Receipt records coordination divergence; the floor does not measure model sampling.",
            )

        return ReliabilityVerificationResult(
            receipt_hash=receipt.receipt_hash(),
            suite_hash=receipt.suite_hash,
            k=receipt.k,
            status=ReliabilityVerificationStatus.MATCH,
            task_results=task_results,
            recomputed_pass_at_1=recomputed_at_1,
            recomputed_pass_caret_k=recomputed_caret_k,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _overall(
        self,
        receipt: ReliabilityReceipt,
        status: ReliabilityVerificationStatus,
        detail: str,
    ) -> ReliabilityVerificationResult:
        return ReliabilityVerificationResult(
            receipt_hash=receipt.receipt_hash(),
            suite_hash=receipt.suite_hash,
            k=receipt.k,
            status=status,
            detail=detail,
        )

    def _check_signature(self, receipt: ReliabilityReceipt) -> str:
        """Return a problem description, or empty string when the signature verifies."""
        if not receipt.signature or not receipt.signer_fingerprint:
            return "Receipt is unsigned (signature or signer_fingerprint is empty)."
        if receipt.signer_fingerprint == StubReliabilitySigner.fingerprint():
            expected = StubReliabilitySigner.expected_signature(receipt)
            if not hmac_mod.compare_digest(receipt.signature, expected):
                return "Stub signature does not verify against the receipt hash."
            return ""
        # Install-identity path: resolve the fingerprint to a trusted public
        # key and verify the detached Ed25519 JWS over the receipt hash.
        # Fail closed — an unverifiable signature is treated as unsigned.
        public_pem = self._trusted_keys.get(receipt.signer_fingerprint)
        if public_pem is None:
            return (
                f"Signer fingerprint {receipt.signer_fingerprint!r} does not resolve "
                "to a trusted public key; an unverifiable signature is treated as unsigned."
            )
        from bernstein.core.security.agent_card_signer import (
            verify_detached_jws_over_canonical,
        )

        if not verify_detached_jws_over_canonical(
            receipt.receipt_hash().encode(),
            receipt.signature,
            public_pem,
            expected_typ=_RELIABILITY_JWS_TYP,
        ):
            return "Install-identity signature does not verify against the trusted public key."
        return ""

    def _verify_task(self, tr: TaskReliabilityResult, k: int) -> TaskReliabilityVerification:
        # --- Attempt count -------------------------------------------------
        if len(tr.attempts) != k:
            return TaskReliabilityVerification(
                task_id=tr.task_id,
                status=ReliabilityVerificationStatus.MISSING_RECEIPT,
                detail=(
                    f"Receipt claims k={k} but embeds {len(tr.attempts)} attempt(s). "
                    "Stripping attempts makes the floor unverifiable."
                ),
            )

        task = self._task_index.get(tr.task_id)
        if task is None:
            return TaskReliabilityVerification(
                task_id=tr.task_id,
                status=ReliabilityVerificationStatus.MISSING_RECEIPT,
                detail=f"Task {tr.task_id!r} not found in the loaded suite.",
            )
        if task.content_hash() != tr.task_hash:
            return TaskReliabilityVerification(
                task_id=tr.task_id,
                status=ReliabilityVerificationStatus.HASH_MISMATCH,
                detail=(
                    f"task_hash mismatch: receipt says {tr.task_hash!r} but suite computes {task.content_hash()!r}."
                ),
            )

        # --- Per-attempt checks -------------------------------------------
        replayed_verdicts: list[bool] = []
        for index, attempt in enumerate(tr.attempts):
            if not attempt.receipt:
                return TaskReliabilityVerification(
                    task_id=tr.task_id,
                    status=ReliabilityVerificationStatus.MISSING_RECEIPT,
                    detail=f"Attempt {index} has no run receipt; the verdict has no replay substrate.",
                )
            schema_problem = validate_run_receipt(attempt.receipt)
            if schema_problem:
                return TaskReliabilityVerification(
                    task_id=tr.task_id,
                    status=ReliabilityVerificationStatus.MALFORMED_RECEIPT,
                    detail=(
                        f"Attempt {index} run receipt is malformed: {schema_problem}. "
                        "A receipt that violates the event schema is never hashed "
                        "into a coordination identity."
                    ),
                )
            live_hash = hashlib.sha256(_canonical_bytes(attempt.receipt)).hexdigest()
            if attempt.stored_receipt_hash != live_hash:
                return TaskReliabilityVerification(
                    task_id=tr.task_id,
                    status=ReliabilityVerificationStatus.HASH_MISMATCH,
                    detail=(
                        f"Attempt {index} receipt_hash mismatch: stored "
                        f"{attempt.stored_receipt_hash!r} != recomputed {live_hash!r}. "
                        "Receipt bytes were modified after the receipt was emitted."
                    ),
                )
            try:
                replayed_passed, _, _ = self._adapter.score_task(task, attempt.receipt)
            except Exception as exc:
                return TaskReliabilityVerification(
                    task_id=tr.task_id,
                    status=ReliabilityVerificationStatus.DIVERGED,
                    detail=f"Attempt {index} scoring raised during replay: {exc}",
                )
            if replayed_passed != attempt.passed:
                return TaskReliabilityVerification(
                    task_id=tr.task_id,
                    status=ReliabilityVerificationStatus.FABRICATED_SCORE,
                    detail=(
                        f"Attempt {index} verdict mismatch: stored passed={attempt.passed} "
                        f"but replay produced passed={replayed_passed}. "
                        "The per-attempt verdict appears to have been fabricated."
                    ),
                )
            replayed_verdicts.append(replayed_passed)

        # --- Coordination identity across the k attempts -------------------
        recomputed_hashes = [coordination_hash(attempt.receipt) for attempt in tr.attempts]
        if len(set(recomputed_hashes)) != 1:
            divergent_index = next(i for i, h in enumerate(recomputed_hashes) if h != recomputed_hashes[0])
            divergent_field = first_divergent_coordination_field(
                tr.attempts[0].receipt, tr.attempts[divergent_index].receipt
            )
            return TaskReliabilityVerification(
                task_id=tr.task_id,
                status=ReliabilityVerificationStatus.COORDINATION_DIVERGED,
                replayed_verdicts=replayed_verdicts,
                detail=(
                    f"Attempt {divergent_index} coordination diverges from attempt 0 "
                    f"at {divergent_field or '(unknown field)'}; the floor does not "
                    "measure model sampling under divergent coordination."
                ),
            )
        if not tr.coordination_identical or tr.coordination_hash != recomputed_hashes[0]:
            return TaskReliabilityVerification(
                task_id=tr.task_id,
                status=ReliabilityVerificationStatus.COORDINATION_DIVERGED,
                replayed_verdicts=replayed_verdicts,
                detail=(
                    "Recorded coordination identity does not match the recomputed "
                    "coordination hash of the embedded attempts."
                ),
            )

        return TaskReliabilityVerification(
            task_id=tr.task_id,
            status=ReliabilityVerificationStatus.MATCH,
            replayed_verdicts=replayed_verdicts,
        )


# ---------------------------------------------------------------------------
# Determinism control: reliability check
# ---------------------------------------------------------------------------


@dataclass
class ReliabilityCheckResult:
    """Outcome of re-running one attempt and comparing coordination bytes."""

    passed: bool
    task_id: str
    attempt_index: int
    # Full receipt bytes identical on re-run (holds for fully deterministic
    # adapters; a stochastic adapter differs only in model-output payloads).
    byte_identical: bool
    coordination_identical: bool
    divergent_field: str = ""
    detail: str = ""

    def report(self) -> str:
        verdict = "PASS" if self.passed else "FAIL"
        lines = [
            f"reliability-check : {verdict}",
            f"task              : {self.task_id}",
            f"attempt           : {self.attempt_index}",
            f"coordination      : {'byte-identical' if self.coordination_identical else 'DIVERGED'}",
            f"full receipt      : {'byte-identical' if self.byte_identical else 'differs in model-output fields'}",
        ]
        if self.divergent_field:
            lines.append(f"divergent field   : {self.divergent_field}")
        if self.detail:
            lines.append(f"detail            : {self.detail}")
        return "\n".join(lines)


def reliability_check(
    receipt: ReliabilityReceipt,
    suite: BenchSuite,
    adapter: ReplayAdapter,
    task_id: str | None = None,
    attempt_index: int = 0,
) -> ReliabilityCheckResult:
    """
    Re-run one attempt from *receipt* and assert coordination byte-identity.

    This is the control that makes a low floor attributable to model
    sampling: the fresh run's coordination projection must be byte-identical
    to the embedded attempt's.  With a fully deterministic adapter the whole
    receipt is byte-identical; with a stochastic adapter only the
    model-output payloads may differ.  A divergence in any coordination
    field fails the check and names the field.

    Attempt alignment: ``ReplayAdapter.run_task`` carries no attempt index,
    so a stateful adapter replays attempts in call order.  *adapter* must
    therefore be freshly positioned (attempt 0); the check replays attempts
    ``0..attempt_index`` in order and compares the run at position
    ``attempt_index`` against the recorded attempt at the same position, so
    the comparison is well-defined for stateful adapters too.
    """
    if not receipt.task_results:
        return ReliabilityCheckResult(
            passed=False,
            task_id="",
            attempt_index=attempt_index,
            byte_identical=False,
            coordination_identical=False,
            detail="Receipt contains no task results.",
        )

    if task_id is None:
        tr = receipt.task_results[0]
    else:
        matches = [t for t in receipt.task_results if t.task_id == task_id]
        if not matches:
            return ReliabilityCheckResult(
                passed=False,
                task_id=task_id,
                attempt_index=attempt_index,
                byte_identical=False,
                coordination_identical=False,
                detail=f"Task {task_id!r} not found in receipt.",
            )
        tr = matches[0]

    if not 0 <= attempt_index < len(tr.attempts):
        return ReliabilityCheckResult(
            passed=False,
            task_id=tr.task_id,
            attempt_index=attempt_index,
            byte_identical=False,
            coordination_identical=False,
            detail=f"Attempt index {attempt_index} out of range (task has {len(tr.attempts)} attempts).",
        )

    task_index = {t.id: t for t in suite.tasks}
    task = task_index.get(tr.task_id)
    if task is None:
        return ReliabilityCheckResult(
            passed=False,
            task_id=tr.task_id,
            attempt_index=attempt_index,
            byte_identical=False,
            coordination_identical=False,
            detail=f"Task {tr.task_id!r} not found in the loaded suite.",
        )

    recorded = tr.attempts[attempt_index].receipt
    recorded_problem = validate_run_receipt(recorded)
    if recorded_problem:
        return ReliabilityCheckResult(
            passed=False,
            task_id=tr.task_id,
            attempt_index=attempt_index,
            byte_identical=False,
            coordination_identical=False,
            detail=f"Recorded attempt receipt is malformed: {recorded_problem}",
        )

    # Replay the adapter to the requested position: attempts 0..attempt_index
    # in call order, comparing the run at position attempt_index.
    fresh = adapter.run_task(task, receipt.scheduler_config)
    for _ in range(attempt_index):
        fresh = adapter.run_task(task, receipt.scheduler_config)
    fresh_problem = validate_run_receipt(fresh)
    if fresh_problem:
        return ReliabilityCheckResult(
            passed=False,
            task_id=tr.task_id,
            attempt_index=attempt_index,
            byte_identical=False,
            coordination_identical=False,
            detail=f"Fresh run receipt is malformed: {fresh_problem}",
        )

    byte_identical = _canonical_bytes(recorded) == _canonical_bytes(fresh)
    divergent_field = first_divergent_coordination_field(recorded, fresh)
    coordination_identical = divergent_field == ""

    return ReliabilityCheckResult(
        passed=coordination_identical,
        task_id=tr.task_id,
        attempt_index=attempt_index,
        byte_identical=byte_identical,
        coordination_identical=coordination_identical,
        divergent_field=divergent_field,
        detail=(
            ""
            if coordination_identical
            else "Coordination was not held fixed; a pass^k floor from this setup measures scheduler noise."
        ),
    )
