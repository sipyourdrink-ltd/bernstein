"""Signed, content-addressed scorecard artifact binding a derived document to its source journal.

Mirrors the signing and binding conventions of :mod:`bernstein.core.replay.run_receipt`
so the scorecard and the run receipt share one DSSE/Ed25519 envelope family.

Wire format
-----------
The signed subject is the canonical JSON of a binding block containing:
``run_id``, ``document_digest`` (SHA-256 of the canonical projected document body,
excluding non-deterministic wall-clock fields), ``journal_head`` (recomputed from
embedded journal rows), and ``journal_event_count``. The verifier rebuilds the
binding block from recomputed values only; it never trusts an asserted head.

Journal binding: the journal rows the scorecard was derived from are embedded,
the head is recomputed via :func:`~bernstein.core.replay.journal.verify_events`,
and the first divergent step is named on tamper.

Non-reproducible fields (e.g. ``generated_at``) live on the document body but are
EXCLUDED from the hashed binding subject via :func:`_project_document_body`.

Determinism: for a fixed ``sdd_dir``, document body, and signing key, bytes are
byte-identical across independent builds (RFC 8032 deterministic Ed25519 + canonical JSON).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.core.replay.journal import (
    run_journal_path,
    verify_events,
)
from bernstein.core.security.audit_dsse import pae

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.security.lineage_kms import KMSAdapter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Wire-format identifiers
# ---------------------------------------------------------------------------

SCORECARD_SCHEMA_VERSION: str = "1.0.0"

SCORECARD_TYPE: str = "https://bernstein.run/attestations/scorecard/v1"

SCORECARD_PAYLOAD_TYPE: str = "application/vnd.bernstein.scorecard+json"

SCORECARD_FILENAME: str = "scorecard.json"

SIGNING_KEY_PATH_ENV: str = "BERNSTEIN_SCORECARD_SIGNING_KEY_PATH"
SIGNING_ENV_VAR_ENV: str = "BERNSTEIN_SCORECARD_SIGNING_ENV_VAR"
SIGNING_KID_ENV: str = "BERNSTEIN_SCORECARD_SIGNING_KID"

#: Wall-clock fields that appear on the document body but are excluded from
#: the hashed binding subject because they are non-reproducible.
_WALL_CLOCK_FIELDS: frozenset[str] = frozenset({"generated_at"})


class ScorecardArtifactError(RuntimeError):
    """Raised when a scorecard artifact cannot be built or verified."""


# ---------------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScorecardDocument:
    """Document body to attest.

    Attributes:
        run_id: The run this scorecard was derived from.
        document_version: Scorecard schema version.
        scorecard: The scorecard body fields (arbitrary dict, type-defined
            by the caller).
        generated_at: Wall-clock timestamp of generation (present on the
            body, excluded from the hashed binding subject).
    """

    run_id: str
    document_version: str
    scorecard: dict[str, Any]
    generated_at: str | None = field(default=None)


@dataclass(frozen=True, slots=True)
class ScorecardArtifact:
    """Built scorecard artifact.

    Attributes:
        run_id: The attested run.
        journal_head: Recomputed journal Merkle head.
        document_digest: SHA-256 of the projected (wall-clock-excluded) document body.
        artifact: The serialisable artifact dict.
        artifact_bytes: Canonical JSON bytes (byte-deterministic).
        artifact_path: On-disk path when written, else ``None``.
    """

    run_id: str
    journal_head: str
    document_digest: str
    artifact: dict[str, Any]
    artifact_bytes: bytes
    artifact_path: Path | None = field(default=None)


@dataclass(frozen=True, slots=True)
class ScorecardVerifyResult:
    """Outcome of :func:`verify_scorecard`.

    Attributes:
        ok: ``True`` only when every recompute and the signature pass.
        status: ``"ok"``, ``"malformed"``, ``"tampered"``, or ``"untrusted_key"``.
        run_id: Run id claimed by the scorecard.
        journal_events: Number of embedded journal rows walked.
        divergent_step: 0-based index of the first divergent journal step,
            when journal tamper was located.
        errors: Human-readable explanations, first failure first.
    """

    ok: bool
    status: str
    run_id: str = ""
    journal_events: int = 0
    divergent_step: int | None = None
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Canonical helpers
# ---------------------------------------------------------------------------


def _canonical_json_bytes(obj: dict[str, Any]) -> bytes:
    """Deterministic JSON bytes — shared with the audit receipt family."""
    from bernstein.core.security.audit_receipt import _canonical_json_bytes as _cjb

    return _cjb(obj)


def _project_document_body(document_dict: dict[str, Any]) -> dict[str, Any]:
    """Return the document body with wall-clock fields excluded.

    ``generated_at`` and other non-reproducible wall-clock fields are
    present on the document body but MUST NOT enter the hashed binding
    subject, otherwise two builds of the same scorecard over identical
    journal content would produce different signed subjects and the
    signature would not be reproducible.
    """
    return {k: v for k, v in document_dict.items() if k not in _WALL_CLOCK_FIELDS}


def _load_journal_rows_strict(journal_path: Path, run_id: str) -> list[dict[str, Any]]:
    """Strict line-by-line parse mirroring run_receipt.py signing path."""
    rows: list[dict[str, Any]] = []
    if not journal_path.exists():
        return rows
    with journal_path.open(encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ScorecardArtifactError(
                    f"refusing to sign scorecard for run {run_id!r}: journal line {line_no} is not valid JSON "
                    f"({exc.msg}); a scorecard is never signed over a malformed journal",
                ) from exc
            if not isinstance(row, dict):
                raise ScorecardArtifactError(
                    f"refusing to sign scorecard for run {run_id!r}: journal line {line_no} is not a JSON object; "
                    "a scorecard is never signed over a malformed journal",
                )
            rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def build_scorecard(
    run_id: str,
    sdd_dir: Path,
    kms_adapter: KMSAdapter,
    document: ScorecardDocument,
    *,
    write: bool = True,
    output_path: Path | None = None,
) -> ScorecardArtifact:
    """Build (and by default write) the signed scorecard for one run.

    Args:
        run_id: The run whose journal anchors this scorecard.
        sdd_dir: The project ``.sdd`` directory.
        kms_adapter: Ed25519 signer implementing
            :class:`~bernstein.core.security.lineage_kms.KMSAdapter`.
        document: The scorecard document body to attest.
        write: When ``False``, build in-memory only.
        output_path: Override the on-disk destination (defaults to
            ``.sdd/runs/<run_id>/scorecard.json``).

    Returns:
        A :class:`ScorecardArtifact`.

    Raises:
        ScorecardArtifactError: The journal is missing/empty or malformed.
    """
    journal_path = run_journal_path(sdd_dir, run_id)
    events = _load_journal_rows_strict(journal_path, run_id)
    if not events:
        raise ScorecardArtifactError(
            f"no journal events for run {run_id!r} at {journal_path}; a scorecard requires a non-empty journal",
        )

    journal_check = verify_events(events)
    if not journal_check.chain_consistent:
        raise ScorecardArtifactError(
            f"refusing to sign scorecard for run {run_id!r}: journal chain fails at step "
            f"{journal_check.divergent_index}: {'; '.join(journal_check.errors)}",
        )

    # Project journal rows (drop wall-clock envelope).
    projected_row_fields = frozenset({"ts", "elapsed_s"})
    journal_rows = [{k: v for k, v in row.items() if k not in projected_row_fields} for row in events]
    journal_head = str(events[-1].get("event_hash", ""))

    # Project document body (drop wall-clock fields for the digest).
    doc_body = {
        "run_id": document.run_id,
        "document_version": document.document_version,
        "scorecard": document.scorecard,
    }
    if document.generated_at is not None:
        doc_body["generated_at"] = document.generated_at
    projected_doc = _project_document_body(doc_body)
    doc_digest = hashlib.sha256(_canonical_json_bytes(projected_doc)).hexdigest()

    # Build the signed subject binding.
    binding: dict[str, Any] = {
        "run_id": run_id,
        "document_digest": doc_digest,
        "journal_head": journal_head,
        "journal_event_count": len(journal_rows),
    }
    binding_bytes = _canonical_json_bytes(binding)

    # Sign over DSSE PAE.
    signature = kms_adapter.sign(pae(SCORECARD_PAYLOAD_TYPE, binding_bytes))
    jwk = kms_adapter.public_key_jwk()
    key_id = str(jwk.get("kid") or "scorecard-key")

    # Assemble artifact.
    artifact: dict[str, Any] = {
        "schema_version": SCORECARD_SCHEMA_VERSION,
        "scorecard_type": SCORECARD_TYPE,
        "run_id": run_id,
        "subject": {
            "name": f"scorecard-{run_id}",
            "digest": {"sha256": doc_digest},
        },
        "document": doc_body,
        "journal": {
            "head_hash": journal_head,
            "event_count": len(journal_rows),
            "events": journal_rows,
        },
        "signing": {
            "alg": "EdDSA",
            "key_id": key_id,
            "payload_type": SCORECARD_PAYLOAD_TYPE,
            "public_key_jwk": jwk,
            "signature_b64": base64.b64encode(signature).decode("ascii"),
        },
    }
    artifact_bytes = _canonical_json_bytes(artifact) + b"\n"

    artifact_path: Path | None = None
    if write:
        artifact_path = output_path or (journal_path.parent / SCORECARD_FILENAME)
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_bytes(artifact_bytes)
        logger.info(
            "Scorecard written run=%s journal_events=%d path=%s",
            run_id,
            len(journal_rows),
            artifact_path,
        )

    return ScorecardArtifact(
        run_id=run_id,
        journal_head=journal_head,
        document_digest=doc_digest,
        artifact=artifact,
        artifact_bytes=artifact_bytes,
        artifact_path=artifact_path,
    )


# ---------------------------------------------------------------------------
# Verify (offline: the artifact bytes are the only input)
# ---------------------------------------------------------------------------


def _malformed(reason: str, *, run_id: str = "") -> ScorecardVerifyResult:
    return ScorecardVerifyResult(ok=False, status="malformed", run_id=run_id, errors=[reason])


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


def verify_scorecard(
    artifact_bytes: bytes,
    *,
    public_key_pem: bytes | None = None,
    key_chain_bytes: bytes | None = None,
    attested_signed_at: str | None = None,
) -> ScorecardVerifyResult:
    """Verify a scorecard artifact using only its own bytes (and an optional pin).

    No HMAC key, no ``.sdd/``: the journal head is recomputed from the embedded
    rows, the document digest is recomputed from the projected body, the signed
    subject is rebuilt from the recomputed values, and the Ed25519 signature is
    checked against the embedded JWK.

    Trust model mirrors :func:`~bernstein.core.replay.run_receipt.verify_run_receipt`:
    without a pin the signature is integrity-only; provenance requires supplying
    ``public_key_pem`` out of band.

    Args:
        artifact_bytes: The scorecard file contents.
        public_key_pem: Optional PEM Ed25519 public key to pin.
        key_chain_bytes: Optional signed key-succession chain document.
        attested_signed_at: Optional ISO-8601 instant for key lifecycle decisions.

    Returns:
        A :class:`ScorecardVerifyResult`.
    """
    try:
        artifact = json.loads(artifact_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _malformed(f"scorecard is not valid JSON: {exc}")

    if not isinstance(artifact, dict):
        return _malformed("scorecard is not a JSON object")

    run_id = str(artifact.get("run_id", ""))
    if not run_id:
        return _malformed("scorecard.run_id missing")
    if artifact.get("scorecard_type") != SCORECARD_TYPE:
        return _malformed(f"unexpected scorecard_type {artifact.get('scorecard_type')!r}", run_id=run_id)

    journal_block = artifact.get("journal")
    signing = artifact.get("signing")
    document = artifact.get("document")
    if not isinstance(journal_block, dict) or not isinstance(journal_block.get("events"), list):
        return _malformed("scorecard.journal.events missing or not a list", run_id=run_id)
    if not isinstance(signing, dict):
        return _malformed("scorecard.signing missing", run_id=run_id)
    if signing.get("payload_type") != SCORECARD_PAYLOAD_TYPE:
        return _malformed(f"unexpected signing.payload_type {signing.get('payload_type')!r}", run_id=run_id)
    if not isinstance(document, dict):
        return _malformed("scorecard.document missing or not an object", run_id=run_id)

    events_any: list[Any] = journal_block["events"]
    if not all(isinstance(e, dict) for e in events_any):
        return _malformed("scorecard.journal.events contains a non-object row", run_id=run_id)
    events: list[dict[str, Any]] = list(events_any)
    if not events:
        return _malformed("scorecard embeds no journal events; an empty range attests nothing", run_id=run_id)

    def _tampered(errors: list[str], divergent_step: int | None = None) -> ScorecardVerifyResult:
        return ScorecardVerifyResult(
            ok=False,
            status="tampered",
            run_id=run_id,
            journal_events=len(events),
            divergent_step=divergent_step,
            errors=errors,
        )

    # 1. Journal: recompute head via verify_events.
    journal_result = verify_events(events)
    if not journal_result.chain_consistent or journal_result.discarded_line_indices:
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

    # 2. Document digest: recompute from projected body.
    projected_doc = _project_document_body(document)
    recomputed_doc_digest = hashlib.sha256(_canonical_json_bytes(projected_doc)).hexdigest()
    stated_doc_digest = str(((artifact.get("subject") or {}).get("digest") or {}).get("sha256", ""))
    if stated_doc_digest != recomputed_doc_digest:
        return _tampered(
            [
                f"document digest {stated_doc_digest[:16]}... does not match "
                f"{recomputed_doc_digest[:16]}... recomputed from the projected body",
            ],
        )

    # 3. Subject binding: rebuilt from recomputed values only.
    binding: dict[str, Any] = {
        "run_id": run_id,
        "document_digest": recomputed_doc_digest,
        "journal_head": recomputed_journal_head,
        "journal_event_count": len(events),
    }
    binding_bytes = _canonical_json_bytes(binding)

    # 4. Signature: embedded JWK, DSSE PAE.
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    jwk = signing.get("public_key_jwk")
    if not isinstance(jwk, dict):
        return _malformed("scorecard.signing.public_key_jwk missing or not an object", run_id=run_id)

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
            return _tampered(["embedded scorecard key does not match the pinned public key"])

    sig_b64 = signing.get("signature_b64")
    if not isinstance(sig_b64, str):
        return _malformed("scorecard.signing.signature_b64 missing", run_id=run_id)
    try:
        signature = base64.b64decode(sig_b64, validate=True)
    except (ValueError, TypeError):
        return _malformed("scorecard.signing.signature_b64 is not valid base64", run_id=run_id)

    try:
        public_key.verify(signature, pae(SCORECARD_PAYLOAD_TYPE, binding_bytes))
    except InvalidSignature:
        return _tampered(["Ed25519 signature does not verify over the recomputed subject binding"])

    # 5. Key lifecycle (mirrors run_receipt).
    if key_chain_bytes is not None and public_key_pem is None:
        return _malformed(
            "a key chain needs public_key_pem: the chain is only evidence relative to the "
            "root key the auditor pinned out of band",
            run_id=run_id,
        )

    if key_chain_bytes is not None and public_key_pem is not None:
        from bernstein.core.security.receipt_key_chain import (
            KeyChainError,
            resolve_signing_key,
            verify_key_chain,
        )

        try:
            chain = verify_key_chain(key_chain_bytes, root_public_key_pem=public_key_pem)
            trust = resolve_signing_key(
                chain,
                kid=str(signing.get("key_id", "")),
                public_key_raw=public_key.public_bytes(
                    serialization.Encoding.Raw,
                    serialization.PublicFormat.Raw,
                ),
                attested_signed_at=attested_signed_at,
            )
        except KeyChainError as exc:
            return ScorecardVerifyResult(
                ok=False,
                status="untrusted_key",
                run_id=run_id,
                journal_events=len(events),
                errors=[str(exc)],
            )
        if not trust.trusted:
            return ScorecardVerifyResult(
                ok=False,
                status="untrusted_key",
                run_id=run_id,
                journal_events=len(events),
                errors=[trust.detail],
            )
        _ = trust  # resolution succeeded; verdict consumed by `untrusted_key` return above

    return ScorecardVerifyResult(
        ok=True,
        status="ok",
        run_id=run_id,
        journal_events=len(events),
    )


# ---------------------------------------------------------------------------
# Finalization hook helper
# ---------------------------------------------------------------------------


def resolve_kms_adapter_from_env() -> KMSAdapter | None:
    """Resolve the scorecard signing adapter from the environment."""
    from bernstein.core.security.lineage_kms import kms_adapter_from_config

    key_path = os.environ.get(SIGNING_KEY_PATH_ENV, "").strip()
    env_var = os.environ.get(SIGNING_ENV_VAR_ENV, "").strip()
    kid = os.environ.get(SIGNING_KID_ENV, "").strip() or None
    if key_path:
        return kms_adapter_from_config(enabled=True, kind="file", key_path=key_path, kid=kid)
    if env_var:
        return kms_adapter_from_config(enabled=True, kind="env", env_var=env_var, kid=kid)
    return None


def write_scorecard_if_configured(
    run_id: str,
    sdd_dir: Path,
    document: ScorecardDocument,
) -> Path | None:
    """Write the scorecard at finalization when a signing key is configured.

    Returns:
        The written scorecard path, or ``None`` when signing is not configured.
    """
    kms_adapter = resolve_kms_adapter_from_env()
    if kms_adapter is None:
        logger.debug(
            "scorecard not written for run %s: no signing key configured (set %s or %s)",
            run_id,
            SIGNING_KEY_PATH_ENV,
            SIGNING_ENV_VAR_ENV,
        )
        return None
    return build_scorecard(run_id, sdd_dir, kms_adapter, document).artifact_path


__all__ = [
    "SCORECARD_FILENAME",
    "SCORECARD_PAYLOAD_TYPE",
    "SCORECARD_SCHEMA_VERSION",
    "SCORECARD_TYPE",
    "ScorecardArtifact",
    "ScorecardArtifactError",
    "ScorecardDocument",
    "ScorecardVerifyResult",
    "build_scorecard",
    "resolve_kms_adapter_from_env",
    "verify_scorecard",
    "write_scorecard_if_configured",
]
