"""FindingVerifyReceipt - lineage-attested receipts for finding verify results.

Issue #2557 (analogous pattern). When a finding is re-verified the system
compares a stored hash against a newly computed one. The verdict (MATCH or
DRIFT) and the drift reason are the result; anchoring it to the spine gives
the verification run tamper-evident provenance.

Substrate coupling mirrors :mod:`bernstein.core.planning.recovery_receipt`:

* **Content addressing.** :meth:`FindingVerifyReceipt.content_hash` is a pure
  function of the receipt payload. Two identical finding states produce a
  byte-identical receipt and therefore a byte-identical content hash.
* **Lineage anchoring.** :func:`record_finding_verify_receipt` records the
  canonical bytes on the run's :class:`~bernstein.core.lineage.spine.LineageSpine`,
  which returns a Merkle-chained, HMAC-tagged entry hash.
* **Tamper evidence.** Mutating any receipt field breaks the content-hash
  bind to the spine entry; forging the stored content hash breaks the HMAC
  chain.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bernstein.core.lineage.spine import LineageSpine

#: DRIFT reason taxonomy. Maps each :attr:`FindingVerifyVerdict.DRIFT_*`
#: member to the stable string the janitor's ``file_contains`` signal uses
#: to assert the module declares it (issue #2557). Keep in sync with the
#: enum - adding a new DRIFT verdict requires a matching entry here.
DRIFT_REASON = frozenset(
    {
        "feed_changed",
        "rule_changed",
        "target_changed",
        "nondeterministic",
    }
)

#: Version stamped into every receipt payload. Bump only on a wire-format
#: change so a verifier can reject unknown receipt shapes.
FINDING_VERIFY_RECEIPT_VERSION = 1

#: Repo-relative directory the content-addressed receipt artifacts live under.
#: Kept repo-relative and POSIX so ``LineageSpine._reject_unsafe_artifact_path``
#: accepts it.
FINDING_VERIFY_ARTIFACT_DIR = ".sdd/lineage/finding-verify"


class FindingVerifyVerdict(Enum):
    """Verdict for a finding re-verification.

    Attributes:
        MATCH: The stored and computed hashes are identical.
        DRIFT: The hashes differ; reason specifies which aspect changed.
    """

    MATCH = "match"
    DRIFT = "drift"

    # DRIFT reasons - each is a distinct verdict value indicating *why*
    # the hashes diverged. A flat enum avoids a separate reason field
    # while keeping the DRIFT family exhaustive.
    DRIFT_FEED_CHANGED = "drift_feed_changed"
    DRIFT_RULE_CHANGED = "drift_rule_changed"
    DRIFT_TARGET_CHANGED = "drift_target_changed"
    DRIFT_NON_DETERMINISTIC = "drift_nondeterministic"


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
    """Return canonical JSON bytes (sorted keys, minimal separators, UTF-8)."""
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    ).encode("utf-8")


def _verdict_is_match(verdict: FindingVerifyVerdict) -> bool:
    return verdict is FindingVerifyVerdict.MATCH


def _verdict_is_drift(verdict: FindingVerifyVerdict) -> bool:
    return verdict.name.startswith("DRIFT_")


@dataclass(frozen=True, slots=True)
class FindingVerifyResult:
    """Outcome of a single finding re-verification.

    Attributes:
        verdict: MATCH or a DRIFT variant.
        reason: Human-readable explanation (populated for DRIFT, empty for MATCH).
        finding_id: Stable identifier for the finding.
        task_id: Associated task id.
        stored_hash: Hash recorded at the previous verification.
        computed_hash: Hash computed at this verification.
    """

    verdict: FindingVerifyVerdict
    reason: str
    finding_id: str
    task_id: str
    stored_hash: str
    computed_hash: str


@dataclass(frozen=True, slots=True)
class FindingVerifyReceipt:
    """A content-addressed, lineage-attestable finding verify receipt.

    The receipt is the verification run's primary artifact. Every field is a
    pure function of the finding state, so the canonical serialization is
    byte-identical across two identical states.

    ``spine_entry_hash`` is set after the receipt is anchored; it is excluded
    from :meth:`canonical_payload` (it is derived from that payload, so
    including it would be circular) and from equality/content hashing.

    Attributes:
        finding_id: Stable identifier for the finding.
        task_id: Associated task id.
        stored_hash: Hash recorded at the previous verification.
        computed_hash: Hash computed at this verification.
        verdict: MATCH or a DRIFT variant.
        reason: Human-readable explanation (empty for MATCH).
        spine_entry_hash: Merkle-chained spine entry hash once anchored, else
            ``None``. Not part of the content-addressed payload.
    """

    finding_id: str
    task_id: str
    stored_hash: str
    computed_hash: str
    verdict: FindingVerifyVerdict
    reason: str = ""
    v: int = FINDING_VERIFY_RECEIPT_VERSION
    spine_entry_hash: str | None = field(default=None, compare=False)

    def canonical_payload(self) -> dict[str, Any]:
        """Return the content-addressed payload (excludes the spine hash)."""
        return {
            "v": self.v,
            "finding_id": self.finding_id,
            "task_id": self.task_id,
            "stored_hash": self.stored_hash,
            "computed_hash": self.computed_hash,
            "verdict": self.verdict.value,
            "reason": self.reason,
        }

    def canonical_bytes(self) -> bytes:
        """Return the canonical JSON bytes hashed for content addressing."""
        return _canonical_bytes(self.canonical_payload())

    def content_hash(self) -> str:
        """Return the ``sha256:``-prefixed digest of the canonical bytes."""
        return "sha256:" + hashlib.sha256(self.canonical_bytes()).hexdigest()

    def artifact_path(self) -> str:
        """Return the content-addressed, repo-relative receipt artifact path."""
        digest = self.content_hash().split(":", 1)[1]
        return f"{FINDING_VERIFY_ARTIFACT_DIR}/{digest}.json"

    def with_entry_hash(self, entry_hash: str) -> FindingVerifyReceipt:
        """Return a copy carrying the anchored spine entry hash."""
        return FindingVerifyReceipt(
            finding_id=self.finding_id,
            task_id=self.task_id,
            stored_hash=self.stored_hash,
            computed_hash=self.computed_hash,
            verdict=self.verdict,
            reason=self.reason,
            v=self.v,
            spine_entry_hash=entry_hash,
        )

    @classmethod
    def from_result(cls, result: FindingVerifyResult, **kwargs: Any) -> FindingVerifyReceipt:
        """Build a receipt from a :class:`FindingVerifyResult`.

        Args:
            result: The verification result to embed.
            **kwargs: Forwarded to the constructor (e.g. ``spine_entry_hash``).

        Returns:
            An unanchored :class:`FindingVerifyReceipt`.
        """
        return cls(
            finding_id=result.finding_id,
            task_id=result.task_id,
            stored_hash=result.stored_hash,
            computed_hash=result.computed_hash,
            verdict=result.verdict,
            reason=result.reason,
            **kwargs,
        )


def build_finding_verify_receipt(
    *,
    finding_id: str,
    task_id: str,
    stored_hash: str,
    computed_hash: str,
    verdict: FindingVerifyVerdict,
    reason: str = "",
) -> FindingVerifyReceipt:
    """Assemble a :class:`FindingVerifyReceipt`.

    Args:
        finding_id: Stable identifier for the finding.
        task_id: Associated task id.
        stored_hash: Hash recorded at the previous verification.
        computed_hash: Hash computed at this verification.
        verdict: MATCH or a DRIFT variant.
        reason: Human-readable explanation (empty for MATCH).

    Returns:
        An unanchored :class:`FindingVerifyReceipt`.
    """
    return FindingVerifyReceipt(
        finding_id=finding_id,
        task_id=task_id,
        stored_hash=stored_hash,
        computed_hash=computed_hash,
        verdict=verdict,
        reason=reason,
    )


def record_finding_verify_receipt(
    receipt: FindingVerifyReceipt,
    *,
    spine: LineageSpine,
    actor: str = "finding-verifier",
    model: str = "",
    timestamp: int = 0,
) -> str:
    """Anchor a finding verify receipt on the run's lineage spine.

    The canonical receipt bytes are the recorded content, so the spine entry's
    ``content_hash`` binds the anchored entry to the exact receipt payload.

    Args:
        receipt: The receipt to anchor.
        spine: The run's lineage spine.
        actor: Producing actor recorded on the spine entry.
        model: Optional model string recorded for provenance.
        timestamp: Stable integer timestamp; defaults to ``0`` so identical
            fixtures replay byte-identically.

    Returns:
        The Merkle-chained spine entry hash.
    """
    return spine.record(
        artifact_path=receipt.artifact_path(),
        content=receipt.canonical_bytes(),
        actor=actor,
        step_id=finding_verify_step_id(receipt),
        model=model,
        timestamp=timestamp,
    )


def finding_verify_step_id(receipt: FindingVerifyReceipt) -> str:
    """Return the deterministic spine ``step_id`` for a finding verify receipt.

    Derived from finding identity rather than a per-run uuid, so the spine
    entry hash is a stable function of the finding being verified.
    """
    return f"finding-verify:{receipt.finding_id}"


__all__ = [
    "DRIFT_REASON",
    "FINDING_VERIFY_ARTIFACT_DIR",
    "FINDING_VERIFY_RECEIPT_VERSION",
    "FindingVerifyReceipt",
    "FindingVerifyResult",
    "FindingVerifyVerdict",
    "build_finding_verify_receipt",
    "finding_verify_step_id",
    "record_finding_verify_receipt",
]
