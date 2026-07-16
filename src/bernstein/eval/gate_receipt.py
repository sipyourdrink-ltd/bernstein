"""Signed verdict receipts for statistical eval gating (#2520).

A gate verdict is not a log line; it is a receipt that carries the exact
statistical evidence it was decided on. :func:`build_verdict_receipt` seals a
:class:`~bernstein.eval.significance.SignificanceResult` two ways, following the
dispatch-receipt pattern (:mod:`bernstein.core.cost.scheduling.receipt`):

* the receipt's canonical bytes are appended to the ``eval-gate`` run of the
  Merkle+HMAC lineage spine, and the spine entry hash becomes the receipt's
  ``journal_entry_hash``; and
* the receipt identity is mirrored into the HMAC audit chain via
  :func:`~bernstein.core.security.audit_chain.record_eval_gate_verdict`.

The receipt IS the proof, not a decoration on a log line. Verification
(:func:`verify_verdict_receipt`) re-derives the receipt hash from the stored
body and, crucially, *re-derives the verdict from the embedded evidence*: it
recomputes :func:`~bernstein.eval.significance.classify` over the stored 2x2
table and parameters and rejects any receipt whose stored evidence does not
entail its stored verdict -- even when the receipt's own hashes are internally
consistent. Strip the evidence and the verdict collapses to a threshold compare
over noise.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.lineage.spine import LineageSpine, content_hash_of
from bernstein.eval.significance import (
    DEFAULT_ALPHA,
    DEFAULT_MIN_N,
    DEFAULT_NON_INFERIORITY_MARGIN,
    PairedTable,
    SignificanceResult,
    Verdict,
    classify,
    result_set_hash,
    suite_content_hash,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from bernstein.core.security.audit_chain import AuditChainStore

logger = logging.getLogger(__name__)

#: Version stamped into every verdict receipt. Bump only on a wire-format
#: change.
VERDICT_RECEIPT_SCHEMA_VERSION = 1

#: Lineage run id under which every verdict receipt is anchored, kept separate
#: so gate receipts never interleave with per-task journals.
EVAL_GATE_RUN_ID = "eval-gate"

_GATE_ACTOR = "bernstein.eval_gate"
_GATE_SUBPATH = (".sdd", "eval", "gate")
_RECEIPT_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def verdict_receipt_path(workdir: Path, receipt_hash: str) -> Path:
    """Return the on-disk receipt path for *receipt_hash* under *workdir*.

    The hash is validated against ``sha256:<64 hex>`` and the resolved path is
    asserted to stay under the gate directory, so a caller-influenced hash can
    never escape the receipt store (path-injection defense in depth).

    Raises:
        ValueError: The hash is not a canonical ``sha256:`` digest, or the
            resolved path escapes the gate directory.
    """
    if not _RECEIPT_HASH_RE.match(receipt_hash):
        msg = f"receipt_hash is not a canonical sha256 digest: {receipt_hash!r}"
        raise ValueError(msg)
    base = workdir.joinpath(*_GATE_SUBPATH)
    candidate = base / f"{receipt_hash}.json"
    base_real = os.path.realpath(base)
    cand_real = os.path.realpath(candidate)
    if os.path.commonpath([base_real, cand_real]) != base_real:
        msg = f"receipt path escapes gate directory: {receipt_hash!r}"
        raise ValueError(msg)
    return candidate


def _hash_obj(obj: Any) -> str:
    payload = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class VerdictReceipt:
    """A sealed statistical verdict receipt.

    The body (everything the ``receipt_hash`` covers) binds the suite and both
    result-set hashes, the candidate and baseline configuration ids, the full
    :class:`SignificanceResult` evidence, the schema version, and the
    timestamp. The ``journal_entry_hash`` is assigned post-seal and is not part
    of the hashed body.
    """

    schema_version: int
    suite_content_hash: str
    baseline_result_set_hash: str
    candidate_result_set_hash: str
    candidate_config_id: str
    baseline_config_id: str
    evidence: SignificanceResult
    timestamp: int
    receipt_hash: str
    journal_entry_hash: str = ""

    @property
    def verdict(self) -> Verdict:
        return self.evidence.verdict

    def body(self) -> dict[str, Any]:
        """The hashed body: every field except the receipt hash and anchor."""
        return {
            "schema_version": self.schema_version,
            "suite_content_hash": self.suite_content_hash,
            "baseline_result_set_hash": self.baseline_result_set_hash,
            "candidate_result_set_hash": self.candidate_result_set_hash,
            "candidate_config_id": self.candidate_config_id,
            "baseline_config_id": self.baseline_config_id,
            "evidence": self.evidence.to_dict(),
            "timestamp": self.timestamp,
        }

    def canonical_payload_without_anchor(self) -> str:
        """Canonical JSON of the body plus receipt hash (excludes the anchor).

        Two machines seal byte-identical bytes here; the lineage anchor is the
        only field that could differ (it never does for identical inputs), so it
        is excluded from the cross-machine equality contract.
        """
        payload = self.body()
        payload["receipt_hash"] = self.receipt_hash
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    def to_dict(self) -> dict[str, Any]:
        payload = self.body()
        payload["receipt_hash"] = self.receipt_hash
        payload["journal_entry_hash"] = self.journal_entry_hash
        return payload

    def canonical_bytes(self) -> bytes:
        """Canonical bytes sealed into the lineage spine (body + hash)."""
        return self.canonical_payload_without_anchor().encode("utf-8")

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> VerdictReceipt:
        return cls(
            schema_version=int(raw["schema_version"]),
            suite_content_hash=str(raw["suite_content_hash"]),
            baseline_result_set_hash=str(raw["baseline_result_set_hash"]),
            candidate_result_set_hash=str(raw["candidate_result_set_hash"]),
            candidate_config_id=str(raw["candidate_config_id"]),
            baseline_config_id=str(raw["baseline_config_id"]),
            evidence=_significance_from_dict(raw["evidence"]),
            timestamp=int(raw["timestamp"]),
            receipt_hash=str(raw["receipt_hash"]),
            journal_entry_hash=str(raw.get("journal_entry_hash", "")),
        )


def _significance_from_dict(raw: Mapping[str, Any]) -> SignificanceResult:
    return SignificanceResult(
        verdict=Verdict(str(raw["verdict"])),
        reason=str(raw["reason"]),
        test=str(raw["test"]),
        alpha=float(raw["alpha"]),
        n_baseline=int(raw["n_baseline"]),
        n_candidate=int(raw["n_candidate"]),
        base_pass=int(raw["base_pass"]),
        cand_pass=int(raw["cand_pass"]),
        base_rate=float(raw["base_rate"]),
        cand_rate=float(raw["cand_rate"]),
        effect=float(raw["effect"]),
        interval_low=float(raw["interval_low"]),
        interval_high=float(raw["interval_high"]),
        p_improvement=float(raw["p_improvement"]),
        p_regression=float(raw["p_regression"]),
        min_n=int(raw["min_n"]),
        min_n_satisfied=bool(raw["min_n_satisfied"]),
        non_inferiority_margin=float(raw["non_inferiority_margin"]),
        table=PairedTable.from_dict(raw["table"]),
    )


def recompute_receipt_hash(payload: Mapping[str, Any]) -> str:
    """Recompute the ``receipt_hash`` from a receipt dict's body fields.

    Exposed so a verifier (or a test) can confirm that a receipt whose hashes
    are internally consistent is still rejected when its evidence does not
    entail its verdict.
    """
    body = {
        "schema_version": int(payload["schema_version"]),
        "suite_content_hash": str(payload["suite_content_hash"]),
        "baseline_result_set_hash": str(payload["baseline_result_set_hash"]),
        "candidate_result_set_hash": str(payload["candidate_result_set_hash"]),
        "candidate_config_id": str(payload["candidate_config_id"]),
        "baseline_config_id": str(payload["baseline_config_id"]),
        "evidence": payload["evidence"],
        "timestamp": int(payload["timestamp"]),
    }
    return _hash_obj(body)


def build_verdict_receipt(
    *,
    baseline_outcomes: Mapping[str, bool],
    candidate_outcomes: Mapping[str, bool],
    candidate_config_id: str,
    baseline_config_id: str,
    workdir: Path,
    lineage_root: Path,
    hmac_key: bytes,
    timestamp: int,
    alpha: float = DEFAULT_ALPHA,
    non_inferiority_margin: float = DEFAULT_NON_INFERIORITY_MARGIN,
    min_n: int = DEFAULT_MIN_N,
    chain: AuditChainStore | None = None,
) -> VerdictReceipt:
    """Classify a paired suite and seal the verdict into a receipt.

    The paired 2x2 table is built from the per-task outcomes (order-invariant),
    the verdict is derived by :func:`classify`, the receipt is anchored in the
    ``eval-gate`` lineage spine, written under ``.sdd/eval/gate``, and (when a
    *chain* is supplied) mirrored into the HMAC audit chain.

    Args:
        baseline_outcomes: Per-task pass/fail under the baseline arm.
        candidate_outcomes: Per-task pass/fail under the candidate arm; must
            cover the identical task set.
        candidate_config_id: Identifier of the candidate configuration.
        baseline_config_id: Identifier of the incumbent baseline configuration.
        workdir: Project root (receipt written under ``.sdd/eval/gate``).
        lineage_root: ``.sdd/lineage`` root for the spine.
        hmac_key: Audit-chain HMAC key for the spine seal.
        timestamp: Integer timestamp anchored into the spine entry (stable, so
            identical inputs seal byte-identically).
        alpha: Significance level.
        non_inferiority_margin: Non-inferiority margin on the paired difference.
        min_n: Minimum n per arm before a promoting verdict is allowed.
        chain: Optional :class:`AuditChainStore` accepting the mirror.

    Returns:
        The sealed :class:`VerdictReceipt`.
    """
    table = PairedTable.from_outcomes(baseline_outcomes, candidate_outcomes)
    result = classify(table, alpha=alpha, non_inferiority_margin=non_inferiority_margin, min_n=min_n)

    unsealed = VerdictReceipt(
        schema_version=VERDICT_RECEIPT_SCHEMA_VERSION,
        suite_content_hash=suite_content_hash(list(baseline_outcomes)),
        baseline_result_set_hash=result_set_hash(baseline_outcomes),
        candidate_result_set_hash=result_set_hash(candidate_outcomes),
        candidate_config_id=candidate_config_id,
        baseline_config_id=baseline_config_id,
        evidence=result,
        timestamp=timestamp,
        receipt_hash="",
    )
    receipt_hash = _hash_obj(unsealed.body())
    sealed_no_anchor = VerdictReceipt(
        schema_version=unsealed.schema_version,
        suite_content_hash=unsealed.suite_content_hash,
        baseline_result_set_hash=unsealed.baseline_result_set_hash,
        candidate_result_set_hash=unsealed.candidate_result_set_hash,
        candidate_config_id=unsealed.candidate_config_id,
        baseline_config_id=unsealed.baseline_config_id,
        evidence=unsealed.evidence,
        timestamp=unsealed.timestamp,
        receipt_hash=receipt_hash,
    )

    spine = LineageSpine(lineage_root, run_id=EVAL_GATE_RUN_ID, hmac_key=hmac_key)
    artifact_path = "/".join((*_GATE_SUBPATH, f"{receipt_hash}.json"))
    anchor = spine.record(
        artifact_path=artifact_path,
        content=sealed_no_anchor.canonical_bytes(),
        actor=_GATE_ACTOR,
        step_id=receipt_hash,
        model=result.test,
        timestamp=timestamp,
    )

    sealed = VerdictReceipt(
        schema_version=sealed_no_anchor.schema_version,
        suite_content_hash=sealed_no_anchor.suite_content_hash,
        baseline_result_set_hash=sealed_no_anchor.baseline_result_set_hash,
        candidate_result_set_hash=sealed_no_anchor.candidate_result_set_hash,
        candidate_config_id=sealed_no_anchor.candidate_config_id,
        baseline_config_id=sealed_no_anchor.baseline_config_id,
        evidence=sealed_no_anchor.evidence,
        timestamp=sealed_no_anchor.timestamp,
        receipt_hash=receipt_hash,
        journal_entry_hash=anchor,
    )

    path = verdict_receipt_path(workdir, receipt_hash)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(sealed.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )

    if chain is not None:
        from bernstein.core.security.audit_chain import record_eval_gate_verdict

        record_eval_gate_verdict(
            chain=chain,
            receipt_hash=receipt_hash,
            verdict=result.verdict.value,
            suite_content_hash=sealed.suite_content_hash,
            baseline_result_set_hash=sealed.baseline_result_set_hash,
            candidate_result_set_hash=sealed.candidate_result_set_hash,
            candidate_config_id=candidate_config_id,
            n_per_arm=result.n_candidate,
            effect=result.effect,
            interval_low=result.interval_low,
            interval_high=result.interval_high,
            alpha=result.alpha,
            min_n_satisfied=result.min_n_satisfied,
            journal_entry_hash=anchor,
        )
    return sealed


def read_verdict_receipt(workdir: Path, receipt_hash: str) -> VerdictReceipt | None:
    """Return the sealed receipt for *receipt_hash* or ``None`` if absent/bad."""
    try:
        path = verdict_receipt_path(workdir, receipt_hash)
    except ValueError:
        return None
    if not path.is_file():
        return None
    try:
        return VerdictReceipt.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        logger.warning("eval: malformed verdict receipt at %s", path)
        return None


@dataclass(frozen=True, slots=True)
class VerdictVerifyResult:
    """Outcome of an offline verdict-receipt verification."""

    ok: bool
    reason: str
    receipt: VerdictReceipt | None


def verify_verdict_receipt(
    *,
    workdir: Path,
    lineage_root: Path,
    hmac_key: bytes,
    receipt_hash: str,
) -> VerdictVerifyResult:
    """Re-verify the receipt for *receipt_hash* offline.

    Checks, from the stored receipt alone:

    * the receipt hash recomputes from the stored body (catches any mutated
      field when the hash was not recomputed);
    * the stored evidence *entails* the stored verdict: :func:`classify` is
      re-run over the stored 2x2 table and parameters and every derived field
      (verdict, effect, interval, p-values, rates, n) must match byte-for-byte,
      so a forged verdict is rejected even when the receipt hashes are
      internally consistent; and
    * the lineage spine verifies and contains an entry whose content hash
      matches the receipt's canonical bytes and whose entry hash matches the
      receipt's ``journal_entry_hash``.
    """
    receipt = read_verdict_receipt(workdir, receipt_hash)
    if receipt is None:
        return VerdictVerifyResult(ok=False, reason=f"no verdict receipt for {receipt_hash!r}", receipt=None)
    if receipt.receipt_hash != receipt_hash:
        return VerdictVerifyResult(ok=False, reason="receipt hash does not match request", receipt=receipt)

    recomputed = _hash_obj(receipt.body())
    if recomputed != receipt.receipt_hash:
        return VerdictVerifyResult(
            ok=False,
            reason="receipt_hash does not recompute from the receipt body (tampered)",
            receipt=receipt,
        )

    # The verdict is re-derived, not trusted: recompute the entire evidence
    # projection from the stored table and parameters.
    rederived = classify(
        receipt.evidence.table,
        alpha=receipt.evidence.alpha,
        non_inferiority_margin=receipt.evidence.non_inferiority_margin,
        min_n=receipt.evidence.min_n,
    )
    if rederived.to_dict() != receipt.evidence.to_dict():
        return VerdictVerifyResult(
            ok=False,
            reason="stored evidence does not entail its verdict (re-derivation mismatch)",
            receipt=receipt,
        )

    spine = LineageSpine(lineage_root, run_id=EVAL_GATE_RUN_ID, hmac_key=hmac_key)
    report = spine.verify()
    if not report.ok:
        detail = "; ".join(report.errors) if report.errors else report.status.value
        return VerdictVerifyResult(ok=False, reason=f"eval-gate spine failed verification: {detail}", receipt=receipt)

    expected_content = content_hash_of(receipt.canonical_bytes())
    anchored = any(
        entry.entry_hash == receipt.journal_entry_hash and entry.content_hash == expected_content
        for entry in spine.iter_entries()
    )
    if not anchored:
        return VerdictVerifyResult(ok=False, reason="receipt is not anchored in the eval-gate spine", receipt=receipt)
    return VerdictVerifyResult(ok=True, reason="", receipt=receipt)


__all__ = [
    "EVAL_GATE_RUN_ID",
    "VERDICT_RECEIPT_SCHEMA_VERSION",
    "VerdictReceipt",
    "VerdictVerifyResult",
    "build_verdict_receipt",
    "read_verdict_receipt",
    "recompute_receipt_hash",
    "verdict_receipt_path",
    "verify_verdict_receipt",
]
