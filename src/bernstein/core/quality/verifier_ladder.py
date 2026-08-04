"""Multi-tier verifier ladder with signed per-tier receipts (#2927).

The pre-merge gate folds three different kinds of evidence -- deterministic
completion signals, the LLM judge verdict, and (where wired) human/consensus
review -- into one boolean. This module makes each tier's coverage a
first-class, re-derivable receipt in the same signed substrate the eval gate
uses (:mod:`bernstein.eval.gate_receipt`):

* every tier that *ran* seals a frozen :class:`TierRecord` (config hash,
  inputs hash, evidence hash, verdict) into the lineage spine under the
  dedicated ``verifier-ladder`` run id, kept apart from per-task journals
  exactly as ``eval-gate`` is;
* the composite :class:`LadderReceipt` binds the ordered tier records to a
  ``merge_eligible`` claim derived by the *pure*, fail-closed
  :func:`derive_ladder_verdict`; and
* each sealed tier is mirrored into the HMAC audit chain via
  :func:`~bernstein.core.security.audit_chain.record_verifier_tier` --
  hashes and verdicts only, never raw diff, rubric, or model output.

Verification (:func:`verify_ladder_receipt`) re-derives rather than trusts:
it re-hashes the stored body, re-runs :func:`derive_ladder_verdict` over the
stored tier records and rejects any receipt whose stored ``merge_eligible``
is not entailed by its tier verdicts, and re-checks every tier's
``spine_entry_hash`` against the spine entry's content hash -- proving each
tier sealed *this* evidence, not a substituted one. Remove the spine and the
composite claim fails closed instead of passing trivially.

This module records what ran; it never decides what runs. Gate policy, tier
ordering, and when a later rung is consulted are unchanged, and nothing here
invokes a human review.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bernstein.core.lineage.spine import LineageSpine, content_hash_of

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from bernstein.core.security.audit_chain import AuditChainStore

logger = logging.getLogger(__name__)

#: Version stamped into every ladder receipt. Bump only on a wire-format
#: change.
LADDER_RECEIPT_SCHEMA_VERSION = 1

#: Lineage run id under which every tier record and ladder receipt is
#: anchored, kept separate so ladder entries never interleave with per-task
#: journals (mirrors ``EVAL_GATE_RUN_ID``).
VERIFIER_LADDER_RUN_ID = "verifier-ladder"

#: Closed verdict set for a tier record. ``skip`` records a tier that was
#: consulted but did not adjudicate (a tripped judge breaker, a failed LLM
#: call, prerequisite signals failing) -- honest coverage, never a pass.
TIER_VERDICTS = ("pass", "fail", "skip")

_LADDER_ACTOR = "bernstein.verifier_ladder"
_LADDER_SUBPATH = (".sdd", "quality", "ladder")
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class VerifierTier(StrEnum):
    """The ordered rungs of the pre-merge verifier ladder.

    Definition order is ladder order: a later rung is only consulted per the
    existing gate policy, but every rung that runs must record.
    """

    DETERMINISTIC = "deterministic"
    JUDGE = "judge"
    HUMAN = "human"


#: Ladder order of the tiers (the enum's definition order).
LADDER_ORDER: tuple[VerifierTier, ...] = tuple(VerifierTier)

#: Tiers whose sealed record must be present -- with a ``pass`` verdict --
#: before :func:`derive_ladder_verdict` can find a ladder merge-eligible.
DEFAULT_REQUIRED_TIERS: tuple[VerifierTier, ...] = (VerifierTier.DETERMINISTIC,)


def canonical_hash(obj: Any) -> str:
    """Return the ``sha256:`` digest of *obj*'s canonical JSON bytes.

    Canonical means sorted keys, minimal separators, UTF-8 -- the same
    convention every receipt in this codebase hashes with, so two machines
    hashing the same structure agree byte-for-byte.
    """
    payload = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _require_canonical_hash(value: str, field_name: str) -> None:
    if not _HASH_RE.match(value):
        msg = f"{field_name} is not a canonical sha256: digest: {value!r}"
        raise ValueError(msg)


def _refuse_unprobeable_or_linked(probe: Path) -> None:
    """Fail-closed link probe for one ladder-store component.

    Deliberately not ``is_filesystem_link``: that shared helper answers
    ``False`` when the probe itself fails (a best-effort contract that
    serves the worktree GC sweep), and a store walk that cannot prove a
    component is not a link must refuse rather than continue. A component
    that does not exist yet is fine -- ``is_symlink`` / ``is_junction``
    return ``False`` without raising for a missing path -- so sealing into
    a fresh workdir still creates the store.

    Raises:
        ValueError: The component is a symlink or junction, or the probe
            itself failed.
    """
    try:
        linked = probe.is_symlink()
        if not linked:
            probe_junction = getattr(probe, "is_junction", None)
            linked = probe_junction is not None and bool(probe_junction())
    except OSError as exc:
        msg = f"ladder receipt store component could not be probed for links; refusing: {probe}: {exc.errno}"
        raise ValueError(msg) from exc
    if linked:
        msg = f"ladder receipt store path is a symlink or junction; refusing to follow it: {probe}"
        raise ValueError(msg)


def _read_leaf_text(path: Path) -> str:
    """Read one receipt leaf without following a symlink planted there.

    Opens with ``O_NOFOLLOW`` so a symlink swapped in at the receipt
    filename after path validation is rejected atomically by the read
    itself -- a separate pre-check would leave a TOCTOU window. Mirrors the
    CAS blob read (:mod:`bernstein.core.persistence.cas_store`). The flag
    is POSIX-only; where it is absent it degrades to 0. A symlinked leaf
    surfaces as ``OSError`` (``ELOOP``), which callers classify as
    unreadable, never parsed.
    """
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "r", encoding="utf-8") as handle:
        return handle.read()


def ladder_receipt_path(workdir: Path, receipt_hash: str) -> Path:
    """Return the on-disk receipt path for *receipt_hash* under *workdir*.

    The hash is validated against ``sha256:<64 hex>`` and the resolved path
    is asserted to stay under the ladder directory, so a caller-influenced
    hash can never escape the receipt store (path-injection defense in
    depth). A receipt store relocated via a filesystem link is refused
    outright: with ``.sdd``, ``.sdd/quality``, or the ladder directory
    itself symlinked (or, on Windows, junctioned -- ``Path.is_symlink()`` is
    ``False`` for NTFS junctions) elsewhere, base and candidate both
    resolve into the link's target and a realpath containment check passes
    vacuously, so the store would follow attacker-placed content. The probe
    fails closed: a component that cannot be probed for links refuses by
    name rather than continuing. Same posture as the MCP shutdown barrier's
    refusal of a ``.sdd`` symlinked elsewhere (#3080). The returned path is
    the *resolved* candidate. Directory-component races are accepted; leaf
    opens are no-follow per :mod:`bernstein.core.persistence.cas_store`.

    Raises:
        ValueError: The hash is not a canonical ``sha256:`` digest, a
            component of the ladder directory is a symlink or junction or
            could not be probed, or the resolved path escapes the ladder
            directory.
    """
    _require_canonical_hash(receipt_hash, "receipt_hash")
    probe = workdir
    for part in _LADDER_SUBPATH:
        probe = probe / part
        _refuse_unprobeable_or_linked(probe)
    base = workdir.joinpath(*_LADDER_SUBPATH)
    candidate = base / f"{receipt_hash}.json"
    base_real = os.path.realpath(base)
    cand_real = os.path.realpath(candidate)
    if os.path.commonpath([base_real, cand_real]) != base_real:
        msg = f"receipt path escapes ladder directory: {receipt_hash!r}"
        raise ValueError(msg)
    return Path(cand_real)


@dataclass(frozen=True, slots=True)
class TierRecord:
    """One sealed verifier-tier record.

    Binds, for one rung of the ladder: the tier's own configuration
    (``config_hash``), the attributed inputs it saw (``inputs_hash``), the
    structured findings it produced (``evidence_hash``), and its verdict.
    ``spine_entry_hash`` is assigned post-seal and is not part of the
    canonical bytes (it anchors them, mirroring
    ``VerdictReceipt.journal_entry_hash``).
    """

    tier: VerifierTier
    config_hash: str
    inputs_hash: str
    evidence_hash: str
    verdict: str
    spine_entry_hash: str = ""

    def __post_init__(self) -> None:
        if self.verdict not in TIER_VERDICTS:
            msg = f"tier verdict must be one of {TIER_VERDICTS}, got {self.verdict!r}"
            raise ValueError(msg)
        _require_canonical_hash(self.config_hash, "config_hash")
        _require_canonical_hash(self.inputs_hash, "inputs_hash")
        _require_canonical_hash(self.evidence_hash, "evidence_hash")
        if self.spine_entry_hash:
            _require_canonical_hash(self.spine_entry_hash, "spine_entry_hash")

    def body(self) -> dict[str, Any]:
        """The sealed body: every field except the post-seal spine anchor."""
        return {
            "tier": self.tier.value,
            "config_hash": self.config_hash,
            "inputs_hash": self.inputs_hash,
            "evidence_hash": self.evidence_hash,
            "verdict": self.verdict,
        }

    def canonical_bytes(self) -> bytes:
        """Canonical bytes sealed into the ``verifier-ladder`` spine run."""
        return json.dumps(self.body(), ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")

    def to_dict(self) -> dict[str, Any]:
        payload = self.body()
        payload["spine_entry_hash"] = self.spine_entry_hash
        return payload

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> TierRecord:
        return cls(
            tier=VerifierTier(str(raw["tier"])),
            config_hash=str(raw["config_hash"]),
            inputs_hash=str(raw["inputs_hash"]),
            evidence_hash=str(raw["evidence_hash"]),
            verdict=str(raw["verdict"]),
            spine_entry_hash=str(raw.get("spine_entry_hash", "")),
        )


def derive_ladder_verdict(
    records: Sequence[TierRecord],
    *,
    required_tiers: Sequence[VerifierTier] = DEFAULT_REQUIRED_TIERS,
) -> bool:
    """Derive the composite ``merge_eligible`` claim from tier records. Pure.

    Fail-closed by construction:

    * an empty record set is never eligible (no coverage is not coverage);
    * a required rung with no record is never eligible (an absent tier must
      not read as a passing one); and
    * any record whose verdict is not ``pass`` -- a ``fail`` *or* a ``skip``
      -- blocks eligibility: a tier that was consulted but did not adjudicate
      cannot support the composite claim.
    """
    if not records:
        return False
    present = {record.tier for record in records}
    if any(tier not in present for tier in required_tiers):
        return False
    return all(record.verdict == "pass" for record in records)


@dataclass(frozen=True, slots=True)
class LadderReceipt:
    """The composite, spine-anchored ladder receipt for one task.

    The body (everything ``receipt_hash`` covers) binds the task id, the
    ordered tier records *including their spine anchors*, the required-tier
    policy the verdict was derived under, the derived ``merge_eligible``
    claim, the schema version, and the timestamp. The receipt's own
    ``spine_entry_hash`` is assigned post-seal and excluded from the hash.
    """

    schema_version: int
    task_id: str
    records: tuple[TierRecord, ...]
    required_tiers: tuple[str, ...]
    merge_eligible: bool
    timestamp: int
    receipt_hash: str
    spine_entry_hash: str = ""

    def body(self) -> dict[str, Any]:
        """The hashed body: every field except the receipt hash and anchor."""
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "records": [record.to_dict() for record in self.records],
            "required_tiers": list(self.required_tiers),
            "merge_eligible": self.merge_eligible,
            "timestamp": self.timestamp,
        }

    def canonical_payload_without_anchor(self) -> str:
        """Canonical JSON of the body plus receipt hash (excludes the anchor).

        Two machines seal byte-identical bytes here; the receipt's own spine
        anchor is the only field excluded from the cross-machine equality
        contract.
        """
        payload = self.body()
        payload["receipt_hash"] = self.receipt_hash
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    def canonical_bytes(self) -> bytes:
        """Canonical bytes sealed into the ``verifier-ladder`` spine run."""
        return self.canonical_payload_without_anchor().encode("utf-8")

    def to_dict(self) -> dict[str, Any]:
        payload = self.body()
        payload["receipt_hash"] = self.receipt_hash
        payload["spine_entry_hash"] = self.spine_entry_hash
        return payload

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> LadderReceipt:
        return cls(
            schema_version=int(raw["schema_version"]),
            task_id=str(raw["task_id"]),
            records=tuple(TierRecord.from_dict(record) for record in raw["records"]),
            required_tiers=tuple(str(tier) for tier in raw["required_tiers"]),
            merge_eligible=bool(raw["merge_eligible"]),
            timestamp=int(raw["timestamp"]),
            receipt_hash=str(raw["receipt_hash"]),
            spine_entry_hash=str(raw.get("spine_entry_hash", "")),
        )


@dataclass(frozen=True, slots=True)
class VerifierLadderContext:
    """Opt-in wiring context for the janitor's ladder emission.

    ``run_janitor()`` seals tier records and builds a ladder receipt only
    when a context is supplied -- the default (``None``) leaves janitor
    behaviour and every ``JanitorResult`` consumer unchanged.

    Attributes:
        lineage_root: ``.sdd/lineage`` root for the ``verifier-ladder`` spine.
        hmac_key: Audit-chain HMAC key for the spine seal.
        chain: Optional audit chain accepting the per-tier mirrors.
        timestamp: Injected clock for the spine seals; ``None`` reads the
            wall clock at emission time. Tests inject a fixed value so
            identical evidence seals byte-identically.
        required_tiers: Tiers whose passing record eligibility requires.
    """

    lineage_root: Path
    hmac_key: bytes
    chain: AuditChainStore | None = None
    timestamp: int | None = None
    required_tiers: tuple[VerifierTier, ...] = DEFAULT_REQUIRED_TIERS


def recompute_ladder_receipt_hash(payload: Mapping[str, Any]) -> str:
    """Recompute the ``receipt_hash`` from a receipt dict's body fields.

    Exposed so a verifier (or a test) can confirm that a receipt whose hashes
    are internally consistent is still rejected when its stored
    ``merge_eligible`` is not entailed by its tier verdicts.
    """
    body = {
        "schema_version": int(payload["schema_version"]),
        "task_id": str(payload["task_id"]),
        "records": payload["records"],
        "required_tiers": payload["required_tiers"],
        "merge_eligible": bool(payload["merge_eligible"]),
        "timestamp": int(payload["timestamp"]),
    }
    return canonical_hash(body)


def _seal_tier_record(record: TierRecord, spine: LineageSpine, *, task_id: str, timestamp: int) -> TierRecord:
    """Seal one tier record's canonical bytes into *spine* and anchor it."""
    content = record.canonical_bytes()
    digest = content_hash_of(content).removeprefix("sha256:")
    artifact_path = "/".join((*_LADDER_SUBPATH, "tier", f"{digest}.json"))
    anchor = spine.record(
        artifact_path=artifact_path,
        content=content,
        actor=_LADDER_ACTOR,
        step_id=f"verifier-tier:{record.tier.value}:{task_id}",
        model="",
        timestamp=timestamp,
    )
    return replace(record, spine_entry_hash=anchor)


def build_ladder_receipt(
    *,
    task_id: str,
    records: Sequence[TierRecord],
    workdir: Path,
    lineage_root: Path,
    hmac_key: bytes,
    timestamp: int,
    required_tiers: Sequence[VerifierTier] = DEFAULT_REQUIRED_TIERS,
    chain: AuditChainStore | None = None,
) -> LadderReceipt:
    """Seal each tier record into the spine and build the composite receipt.

    Records are ordered by ladder order, each is sealed into the
    ``verifier-ladder`` spine run and takes the returned entry hash as its
    anchor, the composite ``merge_eligible`` is derived by the pure
    :func:`derive_ladder_verdict`, and the receipt itself is hashed, sealed,
    written under ``.sdd/quality/ladder``, and (when a *chain* is supplied)
    mirrored per tier into the HMAC audit chain.

    Args:
        task_id: The task whose work the ladder verified.
        records: One unsealed :class:`TierRecord` per tier that ran; at most
            one record per tier.
        workdir: Project root (receipt written under ``.sdd/quality/ladder``).
        lineage_root: ``.sdd/lineage`` root for the spine.
        hmac_key: Audit-chain HMAC key for the spine seal.
        timestamp: Integer timestamp anchored into every spine entry
            (injected, so identical inputs seal byte-identically).
        required_tiers: Tiers whose passing record eligibility requires.
        chain: Optional :class:`AuditChainStore` accepting the tier mirrors.

    Returns:
        The sealed :class:`LadderReceipt`.

    Raises:
        ValueError: *records* is empty or names the same tier twice.
    """
    if not records:
        msg = "a ladder receipt needs at least one tier record"
        raise ValueError(msg)
    tiers = [record.tier for record in records]
    if len(set(tiers)) != len(tiers):
        msg = f"duplicate tier records: {[t.value for t in tiers]}"
        raise ValueError(msg)

    ordered = sorted(records, key=lambda record: LADDER_ORDER.index(record.tier))
    spine = LineageSpine(lineage_root, run_id=VERIFIER_LADDER_RUN_ID, hmac_key=hmac_key)
    sealed_records = tuple(_seal_tier_record(record, spine, task_id=task_id, timestamp=timestamp) for record in ordered)

    merge_eligible = derive_ladder_verdict(sealed_records, required_tiers=required_tiers)
    unsealed = LadderReceipt(
        schema_version=LADDER_RECEIPT_SCHEMA_VERSION,
        task_id=task_id,
        records=sealed_records,
        required_tiers=tuple(tier.value for tier in required_tiers),
        merge_eligible=merge_eligible,
        timestamp=timestamp,
        receipt_hash="",
    )
    receipt_hash = canonical_hash(unsealed.body())
    sealed_no_anchor = replace(unsealed, receipt_hash=receipt_hash)

    anchor = spine.record(
        artifact_path="/".join((*_LADDER_SUBPATH, f"{receipt_hash.removeprefix('sha256:')}.json")),
        content=sealed_no_anchor.canonical_bytes(),
        actor=_LADDER_ACTOR,
        step_id=f"verifier-ladder:{task_id}",
        model="",
        timestamp=timestamp,
    )
    sealed = replace(sealed_no_anchor, spine_entry_hash=anchor)

    path = ladder_receipt_path(workdir, receipt_hash)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(sealed.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )

    if chain is not None:
        from bernstein.core.security.audit_chain import record_verifier_tier

        for record in sealed_records:
            record_verifier_tier(
                chain=chain,
                receipt_hash=receipt_hash,
                task_id=task_id,
                tier=record.tier.value,
                config_hash=record.config_hash,
                inputs_hash=record.inputs_hash,
                evidence_hash=record.evidence_hash,
                verdict=record.verdict,
                spine_entry_hash=record.spine_entry_hash,
            )
    return sealed


def read_ladder_receipt(workdir: Path, receipt_hash: str) -> LadderReceipt | None:
    """Return the sealed receipt for *receipt_hash* or ``None`` if absent/bad."""
    try:
        path = ladder_receipt_path(workdir, receipt_hash)
    except ValueError:
        return None
    try:
        raw = _read_leaf_text(path)
    except FileNotFoundError:
        return None
    except OSError:
        logger.warning("verifier ladder: receipt leaf refused a no-follow open at %s", path)
        return None
    try:
        return LadderReceipt.from_dict(json.loads(raw))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        logger.warning("verifier ladder: malformed receipt at %s", path)
        return None


@dataclass(frozen=True, slots=True)
class LadderVerifyResult:
    """Outcome of an offline ladder-receipt verification.

    ``status`` is ``"ok"``, ``"missing"`` (no readable receipt for the
    requested hash), or ``"failed"`` (any re-derivation or anchor mismatch).
    """

    ok: bool
    status: str
    reason: str
    receipt: LadderReceipt | None


def _fail(status: str, reason: str, receipt: LadderReceipt | None) -> LadderVerifyResult:
    return LadderVerifyResult(ok=False, status=status, reason=reason, receipt=receipt)


def verify_ladder_receipt(
    *,
    workdir: Path,
    lineage_root: Path,
    hmac_key: bytes,
    receipt_hash: str,
) -> LadderVerifyResult:
    """Re-verify the ladder receipt for *receipt_hash* offline.

    Re-derives, never trusts:

    * the receipt hash must recompute from the stored body (catches any
      mutated field when the hash was not recomputed);
    * the stored ``merge_eligible`` must be entailed by the stored tier
      verdicts: :func:`derive_ladder_verdict` is re-run under the stored
      required-tier policy, so a forged composite claim is rejected even
      when the receipt's hashes are internally consistent;
    * the ``verifier-ladder`` spine must verify (Merkle chain + HMAC tags);
      a removed or emptied spine fails closed -- without the substrate no
      tier can be confirmed to have run;
    * every tier record's ``spine_entry_hash`` must resolve to a spine entry
      whose content hash equals the record's own canonical bytes -- proving
      that tier sealed *this* evidence, not a substituted one; and
    * the receipt's own anchor must resolve the same way.

    ``missing`` is reserved for a receipt that cannot be read at all (no
    file, unreadable, malformed JSON, or a refused store path such as a
    symlinked ladder directory). A receipt that *is* readable but does not
    construct -- an unknown tier name, an invalid verdict, a malformed hash
    -- reports as ``failed`` verification: ``receipt_hash`` is an unsigned
    content hash anyone can recompute over a tampered body, so a forged
    field must be a named rejection, never a crash and never a silent
    "missing".
    """
    try:
        path = ladder_receipt_path(workdir, receipt_hash)
    except ValueError as exc:
        return _fail("missing", f"no readable ladder receipt for {receipt_hash!r}: {exc}", None)
    if not path.is_file():
        return _fail("missing", f"no ladder receipt for {receipt_hash!r}", None)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _fail("missing", f"no readable ladder receipt for {receipt_hash!r} (malformed JSON)", None)
    try:
        receipt = LadderReceipt.from_dict(raw)
    except (KeyError, TypeError, ValueError) as exc:
        return _fail("failed", f"receipt is readable but not a valid ladder receipt: {exc}", None)
    if receipt.receipt_hash != receipt_hash:
        return _fail("failed", "receipt hash does not match request", receipt)

    if canonical_hash(receipt.body()) != receipt.receipt_hash:
        return _fail("failed", "receipt_hash does not recompute from the receipt body (tampered)", receipt)

    known = {tier.value for tier in VerifierTier}
    unknown = sorted(set(receipt.required_tiers) - known)
    if unknown:
        return _fail("failed", f"invalid required_tiers policy in receipt: {unknown}", receipt)
    required = tuple(VerifierTier(tier) for tier in receipt.required_tiers)
    rederived = derive_ladder_verdict(receipt.records, required_tiers=required)
    if rederived != receipt.merge_eligible:
        return _fail(
            "failed",
            "stored merge_eligible is not entailed by its tier verdicts (re-derivation mismatch)",
            receipt,
        )

    spine = LineageSpine(lineage_root, run_id=VERIFIER_LADDER_RUN_ID, hmac_key=hmac_key)
    report = spine.verify()
    if not report.ok:
        detail = "; ".join(report.errors) if report.errors else report.status.value
        return _fail("failed", f"verifier-ladder spine failed verification: {detail}", receipt)

    entries = {entry.entry_hash: entry.content_hash for entry in spine.iter_entries()}
    for record in receipt.records:
        expected = content_hash_of(record.canonical_bytes())
        if entries.get(record.spine_entry_hash) != expected:
            return _fail(
                "failed",
                f"tier {record.tier.value!r} has no spine anchor sealing its evidence "
                "(substituted or dangling spine_entry_hash)",
                receipt,
            )

    if entries.get(receipt.spine_entry_hash) != content_hash_of(receipt.canonical_bytes()):
        return _fail("failed", "receipt has no spine anchor sealing its canonical bytes", receipt)

    return LadderVerifyResult(ok=True, status="ok", reason="", receipt=receipt)


__all__ = [
    "DEFAULT_REQUIRED_TIERS",
    "LADDER_ORDER",
    "LADDER_RECEIPT_SCHEMA_VERSION",
    "TIER_VERDICTS",
    "VERIFIER_LADDER_RUN_ID",
    "LadderReceipt",
    "LadderVerifyResult",
    "TierRecord",
    "VerifierLadderContext",
    "VerifierTier",
    "build_ladder_receipt",
    "canonical_hash",
    "derive_ladder_verdict",
    "ladder_receipt_path",
    "read_ladder_receipt",
    "recompute_ladder_receipt_hash",
    "verify_ladder_receipt",
]
