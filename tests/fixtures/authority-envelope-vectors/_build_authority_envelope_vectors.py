#!/usr/bin/env python3
"""Re-mint the authority-envelope golden vectors in this directory (issue #5055).

Run by hand from a source checkout, never by the test suite::

    uv run python tests/fixtures/authority-envelope-vectors/_build_authority_envelope_vectors.py

It builds one deliberately partial envelope -- two authorization decisions, one
of which carries no evidence and is therefore declared in ``coverage.uncovered``
-- signs it with the deterministic Ed25519 key pinned below, and writes a second
copy with one decision mutated.

Why the vectors are committed rather than generated at test time
----------------------------------------------------------------
The vector is the target the standalone verifier is judged against. Minting it
inside the test would move both sides of the comparison at once: a verifier that
stopped checking ``inputs_hash`` and a builder that stopped computing it would
still agree. Committing the bytes means a change to the canonical form, the hash
preimages, or the JWS signing input fails CI instead of silently invalidating an
envelope already handed to an auditor.

Everything here is deterministic, so a re-mint with an unchanged format produces
byte-identical files: the timestamps are fixed constants and the signing key is
seeded. Re-mint only when the envelope format itself changes, and review the
diff as new evidence.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "src"))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# The production side of the canonicalisation. The standalone verifier
# re-implements RFC 8785 locally; the golden vector is what proves the two
# implementations agree.
from bernstein.core.security.agent_card_signer import _b64url, canonicalize_jcs

OUT_DIR = Path(__file__).resolve().parent

SCHEMA_VERSION = "1.0.0"
ENVELOPE_TYPE = "https://bernstein.run/attestations/authority-envelope/v1"
JWS_TYP = "application/vnd.bernstein.authority-envelope+jws"

_PRINCIPAL_SEED = b"p" * 32
_ATTESTOR_SEED = b"a" * 32

OPERATOR = "urn:bernstein:principal:operator:alex"
ORCHESTRATOR = "urn:bernstein:principal:agent:orchestrator-1"
REVIEWER = "urn:bernstein:principal:agent:reviewer-7"

BINDINGS_HASH = hashlib.sha256(b"role-bindings/v3").hexdigest()
ARTEFACT_DIGEST = hashlib.sha256(b"lineage/step-0007.json").hexdigest()


def _sha256_jcs(value: Any) -> str:
    """Content hash over the RFC 8785 canonical bytes of *value*."""
    return hashlib.sha256(canonicalize_jcs(value)).hexdigest()


def _jwk(private_key: Ed25519PrivateKey) -> dict[str, str]:
    """RFC 8037 OKP JWK for the public half of *private_key*."""
    from cryptography.hazmat.primitives import serialization

    raw = private_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return {"kty": "OKP", "crv": "Ed25519", "x": _b64url(raw)}


def _grant(
    *,
    grant_id: str,
    parent_hash: str,
    parent: str | None,
    issuer: str,
    subject: str,
    scope: list[str],
    not_after: str,
) -> dict[str, Any]:
    """Build one grant-chain link with its chained hash filled in."""
    scope = sorted(scope)
    grant_hash = _sha256_jcs(
        {
            "v": SCHEMA_VERSION,
            "grant_id": grant_id,
            "issuer": issuer,
            "subject": subject,
            "scope": scope,
            "not_after": not_after,
            "parent_hash": parent_hash,
        }
    )
    return {
        "grant_id": grant_id,
        "parent": parent,
        "issuer": issuer,
        "subject": subject,
        "scope": scope,
        "not_after": not_after,
        "grant_hash": grant_hash,
    }


def _decision(
    *,
    decision_id: str,
    grant: dict[str, Any],
    action: str,
    resource: str,
    verdict: str,
    policy: dict[str, str],
    inputs: dict[str, Any],
    timestamp: str,
) -> dict[str, Any]:
    """Build one decision record with its recomputable input hash filled in."""
    inputs_hash = _sha256_jcs(
        {
            "v": SCHEMA_VERSION,
            "subject": grant["subject"],
            "action": action,
            "resource": resource,
            "policy": policy,
            "inputs": inputs,
            "grant_hash": grant["grant_hash"],
        }
    )
    return {
        "decision_id": decision_id,
        "grant": grant["grant_id"],
        "subject": grant["subject"],
        "action": action,
        "resource": resource,
        "verdict": verdict,
        "policy": policy,
        "inputs": inputs,
        "inputs_hash": inputs_hash,
        "timestamp": timestamp,
    }


def build_envelope() -> dict[str, Any]:
    """Build and sign the deliberately-partial golden envelope."""
    principal_key = Ed25519PrivateKey.from_private_bytes(_PRINCIPAL_SEED)
    attestor_key = Ed25519PrivateKey.from_private_bytes(_ATTESTOR_SEED)

    principal_jwk = _jwk(principal_key)
    principal = {
        "id": REVIEWER,
        "key_id": "principal-reviewer-7",
        "key": principal_jwk,
        "id_binding": _sha256_jcs({"v": SCHEMA_VERSION, "id": REVIEWER, "key": principal_jwk}),
    }

    root = _grant(
        grant_id="g-root",
        parent=None,
        parent_hash="",
        issuer=OPERATOR,
        subject=ORCHESTRATOR,
        scope=["repo.read", "repo.write", "task.close", "task.read"],
        not_after="2030-01-01T00:00:00Z",
    )
    hop = _grant(
        grant_id="g-hop1",
        parent=root["grant_id"],
        parent_hash=root["grant_hash"],
        issuer=ORCHESTRATOR,
        subject=REVIEWER,
        scope=["repo.read", "task.close"],
        not_after="2027-01-01T00:00:00Z",
    )
    grants = [root, hop]

    policy = {"id": "policy:role-bindings", "version": "3"}
    inputs = {"role": "reviewer", "bindings_hash": BINDINGS_HASH}
    decisions = [
        _decision(
            decision_id="d-1",
            grant=hop,
            action="repo.read",
            resource="urn:bernstein:repo:bernstein",
            verdict="allow",
            policy=policy,
            inputs=inputs,
            timestamp="2026-05-04T09:15:00Z",
        ),
        _decision(
            decision_id="d-2",
            grant=hop,
            action="task.close",
            resource="urn:bernstein:task:T-4180",
            verdict="allow",
            policy=policy,
            inputs=inputs,
            timestamp="2026-05-04T09:17:30Z",
        ),
    ]

    evidence = [
        {
            "decision": "d-1",
            "name": "lineage/step-0007.json",
            "digest": {"sha256": ARTEFACT_DIGEST},
        }
    ]

    coverage = {
        "covered": ["d-1"],
        "uncovered": [
            {
                "decision_id": "d-2",
                "action": "task.close",
                "reason": "the artefact this decision authorised was not exported with the envelope",
            }
        ],
        "statement": (
            "Covers 1 of 2 authorization decisions with an artefact hash. The envelope proves "
            "the recorded grant chain attenuates, that each decision follows from the link it "
            "cites, and that the covered decision names an artefact digest. It does not prove "
            "the artefact was produced, that the signing key is trusted, or that the grants "
            "were unrevoked."
        ),
    }

    body: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "envelope_type": ENVELOPE_TYPE,
        "principal": principal,
        "grants": grants,
        "decisions": decisions,
        "evidence": evidence,
        "coverage": coverage,
    }
    body["section_digests"] = {
        name: _sha256_jcs(body[name]) for name in ("principal", "grants", "decisions", "evidence", "coverage")
    }

    kid = "authority-envelope-vector-key"
    header = {"alg": "EdDSA", "typ": JWS_TYP, "kid": kid}
    header_b64 = _b64url(canonicalize_jcs(header))
    body_b64 = _b64url(canonicalize_jcs(body))
    signature = attestor_key.sign(f"{header_b64}.{body_b64}".encode("ascii"))

    envelope = dict(body)
    envelope["signature"] = {
        "alg": "EdDSA",
        "kid": kid,
        "public_key_jwk": _jwk(attestor_key),
        "jws": f"{header_b64}..{_b64url(signature)}",
    }
    return envelope


def main() -> None:
    """Write the valid and tampered vectors."""
    envelope = build_envelope()

    valid_path = OUT_DIR / "valid-authority-envelope.json"
    valid_bytes = json.dumps(envelope, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    valid_path.write_bytes(valid_bytes)
    print(f"Wrote valid envelope:    {valid_path}  ({len(valid_bytes)} bytes)")

    tampered = json.loads(valid_bytes)
    original = tampered["decisions"][1]["verdict"]
    tampered["decisions"][1]["verdict"] = "deny"
    print(f"Tampering decisions[1].verdict: {original!r} -> 'deny'")
    tampered_path = OUT_DIR / "tampered-authority-envelope.json"
    tampered_bytes = json.dumps(tampered, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    tampered_path.write_bytes(tampered_bytes)
    print(f"Wrote tampered envelope: {tampered_path}  ({len(tampered_bytes)} bytes)")


if __name__ == "__main__":
    main()
