"""Signed, offline-verifiable receipt for a whole run (issue #2924).

A finished run already carries two always-on verifiable substrates - the
Merkle-chained :class:`~bernstein.core.replay.journal.EventJournal` (the
run's replay identity) and the :class:`~bernstein.core.lineage.spine.LineageSpine`
(the run's artifact provenance) - plus the opt-in HMAC audit chain. Each
verifies only its own slice, and two of the three need the operator's key
material or a live ``.sdd/``. This module binds them into one signed
artefact: **the file alone proves integrity** (every head recomputes from
the embedded ranges, any post-signing mutation is caught and localized),
and **the file plus the operator's out-of-band public key proves
provenance** (see the trust-model note on :func:`verify_run_receipt`):

* ``journal``: the timing-excluded projection of every journal row (the
  exact bytes :func:`~bernstein.core.replay.journal.compute_event_hash`
  covers - wall-clock fields never enter the receipt) plus the head hash.
* ``spine``: every spine entry body *without* the keyed ``hmac`` tag.
  Spine ``entry_hash`` values are plain SHA-256 over caller-visible
  fields, so the whole chain recomputes without the operator's HMAC key.
* ``audit_range`` (opt-in): a re-chained audit slice in the same shape
  :mod:`bernstein.core.security.audit_receipt` embeds, keyed by
  ``head_sha256`` which recomputes from the embedded events alone.
* ``signing``: a detached Ed25519 signature over the DSSE
  pre-authentication encoding of the canonical *subject binding* - the
  block that carries the run id and every recomputed head - plus the
  embedded public key as an RFC 7517 / RFC 8037 OKP JWK.

Binding, not decoration
-----------------------
The signed subject is *derived from* the embedded ranges. The verifier
recomputes the journal head, the spine head, and (when present) the audit
range head from the receipt's own bytes, rebuilds the binding block from
those recomputed values, and only then checks the signature. It never
trusts a head the receipt merely asserts. Mutate any embedded row and the
recomputed head diverges at a precise step index; strip a range and the
signed subject becomes unreachable - the receipt stops verifying, it does
not merely lose a line.

Determinism
-----------
For a fixed run directory and signing key the receipt bytes are
byte-identical across independent builds: canonical JSON (sorted keys,
compact separators - the same convention every bernstein receipt and both
chain hashes already use), timing-excluded projections, and RFC 8032
deterministic Ed25519. No wall-clock value enters the serialized bytes.

The signature input is DSSE PAE (``pae(payload_type, binding_bytes)``)
with a dedicated payload type, so a run-receipt signature cannot be
replayed as any other envelope kind signed by the same key.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.core.lineage.spine import LineageSpine, compute_entry_hash
from bernstein.core.replay.journal import (
    run_journal_path,
    verify_events,
)

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Wire-format identifiers
# ---------------------------------------------------------------------------

#: Receipt schema version. Bump only on a wire-format change.
RUN_RECEIPT_SCHEMA_VERSION: str = "1.0.0"

#: Receipt type URL. Versioned so a future v2 can co-exist.
RUN_RECEIPT_TYPE: str = "https://bernstein.run/attestations/run-receipt/v1"

#: DSSE payload type signed via ``pae()``. Domain-separates the run-receipt
#: signature from every other envelope the same Ed25519 key signs.
RUN_RECEIPT_PAYLOAD_TYPE: str = "application/vnd.bernstein.run-receipt+json"

#: Receipt filename inside ``.sdd/runs/<run_id>/`` (next to the journal).
RUN_RECEIPT_FILENAME: str = "run-receipt.json"

#: Env var naming an Ed25519 private key *file* (PEM PKCS#8 or raw 32-byte
#: seed) used to sign run receipts at finalization.
SIGNING_KEY_PATH_ENV: str = "BERNSTEIN_RUN_RECEIPT_SIGNING_KEY_PATH"

#: Env var naming *another env var* that carries a PEM Ed25519 private key
#: (the :class:`~bernstein.core.security.lineage_kms.EnvBasedKMSAdapter`
#: shape used for K8s Secret mounts).
SIGNING_ENV_VAR_ENV: str = "BERNSTEIN_RUN_RECEIPT_SIGNING_ENV_VAR"

#: Env var carrying an operator-stable JWK ``kid`` for the receipt key.
SIGNING_KID_ENV: str = "BERNSTEIN_RUN_RECEIPT_SIGNING_KID"

#: Journal envelope fields that carry wall-clock values. Everything else on
#: a journal row is either a deterministic chain field or decision payload,
#: so the embedded projection keeps it.
_WALL_CLOCK_FIELDS = frozenset({"ts", "elapsed_s"})

#: Spine entry fields whose hash pre-image is optional (W3C trace context).
_SPINE_OPTIONAL_FIELDS = ("traceparent", "tracestate", "baggage")

#: Spine entry fields every embedded row must carry.
_SPINE_REQUIRED_FIELDS = (
    "v",
    "prev_hash",
    "artifact_path",
    "content_hash",
    "actor",
    "step_id",
    "model",
    "timestamp",
    "entry_hash",
)

_GENESIS = ""


class RunReceiptError(RuntimeError):
    """Raised when a run receipt cannot be built."""


# ---------------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunReceipt:
    """A built run receipt.

    Attributes:
        run_id: The attested run.
        journal_head: Recomputed journal Merkle head (the run identity).
        spine_head: Recomputed lineage-spine head (empty for a run that
            recorded no spine entries).
        audit_head_sha256: Audit-range head when the opt-in block was
            included, else ``None``.
        receipt: The serialisable receipt dict.
        receipt_bytes: Canonical JSON bytes (byte-deterministic).
        receipt_path: On-disk path when written, else ``None``.
    """

    run_id: str
    journal_head: str
    spine_head: str
    audit_head_sha256: str | None
    receipt: dict[str, Any]
    receipt_bytes: bytes
    receipt_path: Path | None = field(default=None)

    @property
    def sha256(self) -> str:
        """SHA-256 of the canonical receipt bytes."""
        return hashlib.sha256(self.receipt_bytes).hexdigest()


@dataclass(frozen=True, slots=True)
class RunReceiptVerifyResult:
    """Outcome of :func:`verify_run_receipt`.

    Attributes:
        ok: ``True`` only when every recompute and the signature pass.
        status: ``"ok"``, ``"malformed"`` (unparseable / structurally
            incomplete input), or ``"tampered"`` (a recompute or the
            signature diverged).
        run_id: Run id claimed by the receipt (best-effort on failure).
        journal_events: Number of embedded journal rows walked.
        spine_entries: Number of embedded spine entries walked.
        divergent_step: 0-based index of the first divergent journal step,
            when journal tamper was located.
        errors: Human-readable explanations, first failure first.
    """

    ok: bool
    status: str
    run_id: str = ""
    journal_events: int = 0
    spine_entries: int = 0
    divergent_step: int | None = None
    errors: list[str] = field(default_factory=list[str])


# ---------------------------------------------------------------------------
# Canonical helpers (shared with the audit receipt family)
# ---------------------------------------------------------------------------


def _canonical_json_bytes(obj: dict[str, Any]) -> bytes:
    """Deterministic JSON bytes - the audit-receipt convention, reused.

    Imported lazily-by-copy is deliberately avoided: delegate to the one
    definition in :mod:`bernstein.core.security.audit_receipt` so the whole
    receipt family canonicalises identically.
    """
    from bernstein.core.security.audit_receipt import _canonical_json_bytes as _cjb

    return _cjb(obj)


def _binding_block(
    *,
    run_id: str,
    journal_head: str,
    journal_count: int,
    spine_head: str,
    spine_count: int,
    audit_head_sha256: str | None,
) -> dict[str, Any]:
    """The subject binding: one canonical block over every recomputed head.

    The verifier rebuilds this block from *recomputed* values, so any
    asserted head the receipt carries is decoration - the signature only
    ever matches when the embedded bytes re-derive every field here.
    ``audit_head_sha256`` is omitted (not nulled) when absent so stripping
    the opt-in audit block from a receipt that was signed with it changes
    the binding bytes and collapses verification.
    """
    block: dict[str, Any] = {
        "journal_event_count": journal_count,
        "journal_head": journal_head,
        "run_id": run_id,
        "spine_entry_count": spine_count,
        "spine_head": spine_head,
    }
    if audit_head_sha256 is not None:
        block["audit_range_head_sha256"] = audit_head_sha256
    return block


def _signature_preimage(binding_bytes: bytes) -> bytes:
    """DSSE PAE over the binding bytes with the run-receipt payload type."""
    from bernstein.core.security.audit_dsse import pae

    return pae(RUN_RECEIPT_PAYLOAD_TYPE, binding_bytes)


def _project_journal_row(row: dict[str, Any]) -> dict[str, Any]:
    """Return the embeddable projection of one on-disk journal row.

    Drops only the wall-clock envelope. The chain fields (``index``,
    ``prev_hash``, ``payload_hash``, ``event_hash``) are deterministic
    functions of the decision payload and stay on the row so the verifier
    can run the exact :func:`~bernstein.core.replay.journal.verify_events`
    walk and name the first divergent step.
    """
    return {k: v for k, v in row.items() if k not in _WALL_CLOCK_FIELDS}


def _strict_jsonl_rows(
    path: Path,
    *,
    run_id: str,
    substrate: str,
    refusal: str,
) -> list[tuple[int, dict[str, Any]]]:
    """One strict JSONL scan for every signing substrate.

    The shared :func:`~bernstein.core.replay.journal.load_events` is
    deliberately tolerant - a partial trailing write must not wedge replay
    or gateway readers - but a *signer* must never survive corruption: a
    corrupted middle row surfaces downstream as a chain break, while a
    corrupted **trailing** row would silently vanish and the surviving
    prefix (which chains cleanly from genesis) would be signed as a
    shorter record. Any non-blank line that does not parse to a JSON
    object refuses the whole build, naming the substrate and the physical
    line. Journal and spine share this single implementation so parsing
    and error naming cannot drift between them; substrate-specific field
    validation stays with the caller, which is why rows come back paired
    with their physical line numbers.
    """
    rows: list[tuple[int, dict[str, Any]]] = []
    if not path.exists():
        return rows
    with path.open(encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RunReceiptError(
                    f"refusing to sign run {run_id!r}: {substrate} line {line_no} is not valid JSON "
                    f"({exc.msg}); {refusal}",
                ) from exc
            if not isinstance(row, dict):
                raise RunReceiptError(
                    f"refusing to sign run {run_id!r}: {substrate} line {line_no} is not a JSON object; {refusal}",
                )
            rows.append((line_no, row))
    return rows


def _load_journal_rows_strict(journal_path: Path, run_id: str) -> list[dict[str, Any]]:
    """Strict line-by-line journal parse for the signing path.

    Beyond the shared object-shape scan, every row's chain fields are
    validated before anything is embedded: downstream hashing normalises
    with ``str()`` and drops absent fields from projections, so a row with
    a null ``event`` or a missing ``payload_hash`` would otherwise be
    signed verbatim as a non-canonical journal event instead of refusing.
    """
    pairs = _strict_jsonl_rows(
        journal_path,
        run_id=run_id,
        substrate="journal",
        refusal="a receipt is never signed over a partial journal",
    )
    for line_no, row in pairs:
        index = row.get("index")
        if not isinstance(index, int) or isinstance(index, bool):
            raise RunReceiptError(
                f"refusing to sign run {run_id!r}: journal line {line_no} has a missing or "
                "non-integer 'index'; a receipt is never signed over a malformed journal",
            )
        event = row.get("event")
        if not isinstance(event, str) or not event:
            raise RunReceiptError(
                f"refusing to sign run {run_id!r}: journal line {line_no} has a missing or "
                "non-string 'event'; a receipt is never signed over a malformed journal",
            )
        if not isinstance(row.get("prev_hash"), str):
            raise RunReceiptError(
                f"refusing to sign run {run_id!r}: journal line {line_no} has a missing or "
                "non-string 'prev_hash'; a receipt is never signed over a malformed journal",
            )
        for hash_field in ("payload_hash", "event_hash"):
            value = row.get(hash_field)
            if not isinstance(value, str) or not value:
                raise RunReceiptError(
                    f"refusing to sign run {run_id!r}: journal line {line_no} has a missing or "
                    f"empty {hash_field!r}; a receipt is never signed over a malformed journal",
                )
    return [row for _line_no, row in pairs]


def _spine_rows(sdd_dir: Path, run_id: str) -> list[dict[str, Any]]:
    """Strictly load the run's spine entries as embeddable bodies (no ``hmac``).

    The spine store wants an HMAC key at construction, but reading rows and
    recomputing ``entry_hash`` values never touches it - the entry hash is
    plain SHA-256 over caller-visible fields. The store is used only to
    derive the validated path; the rows are parsed here rather than through
    :meth:`~bernstein.core.lineage.spine.LineageSpine.iter_entries`, which
    tolerantly skips malformed or bad-shape rows - fine for projections,
    fail-open for a signer: a skipped **trailing** row would leave a
    cleanly-chaining prefix and the receipt would attest an incomplete
    spine. Every non-blank line must parse to a complete entry or the whole
    build is refused with the physical line named, so the parsed rows equal
    the stored rows by construction.
    """
    spine = LineageSpine(sdd_dir / "lineage", run_id=run_id, hmac_key=b"")
    spine_path = spine.spine_path
    rows: list[dict[str, Any]] = []
    pairs = _strict_jsonl_rows(
        spine_path,
        run_id=run_id,
        substrate="spine",
        refusal="a receipt is never signed over an incomplete spine",
    )
    for line_no, row in pairs:
        missing = [f for f in (*_SPINE_REQUIRED_FIELDS, "hmac") if f not in row]
        if missing:
            raise RunReceiptError(
                f"refusing to sign run {run_id!r}: spine line {line_no} is missing fields "
                f"{sorted(missing)}; a receipt is never signed over an incomplete spine",
            )
        try:
            body: dict[str, Any] = {
                "v": int(row["v"]),
                "prev_hash": str(row["prev_hash"]),
                "artifact_path": str(row["artifact_path"]),
                "content_hash": str(row["content_hash"]),
                "actor": str(row["actor"]),
                "step_id": str(row["step_id"]),
                "model": str(row["model"]),
                "timestamp": int(row["timestamp"]),
                "entry_hash": str(row["entry_hash"]),
            }
        except (TypeError, ValueError) as exc:
            raise RunReceiptError(
                f"refusing to sign run {run_id!r}: spine line {line_no} has an unusable field "
                f"type ({exc}); a receipt is never signed over an incomplete spine",
            ) from exc
        for optional in _SPINE_OPTIONAL_FIELDS:
            value = row.get(optional)
            if value is not None:
                body[optional] = value
        rows.append(body)
    return rows


def _walk_spine_rows(rows: list[dict[str, Any]]) -> tuple[str, int | None, str]:
    """Recompute the spine chain over embedded rows without any key.

    Returns ``(head, divergent_index, error)``. ``divergent_index`` is the
    0-based index of the first row whose recomputed ``entry_hash`` (or
    ``prev_hash`` link) diverges, with ``error`` naming the reason; both
    are ``None``/empty for an intact chain.
    """
    prev = _GENESIS
    for i, row in enumerate(rows):
        missing = [f for f in _SPINE_REQUIRED_FIELDS if f not in row]
        if missing:
            return prev, i, f"spine entry {i}: missing fields {sorted(missing)}"
        if str(row["prev_hash"]) != prev:
            return prev, i, f"spine entry {i}: prev_hash break"
        try:
            expected = compute_entry_hash(
                prev_hash=str(row["prev_hash"]),
                artifact_path=str(row["artifact_path"]),
                content_hash=str(row["content_hash"]),
                actor=str(row["actor"]),
                step_id=str(row["step_id"]),
                model=str(row["model"]),
                timestamp=int(row["timestamp"]),
                traceparent=row.get("traceparent"),
                tracestate=row.get("tracestate"),
                baggage=row.get("baggage"),
            )
        except (TypeError, ValueError):
            return prev, i, f"spine entry {i}: unhashable field types"
        if str(row["entry_hash"]) != expected:
            return prev, i, f"spine entry {i}: entry_hash mismatch"
        prev = str(row["entry_hash"])
    return prev, None, ""


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def build_run_receipt(
    run_id: str,
    sdd_dir: Path,
    kms_adapter: Any,
    *,
    include_audit_range: bool = False,
    audit_hmac_key: bytes | None = None,
    audit_since: str | None = None,
    audit_until: str | None = None,
    write: bool = True,
    output_path: Path | None = None,
) -> RunReceipt:
    """Build (and by default write) the signed receipt for one run.

    Args:
        run_id: The run to attest. Must name an existing, non-empty
            journal - a run with no recorded events has no identity to
            sign, so the build refuses rather than emitting a receipt that
            attests nothing.
        sdd_dir: The project ``.sdd`` directory.
        kms_adapter: Ed25519 signer implementing
            :class:`~bernstein.core.security.lineage_kms.KMSAdapter`. The
            same key material and determinism guarantees the audit receipt
            relies on - no new key kind or rotation cadence.
        include_audit_range: Opt-in: also embed a re-chained audit-chain
            slice. Requires ``audit_hmac_key`` plus ``audit_since`` /
            ``audit_until`` (the operator HMAC key is needed at *build*
            time to re-chain the slice, never at verify time).
        audit_hmac_key: Operator audit HMAC key (build-time only).
        audit_since: ISO-8601 inclusive lower bound of the audit window.
        audit_until: ISO-8601 exclusive upper bound of the audit window.
        write: When ``False``, build in-memory only.
        output_path: Override the on-disk destination (defaults to
            ``.sdd/runs/<run_id>/run-receipt.json``).

    Returns:
        A :class:`RunReceipt`.

    Raises:
        RunReceiptError: The journal is missing/empty, a journal or spine
            row is malformed (never sign over a parseable subset - the
            build refuses on the first bad line rather than signing the
            surviving rows as a shorter run), an embedded chain does not
            recompute (never sign a broken chain), or the opt-in audit
            range was requested without its inputs.
        JournalPathError: ``run_id`` is not a safe path segment.
    """
    journal_path = run_journal_path(sdd_dir, run_id)
    events = _load_journal_rows_strict(journal_path, run_id)
    if not events:
        raise RunReceiptError(
            f"no journal events for run {run_id!r} at {journal_path}; an empty run has no identity to attest",
        )
    journal_check = verify_events(events)
    if not journal_check.ok:
        raise RunReceiptError(
            f"refusing to sign run {run_id!r}: journal chain fails at step "
            f"{journal_check.divergent_index}: {'; '.join(journal_check.errors)}",
        )
    journal_rows = [_project_journal_row(row) for row in events]
    journal_head = str(events[-1].get("event_hash", ""))

    spine_rows = _spine_rows(sdd_dir, run_id)
    spine_head, spine_divergent, spine_error = _walk_spine_rows(spine_rows)
    if spine_divergent is not None:
        raise RunReceiptError(f"refusing to sign run {run_id!r}: {spine_error}")

    audit_block: dict[str, Any] | None = None
    audit_head: str | None = None
    if include_audit_range:
        if audit_hmac_key is None or audit_since is None or audit_until is None:
            raise RunReceiptError(
                "include_audit_range=True requires audit_hmac_key, audit_since, and audit_until",
            )
        from bernstein.core.security.audit_receipt import _read_range_slice

        rebuilt, head_hmac, head_sha256 = _read_range_slice(
            sdd_dir / "audit",
            since=audit_since,
            until=audit_until,
            key=audit_hmac_key,
        )
        audit_head = head_sha256
        audit_block = {
            "since": audit_since,
            "until": audit_until,
            "head_hmac": head_hmac,
            "head_sha256": head_sha256,
            "event_count": len(rebuilt),
            "events": rebuilt,
        }

    binding = _binding_block(
        run_id=run_id,
        journal_head=journal_head,
        journal_count=len(journal_rows),
        spine_head=spine_head,
        spine_count=len(spine_rows),
        audit_head_sha256=audit_head,
    )
    binding_bytes = _canonical_json_bytes(binding)
    subject_sha256 = hashlib.sha256(binding_bytes).hexdigest()
    signature = kms_adapter.sign(_signature_preimage(binding_bytes))
    jwk = kms_adapter.public_key_jwk()
    key_id = str(jwk.get("kid") or "run-receipt-key")

    receipt: dict[str, Any] = {
        "schema_version": RUN_RECEIPT_SCHEMA_VERSION,
        "receipt_type": RUN_RECEIPT_TYPE,
        "run_id": run_id,
        "subject": {
            "name": f"run-receipt-{run_id}",
            "digest": {"sha256": subject_sha256},
        },
        "journal": {
            "head_hash": journal_head,
            "event_count": len(journal_rows),
            "events": journal_rows,
        },
        "spine": {
            "head_hash": spine_head,
            "entry_count": len(spine_rows),
            "entries": spine_rows,
        },
        "signing": {
            "alg": "EdDSA",
            "key_id": key_id,
            "payload_type": RUN_RECEIPT_PAYLOAD_TYPE,
            "public_key_jwk": jwk,
            "signature_b64": base64.b64encode(signature).decode("ascii"),
        },
    }
    if audit_block is not None:
        receipt["audit_range"] = audit_block
    receipt_bytes = _canonical_json_bytes(receipt) + b"\n"

    receipt_path: Path | None = None
    if write:
        receipt_path = output_path or (journal_path.parent / RUN_RECEIPT_FILENAME)
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_bytes(receipt_bytes)
        logger.info(
            "Run receipt written run=%s journal_events=%d spine_entries=%d path=%s",
            run_id,
            len(journal_rows),
            len(spine_rows),
            receipt_path,
        )

    return RunReceipt(
        run_id=run_id,
        journal_head=journal_head,
        spine_head=spine_head,
        audit_head_sha256=audit_head,
        receipt=receipt,
        receipt_bytes=receipt_bytes,
        receipt_path=receipt_path,
    )


# ---------------------------------------------------------------------------
# Verify (offline: the receipt bytes are the only input)
# ---------------------------------------------------------------------------


def _malformed(reason: str, *, run_id: str = "") -> RunReceiptVerifyResult:
    return RunReceiptVerifyResult(ok=False, status="malformed", run_id=run_id, errors=[reason])


def _public_key_from_jwk(jwk: dict[str, Any]) -> Any:
    """Decode an OKP/Ed25519 JWK (RFC 8037) into an Ed25519 public key."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    if jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519":
        raise ValueError(f"expected kty=OKP, crv=Ed25519; got kty={jwk.get('kty')!r} crv={jwk.get('crv')!r}")
    x = jwk.get("x")
    if not isinstance(x, str):
        raise ValueError("JWK 'x' missing or not a string")
    raw = base64.urlsafe_b64decode(x + "=" * (-len(x) % 4))
    if len(raw) != 32:
        raise ValueError(f"Ed25519 public key must be 32 bytes (got {len(raw)})")
    return Ed25519PublicKey.from_public_bytes(raw)


def verify_run_receipt(
    receipt_bytes: bytes,
    *,
    public_key_pem: bytes | None = None,
) -> RunReceiptVerifyResult:
    """Verify a run receipt using only its own bytes (and an optional pin).

    No HMAC key, no ``.sdd/``: every head is recomputed from the embedded
    ranges, the signed subject is rebuilt from the recomputed heads, and
    the Ed25519 signature is checked against the embedded JWK (which must
    match ``public_key_pem`` when a pin is supplied).

    Trust model: **what a pass proves depends on where the key came
    from.** Without a pin the signature is checked against the key
    embedded in the receipt itself (trust-on-first-use), so a pass is
    *integrity-only* - the file is internally consistent and any
    post-signing mutation is caught at a precise step, but an attacker
    who controls the whole file could have swapped in their own key and
    re-signed, so the embedded key cannot establish *who* produced the
    receipt. Provenance - the receipt was signed by a specific operator's
    key - requires supplying that key out-of-band via ``public_key_pem``;
    the embedded key must then match it.

    Args:
        receipt_bytes: The receipt file contents.
        public_key_pem: Optional PEM Ed25519 public key to pin. The
            embedded JWK must encode the same key, otherwise the receipt
            is rejected even when its content recomputes cleanly.

    Returns:
        A :class:`RunReceiptVerifyResult`. ``status`` is ``"malformed"``
        for unparseable or structurally incomplete input and
        ``"tampered"`` for any recompute or signature divergence - with
        the first divergent journal step index named when journal tamper
        was located.
    """
    try:
        receipt = json.loads(receipt_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _malformed(f"receipt is not valid JSON: {exc}")
    if not isinstance(receipt, dict):
        return _malformed("receipt is not a JSON object")

    run_id = str(receipt.get("run_id", ""))
    if not run_id:
        return _malformed("receipt.run_id missing")
    if receipt.get("receipt_type") != RUN_RECEIPT_TYPE:
        return _malformed(f"unexpected receipt_type {receipt.get('receipt_type')!r}", run_id=run_id)

    journal_block = receipt.get("journal")
    spine_block = receipt.get("spine")
    signing = receipt.get("signing")
    if not isinstance(journal_block, dict) or not isinstance(journal_block.get("events"), list):
        return _malformed("receipt.journal.events missing or not a list", run_id=run_id)
    if not isinstance(spine_block, dict) or not isinstance(spine_block.get("entries"), list):
        return _malformed("receipt.spine.entries missing or not a list", run_id=run_id)
    if not isinstance(signing, dict):
        return _malformed("receipt.signing missing", run_id=run_id)
    if signing.get("payload_type") != RUN_RECEIPT_PAYLOAD_TYPE:
        return _malformed(f"unexpected signing.payload_type {signing.get('payload_type')!r}", run_id=run_id)

    events_any: list[Any] = journal_block["events"]
    if not all(isinstance(e, dict) for e in events_any):
        return _malformed("receipt.journal.events contains a non-object row", run_id=run_id)
    events: list[dict[str, Any]] = list(events_any)
    if not events:
        return _malformed("receipt embeds no journal events; an empty range attests nothing", run_id=run_id)
    entries_any: list[Any] = spine_block["entries"]
    if not all(isinstance(e, dict) for e in entries_any):
        return _malformed("receipt.spine.entries contains a non-object row", run_id=run_id)
    entries: list[dict[str, Any]] = list(entries_any)

    def _tampered(errors: list[str], divergent_step: int | None = None) -> RunReceiptVerifyResult:
        return RunReceiptVerifyResult(
            ok=False,
            status="tampered",
            run_id=run_id,
            journal_events=len(events),
            spine_entries=len(entries),
            divergent_step=divergent_step,
            errors=errors,
        )

    # 1. Journal: the exact verify_journal walk over the embedded rows.
    journal_result = verify_events(events)
    if not journal_result.ok:
        step = journal_result.divergent_index
        return _tampered(
            [f"journal diverges at step {step}: {'; '.join(journal_result.errors)}"],
            divergent_step=step,
        )
    recomputed_journal_head = str(events[-1].get("event_hash", ""))
    if str(journal_block.get("head_hash", "")) != recomputed_journal_head:
        return _tampered(["journal.head_hash does not match the head recomputed from the embedded rows"])
    if journal_block.get("event_count") != len(events):
        return _tampered(["journal.event_count does not match the embedded rows"])

    # 2. Spine: keyless recompute of every entry hash and the chain links.
    recomputed_spine_head, spine_divergent, spine_error = _walk_spine_rows(entries)
    if spine_divergent is not None:
        return _tampered([spine_error])
    if str(spine_block.get("head_hash", "")) != recomputed_spine_head:
        return _tampered(["spine.head_hash does not match the head recomputed from the embedded entries"])
    if spine_block.get("entry_count") != len(entries):
        return _tampered(["spine.entry_count does not match the embedded entries"])

    # 3. Optional audit range: head recomputes from the embedded events.
    audit_head: str | None = None
    audit_block = receipt.get("audit_range")
    if audit_block is not None:
        if not isinstance(audit_block, dict) or not isinstance(audit_block.get("events"), list):
            return _malformed("receipt.audit_range.events missing or not a list", run_id=run_id)
        from bernstein.core.security.audit_multitenant import _events_jsonl_bytes

        audit_events: list[dict[str, Any]] = list(audit_block["events"])
        recomputed_audit_head = hashlib.sha256(_events_jsonl_bytes(audit_events)).hexdigest()
        if str(audit_block.get("head_sha256", "")) != recomputed_audit_head:
            return _tampered(["audit_range.head_sha256 does not match the head recomputed from the embedded events"])
        if audit_block.get("event_count") != len(audit_events):
            return _tampered(["audit_range.event_count does not match the embedded events"])
        audit_head = recomputed_audit_head

    # 4. Subject binding: rebuilt from recomputed values only.
    binding = _binding_block(
        run_id=run_id,
        journal_head=recomputed_journal_head,
        journal_count=len(events),
        spine_head=recomputed_spine_head,
        spine_count=len(entries),
        audit_head_sha256=audit_head,
    )
    binding_bytes = _canonical_json_bytes(binding)
    recomputed_subject = hashlib.sha256(binding_bytes).hexdigest()
    stated_subject = str(((receipt.get("subject") or {}).get("digest") or {}).get("sha256", ""))
    if stated_subject != recomputed_subject:
        return _tampered(
            [
                f"signed subject {stated_subject[:16]}... does not match the digest "
                f"{recomputed_subject[:16]}... recomputed from the embedded ranges"
            ],
        )

    # 5. Signature: embedded JWK (pin-checked when supplied), DSSE PAE.
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    jwk = signing.get("public_key_jwk")
    if not isinstance(jwk, dict):
        return _malformed("receipt.signing.public_key_jwk missing or not an object", run_id=run_id)
    try:
        public_key = _public_key_from_jwk(jwk)
    except ValueError as exc:
        return _malformed(f"embedded JWK is not a usable Ed25519 key: {exc}", run_id=run_id)

    if public_key_pem is not None:
        try:
            pinned = serialization.load_pem_public_key(public_key_pem)
        except (ValueError, TypeError) as exc:
            return _malformed(f"pinned public key is not valid PEM: {exc}", run_id=run_id)
        if not isinstance(pinned, Ed25519PublicKey):
            return _malformed(
                f"pinned public key is not Ed25519 (got {type(pinned).__name__})",
                run_id=run_id,
            )
        raw_pin = pinned.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        raw_emb = public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        if raw_pin != raw_emb:
            return _tampered(["embedded receipt key does not match the pinned public key"])

    sig_b64 = signing.get("signature_b64")
    if not isinstance(sig_b64, str):
        return _malformed("receipt.signing.signature_b64 missing", run_id=run_id)
    try:
        signature = base64.b64decode(sig_b64, validate=True)
    except (ValueError, TypeError):
        return _malformed("receipt.signing.signature_b64 is not valid base64", run_id=run_id)
    try:
        public_key.verify(signature, _signature_preimage(binding_bytes))
    except InvalidSignature:
        return _tampered(["Ed25519 signature does not verify over the recomputed subject binding"])

    return RunReceiptVerifyResult(
        ok=True,
        status="ok",
        run_id=run_id,
        journal_events=len(events),
        spine_entries=len(entries),
    )


# ---------------------------------------------------------------------------
# Finalization hook helper
# ---------------------------------------------------------------------------


def resolve_kms_adapter_from_env() -> Any | None:
    """Resolve the run-receipt signing adapter from the environment.

    Mirrors the ``lineage.kms_adapter`` config dispatch
    (:func:`~bernstein.core.security.lineage_kms.kms_adapter_from_config`)
    with env-var inputs, because the finalization hook runs inside the
    orchestrator where no receipt-specific CLI flags exist. Returns
    ``None`` when neither source is configured - the documented
    "no signing key, no receipt" degradation, matching the audit-receipt
    posture that a signed receipt is only emitted under an explicit
    operator key.
    """
    from bernstein.core.security.lineage_kms import kms_adapter_from_config

    key_path = os.environ.get(SIGNING_KEY_PATH_ENV, "").strip()
    env_var = os.environ.get(SIGNING_ENV_VAR_ENV, "").strip()
    kid = os.environ.get(SIGNING_KID_ENV, "").strip() or None
    if key_path:
        return kms_adapter_from_config(enabled=True, kind="file", key_path=key_path, kid=kid)
    if env_var:
        return kms_adapter_from_config(enabled=True, kind="env", env_var=env_var, kid=kid)
    return None


def write_run_receipt_if_configured(run_id: str, sdd_dir: Path) -> Path | None:
    """Write the run receipt at finalization when a signing key is configured.

    The no-key case is a documented no-op (debug log, ``None`` return),
    never an error: receipts must not silently over-promise, and an
    unsigned "receipt" would be exactly that. Real build failures
    propagate so the caller's log-never-raise wrapper reports them.

    Returns:
        The written receipt path, or ``None`` when signing is not
        configured.
    """
    kms_adapter = resolve_kms_adapter_from_env()
    if kms_adapter is None:
        logger.debug(
            "run receipt not written for run %s: no signing key configured (set %s or %s)",
            run_id,
            SIGNING_KEY_PATH_ENV,
            SIGNING_ENV_VAR_ENV,
        )
        return None
    return build_run_receipt(run_id, sdd_dir, kms_adapter).receipt_path


__all__ = [
    "RUN_RECEIPT_FILENAME",
    "RUN_RECEIPT_PAYLOAD_TYPE",
    "RUN_RECEIPT_SCHEMA_VERSION",
    "RUN_RECEIPT_TYPE",
    "SIGNING_ENV_VAR_ENV",
    "SIGNING_KEY_PATH_ENV",
    "SIGNING_KID_ENV",
    "RunReceipt",
    "RunReceiptError",
    "RunReceiptVerifyResult",
    "build_run_receipt",
    "resolve_kms_adapter_from_env",
    "verify_run_receipt",
    "write_run_receipt_if_configured",
]
