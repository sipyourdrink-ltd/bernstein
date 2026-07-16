"""Deterministic stage promotion and revocation for eval gating (#2520).

A candidate configuration moves ``shadow`` -> ``canary`` -> ``default``. The
stage assignment at any moment is a pure fold over the ordered chain of verdict
receipts: k consecutive non-inferior-or-better verdicts promote shadow to
canary, m consecutive at canary promote to default, and any
significant_regression verdict at canary or default triggers a rollback that
reverts the projection to the prior default. No state file holds the
assignment; the receipt chain IS the assignment, so a verifier holding only the
chain recomputes the full promotion and rollback history, including the stage at
every prefix, offline.

A rollback emits a :class:`Revocation`, which :func:`build_revocation_receipt`
seals into a content-addressed receipt anchored to the lineage spine and
mirrored into the HMAC audit chain -- the same shape as the verdict receipt, so
a regression postmortem links the exact receipts that admitted a change to the
receipt that revoked it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from bernstein.core.lineage.spine import LineageSpine, content_hash_of
from bernstein.eval.significance import PROMOTING_VERDICTS, Verdict

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path

    from bernstein.core.security.audit_chain import AuditChainStore
    from bernstein.eval.gate_receipt import VerdictReceipt

logger = logging.getLogger(__name__)

#: Consecutive non-inferior-or-better verdicts to promote shadow -> canary.
DEFAULT_SHADOW_TO_CANARY_K = 2

#: Consecutive non-inferior-or-better verdicts at canary to promote -> default.
DEFAULT_CANARY_TO_DEFAULT_M = 2

#: Version stamped into every revocation receipt.
REVOCATION_RECEIPT_SCHEMA_VERSION = 1

_REVOCATION_RUN_ID = "eval-gate"
_REVOCATION_ACTOR = "bernstein.eval_gate"
_REVOCATION_SUBPATH = (".sdd", "eval", "gate", "revocations")
_RECEIPT_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class Stage(StrEnum):
    """A candidate configuration's position on the promotion ladder."""

    SHADOW = "shadow"
    CANARY = "canary"
    DEFAULT = "default"
    ROLLED_BACK = "rolled_back"


@dataclass(frozen=True, slots=True)
class VerdictStep:
    """One ordered entry in the receipt chain the projection folds over.

    Attributes:
        receipt_hash: Content hash of the verdict receipt.
        verdict: The receipt's verdict.
        candidate_config_id: The candidate configuration the receipt evaluated.
        baseline_config_id: The incumbent baseline (the prior default).
    """

    receipt_hash: str
    verdict: Verdict
    candidate_config_id: str
    baseline_config_id: str


@dataclass(frozen=True, slots=True)
class Revocation:
    """A rollback implied by a significant_regression at canary or default.

    Attributes:
        step_index: Position in the chain of the triggering verdict.
        trigger_receipt_hash: The significant_regression receipt hash.
        revoked_receipt_hashes: The promoting receipt hashes this rollback
            invalidates (everything that lifted the candidate off shadow).
        reverts_to_stage: The stage the projection reverts to.
        reverts_to_config_id: The configuration serving the reverted stage (the
            prior default).
        candidate_config_id: The configuration being rolled back.
    """

    step_index: int
    trigger_receipt_hash: str
    revoked_receipt_hashes: tuple[str, ...]
    reverts_to_stage: str
    reverts_to_config_id: str
    candidate_config_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_index": self.step_index,
            "trigger_receipt_hash": self.trigger_receipt_hash,
            "revoked_receipt_hashes": list(self.revoked_receipt_hashes),
            "reverts_to_stage": self.reverts_to_stage,
            "reverts_to_config_id": self.reverts_to_config_id,
            "candidate_config_id": self.candidate_config_id,
        }


@dataclass(frozen=True, slots=True)
class PromotionProjection:
    """The stage assignment projected from a receipt chain.

    Attributes:
        final_stage: The candidate's stage after the whole chain.
        default_config_id: The configuration currently serving ``default``
            (the prior default after any rollback).
        stage_at_prefix: The candidate's stage after each prefix of the chain.
        revocations: The rollbacks the projection implies, in chain order.
    """

    final_stage: Stage
    default_config_id: str
    stage_at_prefix: tuple[Stage, ...] = field(default_factory=tuple)
    revocations: tuple[Revocation, ...] = field(default_factory=tuple)


def steps_from_receipts(receipts: Iterable[VerdictReceipt]) -> list[VerdictStep]:
    """Project sealed verdict receipts onto the ordered chain the fold consumes."""
    return [
        VerdictStep(
            receipt_hash=r.receipt_hash,
            verdict=r.verdict,
            candidate_config_id=r.candidate_config_id,
            baseline_config_id=r.baseline_config_id,
        )
        for r in receipts
    ]


def project(
    steps: Sequence[VerdictStep],
    *,
    k: int = DEFAULT_SHADOW_TO_CANARY_K,
    m: int = DEFAULT_CANARY_TO_DEFAULT_M,
) -> PromotionProjection:
    """Fold the ordered verdict chain into a stage assignment.

    Args:
        steps: The ordered verdict-receipt chain.
        k: Consecutive promoting verdicts to lift shadow -> canary.
        m: Consecutive promoting verdicts at canary to lift -> default.

    Returns:
        A :class:`PromotionProjection`. The fold is a pure function of the
        chain: no external state is read.
    """
    if k < 1 or m < 1:
        msg = f"k and m must be >= 1, got k={k}, m={m}"
        raise ValueError(msg)

    stage = Stage.SHADOW
    streak = 0
    default_config_id = ""
    promoting_hashes: list[str] = []
    stage_prefix: list[Stage] = []
    revocations: list[Revocation] = []

    for index, step in enumerate(steps):
        if not default_config_id:
            default_config_id = step.baseline_config_id
        # Coerce defensively: callers deserialising a chain may pass the raw
        # verdict string rather than the enum member.
        verdict = Verdict(step.verdict)

        if verdict in PROMOTING_VERDICTS:
            streak += 1
            promoting_hashes.append(step.receipt_hash)
            if stage is Stage.SHADOW and streak >= k:
                stage = Stage.CANARY
                streak = 0
            elif stage is Stage.CANARY and streak >= m:
                stage = Stage.DEFAULT
                streak = 0
                default_config_id = step.candidate_config_id
        elif verdict is Verdict.SIGNIFICANT_REGRESSION:
            if stage in (Stage.CANARY, Stage.DEFAULT):
                reverts_to = step.baseline_config_id
                revocations.append(
                    Revocation(
                        step_index=index,
                        trigger_receipt_hash=step.receipt_hash,
                        revoked_receipt_hashes=tuple(promoting_hashes),
                        reverts_to_stage=Stage.SHADOW.value,
                        reverts_to_config_id=reverts_to,
                        candidate_config_id=step.candidate_config_id,
                    )
                )
                stage = Stage.ROLLED_BACK
                default_config_id = reverts_to
                streak = 0
                promoting_hashes = []
            else:
                streak = 0
        else:  # insufficient_evidence breaks the consecutive run
            streak = 0
        stage_prefix.append(stage)

    return PromotionProjection(
        final_stage=stage,
        default_config_id=default_config_id,
        stage_at_prefix=tuple(stage_prefix),
        revocations=tuple(revocations),
    )


def replay_history(
    steps: Sequence[VerdictStep],
    *,
    k: int = DEFAULT_SHADOW_TO_CANARY_K,
    m: int = DEFAULT_CANARY_TO_DEFAULT_M,
) -> tuple[Stage, ...]:
    """Return the candidate's stage after each prefix of the chain."""
    return project(steps, k=k, m=m).stage_at_prefix


# ---------------------------------------------------------------------------
# Signed revocation receipt
# ---------------------------------------------------------------------------


def revocation_receipt_path(workdir: Path, receipt_hash: str) -> Path:
    """Return the on-disk revocation-receipt path for *receipt_hash*.

    Validated and containment-checked exactly like the verdict receipt path.

    Raises:
        ValueError: The hash is not canonical or the path escapes the store.
    """
    if not _RECEIPT_HASH_RE.match(receipt_hash):
        msg = f"receipt_hash is not a canonical sha256 digest: {receipt_hash!r}"
        raise ValueError(msg)
    base = workdir.joinpath(*_REVOCATION_SUBPATH)
    candidate = base / f"{receipt_hash}.json"
    base_real = os.path.realpath(base)
    cand_real = os.path.realpath(candidate)
    if os.path.commonpath([base_real, cand_real]) != base_real:
        msg = f"receipt path escapes revocation directory: {receipt_hash!r}"
        raise ValueError(msg)
    return candidate


def _hash_obj(obj: Any) -> str:
    payload = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class RevocationReceipt:
    """A sealed revocation receipt naming the receipts a rollback revokes."""

    schema_version: int
    candidate_config_id: str
    revoked_receipt_hashes: tuple[str, ...]
    reverts_to_stage: str
    reverts_to_config_id: str
    trigger_receipt_hash: str
    timestamp: int
    receipt_hash: str
    journal_entry_hash: str = ""

    def body(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "candidate_config_id": self.candidate_config_id,
            "revoked_receipt_hashes": list(self.revoked_receipt_hashes),
            "reverts_to_stage": self.reverts_to_stage,
            "reverts_to_config_id": self.reverts_to_config_id,
            "trigger_receipt_hash": self.trigger_receipt_hash,
            "timestamp": self.timestamp,
        }

    def canonical_bytes(self) -> bytes:
        payload = self.body()
        payload["receipt_hash"] = self.receipt_hash
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")

    def to_dict(self) -> dict[str, Any]:
        payload = self.body()
        payload["receipt_hash"] = self.receipt_hash
        payload["journal_entry_hash"] = self.journal_entry_hash
        return payload

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> RevocationReceipt:
        return cls(
            schema_version=int(raw["schema_version"]),
            candidate_config_id=str(raw["candidate_config_id"]),
            revoked_receipt_hashes=tuple(str(h) for h in raw["revoked_receipt_hashes"]),
            reverts_to_stage=str(raw["reverts_to_stage"]),
            reverts_to_config_id=str(raw["reverts_to_config_id"]),
            trigger_receipt_hash=str(raw["trigger_receipt_hash"]),
            timestamp=int(raw["timestamp"]),
            receipt_hash=str(raw["receipt_hash"]),
            journal_entry_hash=str(raw.get("journal_entry_hash", "")),
        )


def build_revocation_receipt(
    *,
    revocation: Revocation,
    workdir: Path,
    lineage_root: Path,
    hmac_key: bytes,
    timestamp: int,
    chain: AuditChainStore | None = None,
) -> RevocationReceipt:
    """Seal a :class:`Revocation` into a signed, anchored receipt.

    The receipt is anchored in the ``eval-gate`` lineage spine, written under
    ``.sdd/eval/gate/revocations``, and (when *chain* is supplied) mirrored into
    the HMAC audit chain via
    :func:`~bernstein.core.security.audit_chain.record_eval_gate_revocation`.
    """
    unsealed = RevocationReceipt(
        schema_version=REVOCATION_RECEIPT_SCHEMA_VERSION,
        candidate_config_id=revocation.candidate_config_id,
        revoked_receipt_hashes=revocation.revoked_receipt_hashes,
        reverts_to_stage=revocation.reverts_to_stage,
        reverts_to_config_id=revocation.reverts_to_config_id,
        trigger_receipt_hash=revocation.trigger_receipt_hash,
        timestamp=timestamp,
        receipt_hash="",
    )
    receipt_hash = _hash_obj(unsealed.body())
    sealed_no_anchor = RevocationReceipt(
        schema_version=unsealed.schema_version,
        candidate_config_id=unsealed.candidate_config_id,
        revoked_receipt_hashes=unsealed.revoked_receipt_hashes,
        reverts_to_stage=unsealed.reverts_to_stage,
        reverts_to_config_id=unsealed.reverts_to_config_id,
        trigger_receipt_hash=unsealed.trigger_receipt_hash,
        timestamp=unsealed.timestamp,
        receipt_hash=receipt_hash,
    )

    spine = LineageSpine(lineage_root, run_id=_REVOCATION_RUN_ID, hmac_key=hmac_key)
    artifact_path = "/".join((*_REVOCATION_SUBPATH, f"{receipt_hash}.json"))
    anchor = spine.record(
        artifact_path=artifact_path,
        content=sealed_no_anchor.canonical_bytes(),
        actor=_REVOCATION_ACTOR,
        step_id=receipt_hash,
        model="revocation",
        timestamp=timestamp,
    )

    sealed = RevocationReceipt(
        schema_version=sealed_no_anchor.schema_version,
        candidate_config_id=sealed_no_anchor.candidate_config_id,
        revoked_receipt_hashes=sealed_no_anchor.revoked_receipt_hashes,
        reverts_to_stage=sealed_no_anchor.reverts_to_stage,
        reverts_to_config_id=sealed_no_anchor.reverts_to_config_id,
        trigger_receipt_hash=sealed_no_anchor.trigger_receipt_hash,
        timestamp=sealed_no_anchor.timestamp,
        receipt_hash=receipt_hash,
        journal_entry_hash=anchor,
    )

    path = revocation_receipt_path(workdir, receipt_hash)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(sealed.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )

    if chain is not None:
        from bernstein.core.security.audit_chain import record_eval_gate_revocation

        record_eval_gate_revocation(
            chain=chain,
            receipt_hash=receipt_hash,
            candidate_config_id=revocation.candidate_config_id,
            revoked_receipt_hashes=list(revocation.revoked_receipt_hashes),
            reverts_to_stage=revocation.reverts_to_stage,
            reverts_to_config_id=revocation.reverts_to_config_id,
            trigger_receipt_hash=revocation.trigger_receipt_hash,
            journal_entry_hash=anchor,
        )
    return sealed


def read_revocation_receipt(workdir: Path, receipt_hash: str) -> RevocationReceipt | None:
    """Return the sealed revocation receipt for *receipt_hash* or ``None``."""
    try:
        path = revocation_receipt_path(workdir, receipt_hash)
    except ValueError:
        return None
    if not path.is_file():
        return None
    try:
        return RevocationReceipt.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        logger.warning("eval: malformed revocation receipt at %s", path)
        return None


@dataclass(frozen=True, slots=True)
class RevocationVerifyResult:
    """Outcome of an offline revocation-receipt verification."""

    ok: bool
    reason: str
    receipt: RevocationReceipt | None


def verify_revocation_receipt(
    *,
    workdir: Path,
    lineage_root: Path,
    hmac_key: bytes,
    receipt_hash: str,
) -> RevocationVerifyResult:
    """Re-verify the revocation receipt for *receipt_hash* offline.

    Recomputes the receipt hash from the stored body (catches any mutated
    field), then verifies the lineage spine and the receipt's anchor.
    """
    receipt = read_revocation_receipt(workdir, receipt_hash)
    if receipt is None:
        return RevocationVerifyResult(ok=False, reason=f"no revocation receipt for {receipt_hash!r}", receipt=None)
    if receipt.receipt_hash != receipt_hash:
        return RevocationVerifyResult(ok=False, reason="receipt hash does not match request", receipt=receipt)

    recomputed = _hash_obj(receipt.body())
    if recomputed != receipt.receipt_hash:
        return RevocationVerifyResult(
            ok=False,
            reason="receipt_hash does not recompute from the receipt body (tampered)",
            receipt=receipt,
        )

    spine = LineageSpine(lineage_root, run_id=_REVOCATION_RUN_ID, hmac_key=hmac_key)
    report = spine.verify()
    if not report.ok:
        detail = "; ".join(report.errors) if report.errors else report.status.value
        return RevocationVerifyResult(
            ok=False, reason=f"eval-gate spine failed verification: {detail}", receipt=receipt
        )

    expected_content = content_hash_of(receipt.canonical_bytes())
    anchored = any(
        entry.entry_hash == receipt.journal_entry_hash and entry.content_hash == expected_content
        for entry in spine.iter_entries()
    )
    if not anchored:
        return RevocationVerifyResult(
            ok=False, reason="receipt is not anchored in the eval-gate spine", receipt=receipt
        )
    return RevocationVerifyResult(ok=True, reason="", receipt=receipt)


__all__ = [
    "DEFAULT_CANARY_TO_DEFAULT_M",
    "DEFAULT_SHADOW_TO_CANARY_K",
    "REVOCATION_RECEIPT_SCHEMA_VERSION",
    "PromotionProjection",
    "Revocation",
    "RevocationReceipt",
    "RevocationVerifyResult",
    "Stage",
    "VerdictStep",
    "build_revocation_receipt",
    "project",
    "read_revocation_receipt",
    "replay_history",
    "revocation_receipt_path",
    "steps_from_receipts",
    "verify_revocation_receipt",
]
