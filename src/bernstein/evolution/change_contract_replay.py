"""Replay service for ReplayContract — verifiable offline replay of governance decisions.

Issue #5406 sibling to the types task.  This module is hermetic: no network,
no subprocess, no LLM.  All data is read from disk; all results are returned
as plain dataclasses.  It is a pure service: nothing here mutates state on
disk except :func:`write_verdict_receipt`, which writes the receipt file it
was asked for.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from pathlib import Path  # noqa: TC003
from typing import TYPE_CHECKING, Any

from bernstein.core.replay.journal import read_sealed_journal_head
from bernstein.core.security.governance import (
    GovernanceDecision,
    read_decisions,
)
from bernstein.evolution.types import (
    ContractInvariant,
    PredictedDecisionChange,
    ReplayContract,
    ReplayServiceResult,
    ReplayVerdict,
    RunVerdict,
)

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class ThinCorpusError(ValueError):
    """Fewer than ``min_corpus_size`` sealed runs match the fingerprint."""

    def __init__(self, found: int, required: int, fingerprint: str) -> None:
        self.found = found
        self.required = required
        self.fingerprint = fingerprint
        super().__init__(
            f"thin corpus: found {found} sealed run(s) matching fingerprint {fingerprint!r}, required {required}"
        )


class ReceiptMismatch(ValueError):
    """A verdict receipt's claim disagrees with a fresh recompute."""

    def __init__(self, receipt_path: Path, detail: str) -> None:
        self.receipt_path = receipt_path
        self.detail = detail
        super().__init__(f"receipt mismatch at {receipt_path}: {detail}")


# --------------------------------------------------------------------------
# Invariant predicate registry
# --------------------------------------------------------------------------

#: Module-level registry mapping a contract invariant's ``predicate_hash`` to
#: the callable that evaluates it over a list of governance decisions.  The
#: hash is the stable identity baked into the contract at creation time; the
#: registry makes the predicate reproducible from the contract at replay time.
PREDICATE_REGISTRY: dict[str, Callable[[list[GovernanceDecision]], bool]] = {}


def register_invariant(
    predicate_hash: str,
    predicate: Callable[[list[GovernanceDecision]], bool],
) -> None:
    """Register the implementation for a contract-invariant predicate.

    Args:
        predicate_hash: The ``ContractInvariant.predicate_hash`` this callable
            implements.
        predicate: Returns ``True`` when the invariant holds over the replayed
            decisions.
    """
    PREDICATE_REGISTRY[predicate_hash] = predicate


# --------------------------------------------------------------------------
# Contract canonical bytes (the receipt's self-contained contract copy)
# --------------------------------------------------------------------------


def _contract_to_dict(contract: ReplayContract) -> dict[str, Any]:
    """Project a contract onto its canonical JSON-able dict."""
    return {
        "target_fingerprint": contract.target_fingerprint,
        "predicted_changes": [
            {
                "subject": pc.subject,
                "action": pc.action,
                "expected_verdict": pc.expected_verdict,
            }
            for pc in contract.predicted_changes
        ],
        "invariants": [{"name": inv.name, "predicate_hash": inv.predicate_hash} for inv in contract.invariants],
        "min_corpus_size": contract.min_corpus_size,
    }


def contract_canonical_bytes(contract: ReplayContract) -> bytes:
    """Return the contract's canonical bytes (sorted-key minimal JSON, UTF-8).

    These bytes are what :func:`contract_fingerprint` hashes and what a
    verdict receipt carries as ``contract_canonical`` so a verifier holding
    only the receipt can reconstruct the contract offline.
    """
    return json.dumps(_contract_to_dict(contract), sort_keys=True, separators=(",", ":")).encode("utf-8")


def contract_fingerprint(contract: ReplayContract) -> str:
    """Return the SHA-256 hex digest of the contract's canonical bytes."""
    return hashlib.sha256(contract_canonical_bytes(contract)).hexdigest()


def _contract_from_canonical(raw: dict[str, Any]) -> ReplayContract:
    """Rebuild a :class:`ReplayContract` from its canonical dict projection."""
    return ReplayContract(
        target_fingerprint=str(raw["target_fingerprint"]),
        predicted_changes=tuple(
            PredictedDecisionChange(
                subject=str(pc["subject"]),
                action=str(pc["action"]),
                expected_verdict=str(pc["expected_verdict"]),
            )
            for pc in raw.get("predicted_changes", [])
        ),
        invariants=tuple(
            ContractInvariant(
                name=str(inv["name"]),
                predicate_hash=str(inv["predicate_hash"]),
            )
            for inv in raw.get("invariants", [])
        ),
        min_corpus_size=int(raw.get("min_corpus_size", 5)),
    )


# --------------------------------------------------------------------------
# Corpus selection
# --------------------------------------------------------------------------


def select_corpus(
    *,
    sdd_dir: Path,
    target_fingerprint: str,
    n: int,
) -> list[str]:
    """Select the last N sealed runs whose journal head matches the fingerprint.

    **Matching rule:** a sealed run is a match when the first 8 hex characters
    of its sealed journal head hash equal the first 8 hex characters of
    ``target_fingerprint`` (both compared lowercased, any ``sha256:`` prefix
    stripped).  Runs with no seal — :func:`read_sealed_journal_head` returns
    ``None`` — are skipped silently; they are not part of the corpus yet.

    The corpus is ``.sdd/lineage/`` (one spine per run).  Returned order is
    sorted by the sealed journal head hash ascending (oldest first), so the
    sequence is fully deterministic and independent of filesystem enumeration
    order.

    Args:
        sdd_dir: The ``.sdd`` directory holding ``lineage/``.
        target_fingerprint: Fingerprint prefix to match against sealed heads.
        n: Required number of matching sealed runs.

    Returns:
        Matching run ids, ascending by sealed journal head hash.

    Raises:
        ThinCorpusError: Fewer than *n* sealed runs match the fingerprint.
    """
    lineage_root = sdd_dir / "lineage"
    if not lineage_root.is_dir():
        raise ThinCorpusError(found=0, required=n, fingerprint=target_fingerprint)

    prefix = target_fingerprint.replace("sha256:", "").lower()[:8]

    matches: list[tuple[str, str]] = []  # (run_id, sealed head hash)
    for entry in lineage_root.iterdir():
        if not entry.is_dir():
            continue
        run_id = entry.name
        sealed_head = read_sealed_journal_head(run_id=run_id, sdd_dir=sdd_dir)
        if sealed_head is None:
            continue
        head_hash = sealed_head.replace("sha256:", "").lower()
        if head_hash.startswith(prefix):
            matches.append((run_id, head_hash))

    matches.sort(key=lambda pair: pair[1])

    if len(matches) < n:
        raise ThinCorpusError(found=len(matches), required=n, fingerprint=target_fingerprint)

    return [run_id for run_id, _ in matches[:n]]


# --------------------------------------------------------------------------
# Per-run replay
# --------------------------------------------------------------------------


def _replay_one_run(
    *,
    run_id: str,
    lineage_root: Path,
    contract: ReplayContract,
) -> RunVerdict:
    """Replay the contract against one run's recorded governance decisions."""
    decisions = read_decisions(lineage_root=lineage_root, run_id=run_id)

    decisions_index: dict[tuple[str, str], GovernanceDecision] = {}
    for decision in decisions:
        decisions_index[(decision.subject, decision.action)] = decision

    # 1. Predicted changes: each must be present with the expected verdict,
    #    otherwise the contract predicted a change that did not happen.
    predicted_mismatch: list[str] = []
    changed_subjects: list[str] = []
    predicted_keys = {(pc.subject, pc.action) for pc in contract.predicted_changes}
    for predicted in contract.predicted_changes:
        actual = decisions_index.get((predicted.subject, predicted.action))
        if actual is None:
            predicted_mismatch.append(f"predicted ({predicted.subject}, {predicted.action}) missing")
            continue
        if actual.verdict != predicted.expected_verdict:
            changed_subjects.append(predicted.subject)
            predicted_mismatch.append(
                f"({predicted.subject}, {predicted.action}): expected "
                f"{predicted.expected_verdict!r}, got {actual.verdict!r}"
            )

    # 2. Unexpected changes: a recorded decision outside the predicted set.
    unexpected_keys = sorted(set(decisions_index) - predicted_keys)

    # 3. Invariants against the replayed decisions.
    violated_invariants: list[str] = []
    inconclusive: list[str] = []
    for invariant in contract.invariants:
        predicate = PREDICATE_REGISTRY.get(invariant.predicate_hash)
        if predicate is None:
            inconclusive.append(f"{invariant.name}: predicate {invariant.predicate_hash} not registered")
            continue
        try:
            holds = predicate(decisions)
        except Exception as exc:
            inconclusive.append(f"{invariant.name}: predicate raised {exc}")
            continue
        if not holds:
            violated_invariants.append(invariant.name)

    # 4. Per-run verdict.
    if violated_invariants:
        verdict = ReplayVerdict.INVARIANT_VIOLATED
    elif unexpected_keys or predicted_mismatch:
        verdict = ReplayVerdict.CHANGED_UNEXPECTEDLY
    elif contract.predicted_changes:
        verdict = ReplayVerdict.CHANGED_AS_PREDICTED
    else:
        verdict = ReplayVerdict.UNCHANGED

    details = "; ".join(
        predicted_mismatch
        + [f"unexpected decision ({s}, {a})" for s, a in unexpected_keys]
        + [f"inconclusive: {msg}" for msg in inconclusive]
    )

    return RunVerdict(
        run_id=run_id,
        verdict=verdict,
        changed_subjects=changed_subjects,
        violated_invariants=violated_invariants,
        details=details or "all predictions matched",
    )


# --------------------------------------------------------------------------
# replay_contract — top-level service entry point
# --------------------------------------------------------------------------


def replay_contract(
    *,
    sdd_dir: Path,
    contract: ReplayContract,
) -> ReplayServiceResult:
    """Replay a contract against the recorded corpus. Pure; no state mutated.

    Selects the corpus with ``n=contract.min_corpus_size``, replays each
    selected run's governance decisions against ``contract.predicted_changes``
    and ``contract.invariants``, and aggregates per-run verdicts: any
    INVARIANT_VIOLATED run marks the result, else any CHANGED_UNEXPECTEDLY
    run, else all CHANGED_AS_PREDICTED -> CHANGED_AS_PREDICTED, else
    UNCHANGED.

    Args:
        sdd_dir: The ``.sdd`` directory.
        contract: The change contract to replay.

    Returns:
        A :class:`ReplayServiceResult`.

    Raises:
        ThinCorpusError: Fewer than ``contract.min_corpus_size`` sealed runs
            match the contract's target fingerprint.
    """
    lineage_root = sdd_dir / "lineage"

    selected_run_ids = select_corpus(
        sdd_dir=sdd_dir,
        target_fingerprint=contract.target_fingerprint,
        n=contract.min_corpus_size,
    )

    run_verdicts = [
        _replay_one_run(run_id=run_id, lineage_root=lineage_root, contract=contract) for run_id in selected_run_ids
    ]

    if any(rv.verdict == ReplayVerdict.INVARIANT_VIOLATED for rv in run_verdicts):
        aggregate = ReplayVerdict.INVARIANT_VIOLATED
    elif any(rv.verdict == ReplayVerdict.CHANGED_UNEXPECTEDLY for rv in run_verdicts):
        aggregate = ReplayVerdict.CHANGED_UNEXPECTEDLY
    elif run_verdicts and all(rv.verdict == ReplayVerdict.CHANGED_AS_PREDICTED for rv in run_verdicts):
        aggregate = ReplayVerdict.CHANGED_AS_PREDICTED
    else:
        aggregate = ReplayVerdict.UNCHANGED

    return ReplayServiceResult(
        verdict=aggregate,
        contract_fingerprint=contract_fingerprint(contract),
        selected_run_ids=selected_run_ids,
        run_verdicts=run_verdicts,
        thin_corpus=False,
    )


# --------------------------------------------------------------------------
# Receipt writing and offline verification
# --------------------------------------------------------------------------


def _receipt_body(result: ReplayServiceResult, contract: ReplayContract) -> dict[str, Any]:
    """Return the receipt body (everything before ``service_receipt_hash``)."""
    return {
        "contract_fingerprint": result.contract_fingerprint,
        "selected_run_ids": result.selected_run_ids,
        "run_verdicts": [
            {
                "run_id": rv.run_id,
                "verdict": rv.verdict.value,
                "changed_subjects": rv.changed_subjects,
                "violated_invariants": rv.violated_invariants,
                "details": rv.details,
            }
            for rv in result.run_verdicts
        ],
        "contract_canonical": contract_canonical_bytes(contract).hex(),
    }


def _receipt_hash(body: dict[str, Any]) -> str:
    """Return the sha256 hex over the receipt body's canonical JSON."""
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def write_verdict_receipt(
    *,
    result: ReplayServiceResult,
    contract: ReplayContract,
    out_path: Path,
) -> Path:
    """Write a self-contained verdict receipt for the replay to ``out_path``.

    Receipt shape (JSON): ``{"contract_fingerprint": <hash of the contract's
    canonical bytes>, "selected_run_ids": [...], "run_verdicts": [...],
    "contract_canonical": <canonical bytes of the contract as hex>,
    "service_receipt_hash": <sha256 hex>}``.

    The receipt is self-contained: it carries the contract's canonical bytes,
    so a verifier holding only the receipt file plus the recorded corpus can
    verify the verdict offline via :func:`verify_verdict_receipt`.

    Args:
        result: The :func:`replay_contract` result being receipted.
        contract: The contract that was replayed.
        out_path: Destination path for the receipt file.

    Returns:
        ``out_path``.
    """
    body = _receipt_body(result, contract)
    receipt = body | {"service_receipt_hash": _receipt_hash(body)}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(receipt, indent=2, ensure_ascii=False), encoding="utf-8")
    return out_path


def verify_verdict_receipt(
    *,
    receipt_path: Path,
    lineage_root: Path,
) -> bool:
    """Offline verification of a verdict receipt. No network, no subprocess.

    Re-reads the contract from the receipt's ``contract_canonical`` bytes,
    recomputes the contract fingerprint, re-runs :func:`replay_contract`
    against the same corpus (``lineage_root`` is ``<sdd>/lineage``, so
    ``sdd_dir = lineage_root.parent``), and compares the fresh result to the
    receipt's ``selected_run_ids`` and ``run_verdicts``.  Finally recomputes
    ``service_receipt_hash`` over the receipt body and compares it to the
    stored hash.  Any disagreement raises :class:`ReceiptMismatch`.

    Args:
        receipt_path: Path to the receipt JSON file.
        lineage_root: The ``.sdd/lineage`` directory holding the run spines
            and recorded decisions.

    Returns:
        ``True`` when every recomputation matches the receipt.

    Raises:
        ReceiptMismatch: The receipt's claim disagrees with the recompute.
        ThinCorpusError: The corpus can no longer satisfy the contract's
            ``min_corpus_size`` (propagated from :func:`replay_contract`).
        OSError: The receipt file is unreadable or missing.
    """
    try:
        stored: dict[str, Any] = json.loads(receipt_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ReceiptMismatch(receipt_path, f"invalid JSON: {exc}") from exc

    stored_hash = str(stored.get("service_receipt_hash", ""))
    stored_fingerprint = str(stored.get("contract_fingerprint", ""))
    canonical_hex = str(stored.get("contract_canonical", ""))
    if not canonical_hex:
        raise ReceiptMismatch(receipt_path, "missing 'contract_canonical' field")
    try:
        contract_bytes = bytes.fromhex(canonical_hex)
    except ValueError as exc:
        raise ReceiptMismatch(receipt_path, f"'contract_canonical' is not hex: {exc}") from exc

    # 1. The contract must rebuild from the receipt's canonical bytes and its
    #    fingerprint must match the stored one.
    try:
        contract = _contract_from_canonical(json.loads(contract_bytes))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ReceiptMismatch(receipt_path, f"cannot rebuild contract from 'contract_canonical': {exc}") from exc
    recomputed_fingerprint = contract_fingerprint(contract)
    if recomputed_fingerprint != stored_fingerprint:
        raise ReceiptMismatch(
            receipt_path,
            f"contract fingerprint mismatch: stored={stored_fingerprint!r}, recomputed={recomputed_fingerprint!r}",
        )

    # 2. Re-run the replay service against the same corpus.
    sdd_dir = lineage_root.parent
    fresh = replay_contract(sdd_dir=sdd_dir, contract=contract)
    if fresh.selected_run_ids != list(stored.get("selected_run_ids", [])):
        raise ReceiptMismatch(
            receipt_path,
            f"selected_run_ids mismatch: stored="
            f"{stored.get('selected_run_ids', [])!r}, "
            f"recomputed={fresh.selected_run_ids!r}",
        )
    fresh_verdicts = [
        {
            "run_id": rv.run_id,
            "verdict": rv.verdict.value,
            "changed_subjects": rv.changed_subjects,
            "violated_invariants": rv.violated_invariants,
            "details": rv.details,
        }
        for rv in fresh.run_verdicts
    ]
    if fresh_verdicts != list(stored.get("run_verdicts", [])):
        raise ReceiptMismatch(
            receipt_path,
            f"run_verdicts mismatch: stored={stored.get('run_verdicts', [])!r}, recomputed={fresh_verdicts!r}",
        )

    # 3. The receipt's stored hash must match a fresh hash over its body.
    body = {k: v for k, v in stored.items() if k != "service_receipt_hash"}
    recomputed_hash = _receipt_hash(body)
    if not hmac.compare_digest(recomputed_hash.encode(), stored_hash.encode()):
        raise ReceiptMismatch(
            receipt_path,
            f"service_receipt_hash mismatch: stored={stored_hash!r}, recomputed={recomputed_hash!r}",
        )

    return True
