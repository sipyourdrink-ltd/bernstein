"""Default verifier kinds wired into the ``bernstein verify`` dispatcher (#5103).

Slice 1 of #5103 wires enough kinds to prove the registry-backed dispatch
shape works, not all 55 groups. The two wired here were chosen because they
are the artefact-path verifiers whose entire input is one portable file with
no other required state (no ``--workdir``, no ``.sdd/`` lookup by id): an
AI-BOM document (:mod:`bernstein.core.compliance.ai_bom`) and a result
receipt bundle (:mod:`bernstein.core.security.result_receipt_bundle`).

Most of the other ~53 ``verify`` commands surveyed for this issue take an id
(a run id, task id, or event id) and re-derive state from ``.sdd/`` rather
than checking a portable artefact file -- a materially different calling
convention that a single ``bernstein verify <artefact-path>`` entry point
cannot dispatch to as-is. That is left as an open question for whichever
later slice migrates them (see the PR body for #5103).
"""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING, Any, cast

from bernstein.core.verify_dispatch import VerifierSpec, VerifyOutcome, register_verifier

if TYPE_CHECKING:
    from pathlib import Path

_registered = False


def register_default_verifiers() -> None:
    """Register the kinds this slice wires. Idempotent -- safe to call repeatedly."""
    global _registered
    if _registered:
        return
    register_verifier(VerifierSpec(kind="bom", sniff=_sniff_bom, verify=_verify_bom))
    register_verifier(VerifierSpec(kind="receipt-bundle", sniff=_sniff_receipt_bundle, verify=_verify_receipt_bundle))
    _registered = True


# ---------------------------------------------------------------------------
# bom -- bernstein.core.compliance.ai_bom
# ---------------------------------------------------------------------------


def _sniff_bom(path: Path, payload: dict[str, Any] | None) -> bool:
    from bernstein.core.compliance.ai_bom import BOM_SCHEMA_URL

    return isinstance(payload, dict) and payload.get("schema") == BOM_SCHEMA_URL


def _verify_bom(path: Path) -> VerifyOutcome:
    from bernstein.core.compliance.ai_bom import verify_bom

    report = verify_bom(path.read_bytes())
    if report.ok:
        return VerifyOutcome(
            kind="bom",
            ok=True,
            exit_code=0,
            message=f"checked {report.checked_count} element(s)",
            detail={"checked_count": report.checked_count},
        )
    return VerifyOutcome(
        kind="bom",
        ok=False,
        exit_code=1,
        message=f"{len(report.errors)} error(s); checked {report.checked_count} element(s)",
        detail={"checked_count": report.checked_count, "errors": list(report.errors)},
    )


# ---------------------------------------------------------------------------
# receipt-bundle -- bernstein.core.security.result_receipt_bundle
# ---------------------------------------------------------------------------


def _sniff_receipt_bundle(path: Path, payload: dict[str, Any] | None) -> bool:
    """A DSSE envelope whose embedded in-toto statement is a result-receipt.

    The predicate type lives inside the base64-encoded ``payload`` field, not
    at the envelope's top level, so this decodes one layer to read it --
    mirroring what :func:`~bernstein.core.security.result_receipt_bundle.load_bundle`
    does, without running full signature verification just to detect the kind.
    """
    from bernstein.core.security.result_receipt_bundle import RESULT_RECEIPT_PREDICATE_TYPE

    if not isinstance(payload, dict):
        return False
    raw_payload = payload.get("payload")
    if not isinstance(raw_payload, str):
        return False
    try:
        decoded: Any = json.loads(base64.b64decode(raw_payload))
    except (ValueError, TypeError):
        return False
    if not isinstance(decoded, dict):
        return False
    statement = cast("dict[str, Any]", decoded)
    return statement.get("predicateType") == RESULT_RECEIPT_PREDICATE_TYPE


def _verify_receipt_bundle(path: Path) -> VerifyOutcome:
    """Verify against the bundle's own embedded worker key (trust on first use).

    Matches ``bernstein receipt verify <path>`` invoked with no ``--pubkey``:
    the dispatcher has no channel for a pinned key, so provenance (a pinned
    key) is out of reach here and only the integrity tier is offered.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    from bernstein.core.security.audit_dsse import EnvelopeFormatError
    from bernstein.core.security.result_receipt_bundle import load_bundle, verify_result_bundle

    try:
        envelope = load_bundle(path)
    except (OSError, ValueError, json.JSONDecodeError, EnvelopeFormatError) as exc:
        return VerifyOutcome(kind="receipt-bundle", ok=False, exit_code=1, message=f"could not parse bundle: {exc}")

    try:
        payload = json.loads(envelope.payload_bytes)
        pem = payload["predicate"]["bundle"]["worker"]["public_key_pem"].encode("ascii")
        public_key = serialization.load_pem_public_key(pem)
    except (KeyError, ValueError) as exc:
        return VerifyOutcome(
            kind="receipt-bundle",
            ok=False,
            exit_code=1,
            message=f"bundle carries no usable worker public key: {exc}",
        )
    if not isinstance(public_key, Ed25519PublicKey):
        return VerifyOutcome(
            kind="receipt-bundle", ok=False, exit_code=1, message="embedded key is not an Ed25519 public key"
        )

    result = verify_result_bundle(envelope, public_key)
    if result.ok:
        return VerifyOutcome(
            kind="receipt-bundle",
            ok=True,
            exit_code=0,
            message=f"verifies against embedded key (trust on first use); digest {result.digest}",
            detail={"keyid": result.keyid, "digest": result.digest, "pinned_key": False},
        )
    return VerifyOutcome(
        kind="receipt-bundle",
        ok=False,
        exit_code=1,
        message=f"{len(result.errors)} error(s)",
        detail={"errors": [f"{e.field}: {e.message}" for e in result.errors]},
    )


__all__ = ["register_default_verifiers"]
