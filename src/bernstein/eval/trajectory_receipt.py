"""Signed, independently-replayable trajectory receipts for benchmark scores (#2925).

A published benchmark number is not an audit artefact; it is a bare scalar.
This module makes it one.  :func:`build_trajectory_receipt` seals the exact
replayable trajectory that produced a score into a content-addressed,
spine-anchored, offline-verifiable envelope whose verification *re-derives*
the score from the embedded trajectory -- never trusts the printed aggregate.

The design mirrors :mod:`bernstein.eval.gate_receipt` exactly: the receipt IS
the proof, not a decoration on a log line.

What a sealed receipt establishes offline, without re-running the benchmark:

* **Byte integrity** -- the stored file is exactly the canonical encoding of
  the content its ``receipt_hash`` covers.  Verification re-canonicalises the
  stored bytes and compares them against the file, so an unknown key, a
  duplicate key, or a reordered field is a rejection rather than a difference
  the decoder silently drops before rehashing.
* **Contamination** -- the suite the number was scored on is the suite it
  claims, via ``suite_content_hash`` recomputed from the embedded task ids.
* **Internal consistency** -- the published scalar is entailed by the embedded
  per-task components under the harness formula.  A scalar edited in isolation,
  or an aggregate inflated away from its anchors, is rejected.
* **Selection disclosure** -- the signed body always states the selection mode.
  A best-of-N run must carry all N candidate heads plus the selection rule, and
  a single-shot run carries an explicit signed assertion to that effect rather
  than an absent field.
* **Spine anchoring** -- the canonical bytes are anchored in the ``eval-bench``
  lineage spine under a verified HMAC chain.

What a receipt does **not** establish: that the anchored journals exist, or
that replaying them reproduces the sealed per-task components.
``journal_head_hash`` and ``events_content_hash`` *name* the trajectory to
replay; executing that replay is a separate operator step against the journals
themselves.  A receipt whose components were invented wholesale, with journal
hashes pointing at nothing, is internally consistent and will verify.  The
receipt binds a published score to a named trajectory; it does not re-run it.

Offline third-party verifiability without the HMAC key is out of scope for
this module (COSE/in-toto projection is the second PR); everything here is
verifiable by a key-holding operator.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import math
import os
import re
import tempfile
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.lineage.spine import LineageSpine, content_hash_of
from bernstein.core.security.path_containment import (
    PathContainmentError,
    contained_subpath,
)
from bernstein.eval.metrics import EvalScoreComponents, TierScores
from bernstein.eval.significance import suite_content_hash

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from bernstein.core.security.audit_chain import AuditChainStore

logger = logging.getLogger(__name__)

#: Version stamped into every trajectory receipt.  Bump only on a wire-format
#: change.
TRAJECTORY_RECEIPT_SCHEMA_VERSION = 1

#: Lineage run id under which every trajectory receipt is anchored, kept
#: separate so benchmark receipts never interleave with eval-gate receipts or
#: per-task journals.
EVAL_BENCH_RUN_ID = "eval-bench"

#: Status written when the receipt covers zero tasks.
NO_TASKS_STATUS = "NO_TASKS"

#: Selection mode for a run that evaluated exactly one candidate.  Always
#: written explicitly so "not best-of-N" is a signed assertion in the body
#: rather than the absence of a field.
SELECTION_SINGLE_SHOT = "single_shot"

#: Selection mode for a run that evaluated N candidates and published one.
SELECTION_BEST_OF_N = "best_of_n"

_BENCH_ACTOR = "bernstein.eval_bench"
_BENCH_SUBPATH = (".sdd", "eval", "bench")
_RECEIPT_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


# ---------------------------------------------------------------------------
# Per-task anchor
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TaskTrajectoryAnchor:
    """Content-addressed anchors for one task's sealed trajectory.

    Attributes:
        task_id: The golden task identifier.
        journal_head_hash: Merkle head of the ``EventJournal`` after the run.
        events_content_hash: ``sha256:``-prefixed hash of the ``events.jsonl``
            fixture bytes captured by ``ReplayGateway``.
        model_id: Model identifier used for the run.
        config_fingerprint: Stable identifier for the run configuration.
        components: Per-task score components sealed into the receipt.
    """

    task_id: str
    journal_head_hash: str
    events_content_hash: str
    model_id: str
    config_fingerprint: str
    components: EvalScoreComponents

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "journal_head_hash": self.journal_head_hash,
            "events_content_hash": self.events_content_hash,
            "model_id": self.model_id,
            "config_fingerprint": self.config_fingerprint,
            "components": {
                "task_success": self.components.task_success,
                "code_quality": self.components.code_quality,
                "efficiency": self.components.efficiency,
                "reliability": self.components.reliability,
                "safety": self.components.safety,
            },
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> TaskTrajectoryAnchor:
        c = raw["components"]
        return cls(
            task_id=str(raw["task_id"]),
            journal_head_hash=str(raw["journal_head_hash"]),
            events_content_hash=str(raw["events_content_hash"]),
            model_id=str(raw["model_id"]),
            config_fingerprint=str(raw["config_fingerprint"]),
            components=EvalScoreComponents(
                task_success=float(c["task_success"]),
                code_quality=float(c["code_quality"]),
                efficiency=float(c["efficiency"]),
                reliability=float(c["reliability"]),
                safety=float(c["safety"]),
            ),
        )


# ---------------------------------------------------------------------------
# Best-of-N selection provenance
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BestOfNProvenance:
    """Provenance record for a best-of-N selection.

    When the published run used ``BestOfNRunner``, the receipt carries the
    journal heads of **all N candidates** and the deterministic selection rule.
    A receipt carrying only the winner's head is rejected as unverifiable.

    Attributes:
        n_candidates: Total number of candidates evaluated.
        candidate_journal_heads: Journal head hashes for every candidate, in
            evaluation order.
        selection_rule: Human-readable description of the selection rule
            (e.g. ``"highest_final_score"``).
        selected_index: Zero-based index of the selected winner.
    """

    n_candidates: int
    candidate_journal_heads: list[str]
    selection_rule: str
    selected_index: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_candidates": self.n_candidates,
            "candidate_journal_heads": list(self.candidate_journal_heads),
            "selection_rule": self.selection_rule,
            "selected_index": self.selected_index,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> BestOfNProvenance:
        return cls(
            n_candidates=int(raw["n_candidates"]),
            candidate_journal_heads=list(raw["candidate_journal_heads"]),
            selection_rule=str(raw["selection_rule"]),
            selected_index=int(raw["selected_index"]),
        )


# ---------------------------------------------------------------------------
# The receipt
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TrajectoryReceipt:
    """A sealed benchmark-score trajectory receipt.

    The body (everything the ``receipt_hash`` covers) binds the suite identity,
    every per-task trajectory anchor, the aggregate score components and tier
    scores, the schema version, and (when applicable) best-of-N provenance.
    No wall-clock value enters the signed bytes.  The ``journal_entry_hash``
    is assigned post-seal and is NOT part of the hashed body.
    """

    schema_version: int
    suite_content_hash: str
    published_score: float
    task_anchors: list[TaskTrajectoryAnchor]
    aggregate: EvalScoreComponents
    per_tier: TierScores
    run_id: str
    status: str
    best_of_n: BestOfNProvenance | None
    selection_mode: str
    receipt_hash: str
    journal_entry_hash: str = ""

    def body(self) -> dict[str, Any]:
        """The hashed body: every field except ``receipt_hash`` and anchor."""
        d: dict[str, Any] = {
            "schema_version": self.schema_version,
            "suite_content_hash": self.suite_content_hash,
            "published_score": self.published_score,
            "task_anchors": [a.to_dict() for a in self.task_anchors],
            "aggregate": {
                "task_success": self.aggregate.task_success,
                "code_quality": self.aggregate.code_quality,
                "efficiency": self.aggregate.efficiency,
                "reliability": self.aggregate.reliability,
                "safety": self.aggregate.safety,
            },
            "per_tier": {
                "smoke": self.per_tier.smoke,
                "standard": self.per_tier.standard,
                "stretch": self.per_tier.stretch,
                "adversarial": self.per_tier.adversarial,
            },
            "run_id": self.run_id,
            "status": self.status,
            "selection_mode": self.selection_mode,
            "best_of_n": self.best_of_n.to_dict() if self.best_of_n is not None else None,
        }
        return d

    def canonical_payload_without_anchor(self) -> str:
        """Canonical JSON of the body plus receipt hash (excludes the anchor).

        Two machines seal byte-identical bytes here; the lineage anchor is the
        only field that could differ, so it is excluded from the cross-machine
        equality contract.
        """
        payload = self.body()
        payload["receipt_hash"] = self.receipt_hash
        return _canonical_dumps(payload)

    def to_dict(self) -> dict[str, Any]:
        payload = self.body()
        payload["receipt_hash"] = self.receipt_hash
        payload["journal_entry_hash"] = self.journal_entry_hash
        return payload

    def canonical_bytes(self) -> bytes:
        """Canonical bytes sealed into the lineage spine (body + hash)."""
        return self.canonical_payload_without_anchor().encode("utf-8")

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> TrajectoryReceipt:
        agg = raw["aggregate"]
        pt = raw["per_tier"]
        bon_raw = raw.get("best_of_n")
        return cls(
            schema_version=int(raw["schema_version"]),
            suite_content_hash=str(raw["suite_content_hash"]),
            published_score=float(raw["published_score"]),
            task_anchors=[TaskTrajectoryAnchor.from_dict(a) for a in raw["task_anchors"]],
            aggregate=EvalScoreComponents(
                task_success=float(agg["task_success"]),
                code_quality=float(agg["code_quality"]),
                efficiency=float(agg["efficiency"]),
                reliability=float(agg["reliability"]),
                safety=float(agg["safety"]),
            ),
            per_tier=TierScores(
                smoke=float(pt["smoke"]),
                standard=float(pt["standard"]),
                stretch=float(pt["stretch"]),
                adversarial=float(pt["adversarial"]),
            ),
            run_id=str(raw["run_id"]),
            status=str(raw["status"]),
            best_of_n=BestOfNProvenance.from_dict(bon_raw) if bon_raw is not None else None,
            selection_mode=str(raw["selection_mode"]),
            receipt_hash=str(raw["receipt_hash"]),
            journal_entry_hash=str(raw["journal_entry_hash"]),
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class ReceiptBytesError(ValueError):
    """Stored receipt bytes are not a faithful canonical encoding of a receipt.

    Raised when the file on disk carries anything the receipt schema would drop
    on the way to a hash: an unrecognised key, a duplicate key, reordered or
    re-spaced fields, or a non-finite numeric literal.  Treating these as a
    decode failure is what stops a tampered file from being laundered into a
    matching hash by rehashing the parsed projection instead of the bytes.
    """


def _canonical_dumps(obj: Any) -> str:
    """Canonical JSON serialisation used for every hash and every comparison.

    ``allow_nan=False`` keeps the output inside RFC 8259, so a receipt written
    here decodes identically in a verifier that is not Python.
    """
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)


def _reject_nonfinite(name: str) -> float:
    """``json.loads`` hook rejecting the ``NaN``/``Infinity`` extensions."""
    msg = f"non-finite JSON literal {name!r} is not valid JSON in a receipt"
    raise ReceiptBytesError(msg)


def _require_finite_components(components: EvalScoreComponents, *, where: str) -> None:
    """Reject non-finite score components.

    ``NaN`` defeats every tolerance comparison in this module: ``abs(x - nan)
    > tol`` is False, so a ``NaN`` slipped into a scalar would pass the
    published-score check rather than fail it.  Rejecting at the boundary keeps
    the comparisons meaningful.

    Raises:
        ValueError: Any component is ``NaN`` or infinite.
    """
    for field_name in ("task_success", "code_quality", "efficiency", "reliability", "safety"):
        value = getattr(components, field_name)
        if not math.isfinite(value):
            msg = f"non-finite {field_name} in {where}: {value!r}"
            raise ValueError(msg)


def _hash_obj(obj: Any) -> str:
    """Canonical JSON sha256 hash -- identical to gate_receipt._hash_obj."""
    return "sha256:" + hashlib.sha256(_canonical_dumps(obj).encode("utf-8")).hexdigest()


def decode_receipt_bytes(raw: bytes) -> TrajectoryReceipt:
    """Decode sealed receipt bytes, proving the bytes are canonical.

    The decode is deliberately not tolerant.  After parsing, the parsed record
    is re-canonicalised and compared byte-for-byte against *raw*.  Any content
    the schema does not round-trip -- an injected key, a duplicate key, a
    reordering, stray whitespace -- makes the two differ and is rejected here,
    before any hash is recomputed.

    Without this step a verifier rehashes its own parsed projection of the
    file rather than the file, and reports a clean result on bytes that were
    edited after sealing.

    Args:
        raw: The exact bytes read from the receipt file.

    Returns:
        The decoded :class:`TrajectoryReceipt`.

    Raises:
        ReceiptBytesError: The bytes are not the canonical encoding of a
            well-formed receipt.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        msg = f"receipt bytes are not valid UTF-8: {exc}"
        raise ReceiptBytesError(msg) from exc

    try:
        parsed = json.loads(text, parse_constant=_reject_nonfinite)
    except json.JSONDecodeError as exc:
        msg = f"receipt bytes are not valid JSON: {exc}"
        raise ReceiptBytesError(msg) from exc

    try:
        receipt = TrajectoryReceipt.from_dict(parsed)
    except (KeyError, TypeError, ValueError) as exc:
        msg = f"receipt bytes do not decode to a well-formed receipt: {exc}"
        raise ReceiptBytesError(msg) from exc

    try:
        recanonicalised = _canonical_dumps(receipt.to_dict())
    except ValueError as exc:  # non-finite float that survived parsing
        msg = f"receipt carries a non-finite numeric value: {exc}"
        raise ReceiptBytesError(msg) from exc

    if recanonicalised != text:
        msg = "stored receipt bytes are not the canonical encoding of their own content"
        raise ReceiptBytesError(msg)
    return receipt


def _recompute_aggregate(task_anchors: list[TaskTrajectoryAnchor]) -> EvalScoreComponents:
    """Re-derive aggregate EvalScoreComponents from per-task anchors.

    This is the formula re-derivation step in ``verify_trajectory_receipt``:
    the aggregate is never trusted -- it is always recomputed from the
    embedded per-task components using the harness formula.

        Score = (0.5*TaskSuccess + 0.3*CodeQuality + 0.2*Efficiency)
                * Reliability * Safety

    Individual per-task components are averaged to produce the aggregate.
    """
    if not task_anchors:
        return EvalScoreComponents()
    n = len(task_anchors)
    task_success = sum(a.components.task_success for a in task_anchors) / n
    code_quality = sum(a.components.code_quality for a in task_anchors) / n
    efficiency = sum(a.components.efficiency for a in task_anchors) / n
    reliability = sum(a.components.reliability for a in task_anchors) / n
    safety = sum(a.components.safety for a in task_anchors) / n
    return EvalScoreComponents(
        task_success=task_success,
        code_quality=code_quality,
        efficiency=efficiency,
        reliability=reliability,
        safety=safety,
    )


def _components_equal(a: EvalScoreComponents, b: EvalScoreComponents, *, tol: float = 1e-9) -> bool:
    """Floating-point component comparison with a tight tolerance."""
    return (
        abs(a.task_success - b.task_success) < tol
        and abs(a.code_quality - b.code_quality) < tol
        and abs(a.efficiency - b.efficiency) < tol
        and abs(a.reliability - b.reliability) < tol
        and abs(a.safety - b.safety) < tol
    )


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def trajectory_receipt_path(workdir: Path, receipt_hash: str) -> Path:
    """Return the on-disk receipt path for *receipt_hash* under *workdir*.

    The hash is validated and the resolved path is checked to stay under the
    bench directory (path-injection defence in depth).

    Raises:
        ValueError: The hash is not a canonical ``sha256:`` digest, or the
            resolved path escapes the bench directory.
    """
    if not _RECEIPT_HASH_RE.match(receipt_hash):
        msg = f"receipt_hash is not a canonical sha256 digest: {receipt_hash!r}"
        raise ValueError(msg)
    base = workdir.joinpath(*_BENCH_SUBPATH)
    # contained_subpath, not contained_path: the canonical hash carries a
    # ``sha256:`` prefix, and contained_path's single-segment allowlist has no
    # ':' in it. The candidate is still one path component -- no '/' or '\\'
    # ever reaches validate_relative_path -- so contained_subpath's weaker,
    # multi-component-shaped check enforces the same barrier this needs: the
    # allowlist step is a no-op given _RECEIPT_HASH_RE above, and the
    # realpath-containment step is what actually guards a pre-planted symlink
    # named "<hash>.json" inside the bench directory.
    try:
        return contained_subpath(base, f"{receipt_hash}.json", label="receipt hash")
    except PathContainmentError as exc:
        msg = f"receipt path escapes bench directory: {receipt_hash!r}"
        raise ValueError(msg) from exc


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def build_trajectory_receipt(
    *,
    run_id: str,
    task_anchors: list[TaskTrajectoryAnchor],
    per_tier: TierScores,
    workdir: Path,
    lineage_root: Path,
    hmac_key: bytes,
    best_of_n: BestOfNProvenance | None = None,
    chain: AuditChainStore | None = None,
) -> TrajectoryReceipt:
    """Seal a benchmark score into a signed, independently-replayable receipt.

    The receipt is content-addressed, anchored in the ``eval-bench`` lineage
    spine, written under ``.sdd/eval/bench``, and (when a *chain* is supplied)
    mirrored into the HMAC audit chain via
    :func:`~bernstein.core.security.audit_chain.record_trajectory_receipt`.

    An empty *task_anchors* list produces a receipt with
    ``status=NO_TASKS`` and ``published_score=0.0``; this is a distinct,
    verifiable state (not a trivial pass) -- mirroring the spine's
    ``NO_ENTRIES`` contract.

    No wall-clock value enters the receipt body or the signed bytes.

    Args:
        run_id: The benchmark run identifier.
        task_anchors: Per-task trajectory anchors (may be empty).
        per_tier: Per-tier pass rates.
        workdir: Project root (receipt written under ``.sdd/eval/bench``).
        lineage_root: ``.sdd/lineage`` root for the spine.
        hmac_key: Audit-chain HMAC key for the spine seal.
        best_of_n: When the run used best-of-N, provenance covering all N
            candidates.  Omitting it seals ``selection_mode=single_shot``,
            which is an explicit claim in the signed body, not a silent gap.
        chain: Optional :class:`AuditChainStore` accepting the mirror.

    Returns:
        The sealed :class:`TrajectoryReceipt`.

    Raises:
        ValueError: Two anchors share a ``task_id``, or a component value is
            not finite.
    """
    # Canonicalise task order by task_id so the receipt hash is independent of
    # the order in which the caller supplies anchors (order-canonical, not
    # order-lucky).  suite_content_hash already sorts its input, but the
    # task_anchors list itself must also be sorted so that the per-task
    # section of the body is deterministic across independent recordings.
    canonical_anchors = sorted(task_anchors, key=lambda a: a.task_id)

    # Two anchors with the same task_id collapse in suite_content_hash (which
    # de-duplicates) while both still feed the aggregate mean, so a duplicate
    # would let one task be counted twice behind an honest-looking suite hash.
    seen: set[str] = set()
    for a in canonical_anchors:
        if a.task_id in seen:
            msg = f"duplicate task_id in task_anchors: {a.task_id!r}"
            raise ValueError(msg)
        seen.add(a.task_id)
        _require_finite_components(a.components, where=f"task {a.task_id!r}")

    if not canonical_anchors:
        status = NO_TASKS_STATUS
        aggregate = EvalScoreComponents()
        published_score = 0.0
    else:
        status = "ok"
        aggregate = _recompute_aggregate(canonical_anchors)
        published_score = aggregate.final_score

    s_hash = suite_content_hash([a.task_id for a in canonical_anchors])
    selection_mode = SELECTION_SINGLE_SHOT if best_of_n is None else SELECTION_BEST_OF_N

    unsealed = TrajectoryReceipt(
        schema_version=TRAJECTORY_RECEIPT_SCHEMA_VERSION,
        suite_content_hash=s_hash,
        published_score=published_score,
        task_anchors=canonical_anchors,
        aggregate=aggregate,
        per_tier=per_tier,
        run_id=run_id,
        status=status,
        best_of_n=best_of_n,
        selection_mode=selection_mode,
        receipt_hash="",
    )
    receipt_hash = _hash_obj(unsealed.body())

    sealed_no_anchor = TrajectoryReceipt(
        schema_version=unsealed.schema_version,
        suite_content_hash=unsealed.suite_content_hash,
        published_score=unsealed.published_score,
        task_anchors=unsealed.task_anchors,
        aggregate=unsealed.aggregate,
        per_tier=unsealed.per_tier,
        run_id=unsealed.run_id,
        status=unsealed.status,
        best_of_n=unsealed.best_of_n,
        selection_mode=unsealed.selection_mode,
        receipt_hash=receipt_hash,
    )

    spine = LineageSpine(lineage_root, run_id=EVAL_BENCH_RUN_ID, hmac_key=hmac_key)
    artifact_path = "/".join((*_BENCH_SUBPATH, f"{receipt_hash}.json"))
    anchor = spine.record(
        artifact_path=artifact_path,
        content=sealed_no_anchor.canonical_bytes(),
        actor=_BENCH_ACTOR,
        step_id=receipt_hash,
        model=run_id,
        timestamp=0,  # no wall-clock in sealed bytes
    )

    sealed = TrajectoryReceipt(
        schema_version=sealed_no_anchor.schema_version,
        suite_content_hash=sealed_no_anchor.suite_content_hash,
        published_score=sealed_no_anchor.published_score,
        task_anchors=sealed_no_anchor.task_anchors,
        aggregate=sealed_no_anchor.aggregate,
        per_tier=sealed_no_anchor.per_tier,
        run_id=sealed_no_anchor.run_id,
        status=sealed_no_anchor.status,
        best_of_n=sealed_no_anchor.best_of_n,
        selection_mode=sealed_no_anchor.selection_mode,
        receipt_hash=receipt_hash,
        journal_entry_hash=anchor,
    )

    # Mirror into the audit chain BEFORE writing the receipt file so that if
    # the chain write fails the receipt file does not yet exist — the two
    # states remain reconcilable (no orphan file with no chain entry).
    if chain is not None:
        from bernstein.core.security.audit_chain import record_trajectory_receipt

        record_trajectory_receipt(
            chain=chain,
            receipt_hash=receipt_hash,
            run_id=run_id,
            suite_content_hash=s_hash,
            published_score=published_score,
            n_tasks=len(canonical_anchors),
            status=status,
            journal_entry_hash=anchor,
        )

    path = trajectory_receipt_path(workdir, receipt_hash)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(path, _canonical_dumps(sealed.to_dict()))

    return sealed


def _write_atomic(path: Path, text: str) -> None:
    """Write *text* to *path* via a same-directory temp file and ``replace``.

    The spine entry and the audit-chain mirror are already durable by the time
    the receipt file is written, so a torn write here would leave a verifier
    reading a truncated receipt for an anchor that exists.  ``os.replace`` is
    atomic within a filesystem, so a reader sees either the previous state or
    the complete receipt.
    """
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".receipt-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


def read_trajectory_receipt(workdir: Path, receipt_hash: str) -> TrajectoryReceipt | None:
    """Return the sealed receipt for *receipt_hash* or ``None`` if absent/bad.

    "Bad" includes bytes that parse but are not the canonical encoding of what
    they decode to; see :func:`decode_receipt_bytes`.
    """
    try:
        receipt, _ = _load_receipt(workdir, receipt_hash)
    except ReceiptBytesError as exc:
        logger.warning("eval: malformed trajectory receipt for %s: %s", receipt_hash, exc)
        return None
    return receipt


def _load_receipt(workdir: Path, receipt_hash: str) -> tuple[TrajectoryReceipt | None, str]:
    """Load and canonically validate one receipt.

    Returns:
        ``(receipt, reason)``.  ``receipt`` is ``None`` with a populated
        *reason* when the receipt is simply absent; a receipt that is present
        but unacceptable raises instead.

    Raises:
        ReceiptBytesError: The file exists but its bytes are not a faithful
            canonical encoding of a well-formed receipt.
    """
    try:
        path = trajectory_receipt_path(workdir, receipt_hash)
    except ValueError as exc:
        return None, str(exc)
    if not path.is_file():
        return None, f"no trajectory receipt for {receipt_hash!r}"
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return None, f"trajectory receipt unreadable for {receipt_hash!r}: {exc}"
    return decode_receipt_bytes(raw), ""


# ---------------------------------------------------------------------------
# Verify result
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TrajectoryVerifyResult:
    """Outcome of an offline trajectory-receipt verification."""

    ok: bool
    reason: str
    receipt: TrajectoryReceipt | None
    #: Zero-based index of the first divergent task anchor, or -1 when none.
    failing_task_index: int = -1
    #: The hash that was asked for, retained on every path (including the ones
    #: where the receipt could not be decoded) so a caller can always name the
    #: file it failed on.
    requested_hash: str = ""


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------


def verify_trajectory_receipt(
    *,
    workdir: Path,
    lineage_root: Path,
    hmac_key: bytes,
    receipt_hash: str,
) -> TrajectoryVerifyResult:
    """Re-verify the receipt for *receipt_hash* offline.

    Verification fails closed unless every step passes:

    0. The stored bytes are exactly the canonical encoding of the receipt they
       decode to, so the hash covers the file rather than a parsed projection
       of it (byte-integrity detection).
    1. The receipt hash recomputes from the stored body (tamper detection).
    2. The ``suite_content_hash`` recomputes from the embedded task ids
       (contamination detection).
    3. The aggregate ``EvalScoreComponents`` re-derives from the per-task
       components via the harness formula (inflated-aggregate detection).
    4. The ``published_score`` matches the recomputed ``final_score``
       (scalar-edit detection).
    5. The sealed ``selection_mode`` agrees with the presence of
       ``best_of_n``, and a best-of-N receipt carries all N candidate heads
       with an in-range selected index (cherry-pick detection).
    6. The lineage spine verifies and contains an entry whose content hash
       matches the receipt's canonical bytes and whose entry hash matches
       ``journal_entry_hash``.

    A receipt with ``status=NO_TASKS`` passes structural checks but reports
    ``published_score=0.0`` and zero task anchors; step 3 re-derives the
    same empty aggregate.

    This proves the receipt is intact and self-consistent.  It does not replay
    the anchored journals; see the module docstring for what that leaves open.

    Raises nothing; all failure modes return ``ok=False`` with a reason.
    """
    # Step 0 -- the stored bytes are canonical for their own content.
    try:
        receipt, absent_reason = _load_receipt(workdir, receipt_hash)
    except ReceiptBytesError as exc:
        return TrajectoryVerifyResult(
            ok=False,
            reason=f"receipt bytes rejected (tampered): {exc}",
            receipt=None,
            requested_hash=receipt_hash,
        )
    if receipt is None:
        return TrajectoryVerifyResult(
            ok=False,
            reason=absent_reason,
            receipt=None,
            requested_hash=receipt_hash,
        )
    if receipt.receipt_hash != receipt_hash:
        return TrajectoryVerifyResult(
            ok=False,
            reason="receipt hash does not match request",
            receipt=receipt,
            requested_hash=receipt_hash,
        )

    # Step 1 -- hash recomputes
    recomputed_hash = _hash_obj(receipt.body())
    if recomputed_hash != receipt.receipt_hash:
        return TrajectoryVerifyResult(
            ok=False,
            reason="receipt_hash does not recompute from the receipt body (tampered)",
            receipt=receipt,
            requested_hash=receipt_hash,
        )

    # Step 2 -- suite-content-hash (contamination)
    expected_suite_hash = suite_content_hash([a.task_id for a in receipt.task_anchors])
    if expected_suite_hash != receipt.suite_content_hash:
        return TrajectoryVerifyResult(
            ok=False,
            reason=(
                f"suite_content_hash mismatch: stored {receipt.suite_content_hash!r} "
                f"!= recomputed {expected_suite_hash!r} (contamination)"
            ),
            receipt=receipt,
            requested_hash=receipt_hash,
        )

    # Step 3 -- re-derive aggregate from per-task components.
    #
    # The divergence is between the stored aggregate and the aggregate implied
    # by the anchors; it does not localise to a single task.  An earlier
    # version named the anchor furthest from the recomputed mean, which is the
    # honest outlier of the suite rather than the edited record, so the report
    # now lists the divergent aggregate fields and names no task.
    recomputed_agg = _recompute_aggregate(receipt.task_anchors)
    if not _components_equal(recomputed_agg, receipt.aggregate):
        divergent = ", ".join(
            f"{name}: stored={getattr(receipt.aggregate, name)!r} recomputed={getattr(recomputed_agg, name)!r}"
            for name in ("task_success", "code_quality", "efficiency", "reliability", "safety")
            if abs(getattr(receipt.aggregate, name) - getattr(recomputed_agg, name)) >= 1e-9
        )
        return TrajectoryVerifyResult(
            ok=False,
            reason=(
                "aggregate EvalScoreComponents do not re-derive from the "
                f"{len(receipt.task_anchors)} per-task anchors; divergent fields -- {divergent}"
            ),
            receipt=receipt,
            requested_hash=receipt_hash,
        )

    # Step 4 -- published_score matches recomputed final_score (scalar edit)
    recomputed_score = recomputed_agg.final_score
    if abs(recomputed_score - receipt.published_score) > 1e-9:
        return TrajectoryVerifyResult(
            ok=False,
            reason=(
                f"published_score {receipt.published_score} does not match "
                f"recomputed final_score {recomputed_score} (scalar edit)"
            ),
            receipt=receipt,
            requested_hash=receipt_hash,
        )

    # Step 5 -- selection provenance.
    #
    # selection_mode is mandatory in the signed body, so a best-of-N receipt
    # cannot be downgraded to "single-shot" by deleting a field: the claim is
    # always stated, and the two representations must agree.
    if receipt.selection_mode not in (SELECTION_SINGLE_SHOT, SELECTION_BEST_OF_N):
        return TrajectoryVerifyResult(
            ok=False,
            reason=f"unknown selection_mode {receipt.selection_mode!r}",
            receipt=receipt,
            requested_hash=receipt_hash,
        )
    if (receipt.selection_mode == SELECTION_BEST_OF_N) != (receipt.best_of_n is not None):
        return TrajectoryVerifyResult(
            ok=False,
            reason=(
                f"selection_mode={receipt.selection_mode!r} disagrees with best_of_n "
                f"{'present' if receipt.best_of_n is not None else 'absent'} (cherry-pick)"
            ),
            receipt=receipt,
            requested_hash=receipt_hash,
        )
    if receipt.best_of_n is not None:
        bon = receipt.best_of_n
        if len(bon.candidate_journal_heads) != bon.n_candidates:
            return TrajectoryVerifyResult(
                ok=False,
                reason=(
                    f"best_of_n carries {len(bon.candidate_journal_heads)} heads "
                    f"but claims n_candidates={bon.n_candidates} (cherry-pick: missing heads)"
                ),
                receipt=receipt,
                requested_hash=receipt_hash,
            )
        if bon.selected_index < 0 or bon.selected_index >= bon.n_candidates:
            return TrajectoryVerifyResult(
                ok=False,
                reason=(
                    f"best_of_n selected_index={bon.selected_index} out of range [0, {bon.n_candidates}) (cherry-pick)"
                ),
                receipt=receipt,
                requested_hash=receipt_hash,
            )

    # Step 6 -- spine verification and anchor check
    spine = LineageSpine(lineage_root, run_id=EVAL_BENCH_RUN_ID, hmac_key=hmac_key)
    report = spine.verify()
    if not report.ok:
        detail = "; ".join(report.errors) if report.errors else report.status.value
        return TrajectoryVerifyResult(
            ok=False,
            reason=f"eval-bench spine failed verification: {detail}",
            receipt=receipt,
            requested_hash=receipt_hash,
        )

    expected_content = content_hash_of(receipt.canonical_bytes())
    anchored = any(
        entry.entry_hash == receipt.journal_entry_hash and entry.content_hash == expected_content
        for entry in spine.iter_entries()
    )
    if not anchored:
        return TrajectoryVerifyResult(
            ok=False,
            reason="receipt is not anchored in the eval-bench spine",
            receipt=receipt,
            requested_hash=receipt_hash,
        )

    return TrajectoryVerifyResult(ok=True, reason="", receipt=receipt, requested_hash=receipt_hash)


def verify_all_trajectory_receipts(workdir: Path, *, hmac_key: bytes) -> list[TrajectoryVerifyResult]:
    """Verify every trajectory receipt under ``workdir/.sdd/eval/bench``.

    Used by ``bernstein audit verify`` so a tampered benchmark score is
    detected exactly like a tampered chain entry.  Returns one result per
    receipt; returns an empty list when none exist (silent no-op, never a
    false failure).
    """
    bench_dir = workdir.joinpath(*_BENCH_SUBPATH)
    lineage_root = workdir / ".sdd" / "lineage"
    if not bench_dir.is_dir():
        return []
    results: list[TrajectoryVerifyResult] = []
    for path in sorted(bench_dir.glob("sha256:*.json")):
        receipt_hash = path.stem
        results.append(
            verify_trajectory_receipt(
                workdir=workdir,
                lineage_root=lineage_root,
                hmac_key=hmac_key,
                receipt_hash=receipt_hash,
            )
        )
    return results


__all__ = [
    "EVAL_BENCH_RUN_ID",
    "NO_TASKS_STATUS",
    "SELECTION_BEST_OF_N",
    "SELECTION_SINGLE_SHOT",
    "TRAJECTORY_RECEIPT_SCHEMA_VERSION",
    "BestOfNProvenance",
    "ReceiptBytesError",
    "TaskTrajectoryAnchor",
    "TrajectoryReceipt",
    "TrajectoryVerifyResult",
    "build_trajectory_receipt",
    "decode_receipt_bytes",
    "read_trajectory_receipt",
    "trajectory_receipt_path",
    "verify_all_trajectory_receipts",
    "verify_trajectory_receipt",
]
