#!/usr/bin/env python3
"""Re-mint the evidence-envelope golden vector in this directory (issue #5063).

Run by hand from a source checkout, never by the test suite::

    uv run python tests/fixtures/evidence-envelope-vectors/_build_evidence_envelope_vectors.py

Importing this module builds nothing: everything happens inside
:func:`build`, behind a ``__main__`` guard, so a test can import it and call
it against a temporary directory without overwriting the committed files.

What is authored here and what is not
-------------------------------------
This slice ships no producer, so the envelope's *content* is authored below
rather than projected from a run: five declared actions, three of them
carrying decisions, and the other two named in ``coverage.uncovered``. That
content is a worked example of the schema, and it is chosen to exercise the
rule the format exists for -- a partially-covered envelope that says so.

The *encoding* is not authored. The canonical bytes come from
:func:`~bernstein.core.security.evidence_envelope.canonical_envelope_bytes`
and the signature is a real Ed25519 signature over
:func:`~bernstein.core.security.evidence_envelope.envelope_signing_input`,
both production functions. That is what makes the committed file able to
detect encoding drift:
``tests/unit/test_evidence_envelope_format_vectors.py`` re-encodes the
committed bytes with today's canonicaliser and re-verifies the committed
signature, so a change to either side diverges from a file that was already
published.

Determinism
-----------
Every input is fixed: the signing key is derived from a pinned seed, the
timestamps are constants, and JCS plus Ed25519 are deterministic given
deterministic inputs. Running this twice must produce byte-identical output,
which ``test_regenerating_the_vector_is_byte_identical_to_the_committed_file``
enforces.

The signing key is a test key, published in this directory alongside the
vector it signs. It is not, and must never become, an installation identity.
"""

from __future__ import annotations

import base64
import hashlib
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "src"))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.core.security.agent_card_signer import canonicalize_jcs
from bernstein.core.security.evidence_envelope import (
    EVIDENCE_ENVELOPE_SCHEMA_VERSION,
    EVIDENCE_ENVELOPE_TYP,
    EVIDENCE_ENVELOPE_TYPE,
    canonical_envelope_bytes,
    envelope_jws_header,
    envelope_signing_input,
)

#: Deterministic signing seed the vector was minted under. A test-only key,
#: published alongside the vector it signs.
_SIGN_SEED = b"e" * 32

#: Key identifier carried in both the JWK and the JWS protected header.
_KID = "install-evidencefixture01"

#: Fixture clock. Every timestamp below is a constant, never wall-clock.
_ISSUED_AT = 1700000000

_VECTOR_NAME = "partial-coverage-envelope.json"
_DIGEST_NAME = "partial-coverage-envelope.sha256"
_KEY_NAME = "evidence-envelope-vectors-key.pem"


def _b64url(data: bytes) -> str:
    """Base64-url-encode without padding (RFC 7515 section 2)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _fixture_digest(label: str) -> str:
    """Return a deterministic ``sha256:<hex>`` stand-in for *label*.

    The vector references material that does not exist on any machine that
    reads it, so the digests cannot be real content hashes. They are derived
    from a fixed label instead, which keeps the file reproducible and keeps
    the shape a reader checks (``sha256:`` plus 64 lowercase hex) honest.
    """
    return f"sha256:{hashlib.sha256(label.encode('utf-8')).hexdigest()}"


def _hex_digest(label: str) -> str:
    """Return the bare hex form of :func:`_fixture_digest` for evidence rows."""
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _binding() -> dict[str, Any]:
    """Return every envelope section except the signature."""
    return {
        "schema_version": EVIDENCE_ENVELOPE_SCHEMA_VERSION,
        "envelope_type": EVIDENCE_ENVELOPE_TYPE,
        "principal": {
            "id": "spiffe://bernstein.run/install/evidence-fixture",
            "key": {
                "kty": "OKP",
                "crv": "Ed25519",
                "x": _public_key_b64url(),
                "kid": _KID,
            },
        },
        "grants": [
            {
                "grant_id": "grant-root",
                "parent_grant_id": None,
                "issuer": "spiffe://bernstein.run/operator/fixture",
                "subject": "spiffe://bernstein.run/install/evidence-fixture",
                "actions": ["repo.read", "repo.write", "net.fetch"],
                "not_after": _ISSUED_AT + 86400,
                "constraints_hash": _fixture_digest("grant-root-constraints"),
            },
            {
                "grant_id": "grant-worker",
                "parent_grant_id": "grant-root",
                "issuer": "spiffe://bernstein.run/install/evidence-fixture",
                "subject": "spiffe://bernstein.run/install/evidence-fixture/worker/1",
                "actions": ["repo.read", "repo.write"],
                "not_after": _ISSUED_AT + 3600,
                "constraints_hash": _fixture_digest("grant-worker-constraints"),
            },
        ],
        "decisions": [
            _decision("d1", "repo.read", "allow", "grant-worker", 1),
            _decision("d2", "repo.write", "allow", "grant-worker", 2),
            _decision("d3", "net.fetch", "deny", None, 3),
        ],
        "evidence": [
            {
                "decision_id": "d1",
                "kind": "spine_entry",
                "digest": {"alg": "sha256", "value": _hex_digest("d1-spine")},
                "locator": ".sdd/runs/fixture-run/spine.jsonl#3",
            },
            {
                "decision_id": "d2",
                "kind": "artifact",
                "digest": {"alg": "sha256", "value": _hex_digest("d2-artifact")},
                "locator": ".sdd/artifacts/fixture-run/patch.diff",
            },
            {
                "decision_id": "d3",
                "kind": "journal_entry",
                "digest": {"alg": "sha256", "value": _hex_digest("d3-journal")},
                "locator": ".sdd/runs/fixture-run/journal.jsonl#11",
            },
        ],
        "coverage": {
            "actions_declared": 5,
            "actions_covered": 3,
            "uncovered": [
                {
                    "action": "repo.push",
                    "reason": "the action was recorded in the run journal with no governance decision beside it",
                },
                {
                    "action": "net.fetch:proxy",
                    "reason": "the decision was made before this envelope's window opened",
                },
            ],
            "limitations": [
                "Software evidence signed by an installation key: there is no TEE, no TPM and no hardware root of trust behind any claim here.",
                "The envelope proves these sections were signed together. It does not prove the actions happened, and it cannot prove that an action absent from both the decisions and the uncovered list was never taken.",
                "The grant chain is carried, not evaluated: whether each link attenuates its parent and was live at decision time is a verifier's question, and no verifier ships in this slice.",
            ],
        },
    }


def _decision(decision_id: str, action: str, verdict: str, grant_id: str | None, offset: int) -> dict[str, Any]:
    """Return one decision row in the shape GovernanceDecision projects into."""
    return {
        "decision_id": decision_id,
        "run_id": "fixture-run",
        "subject": "spiffe://bernstein.run/install/evidence-fixture/worker/1",
        "action": action,
        "verdict": verdict,
        "policy": {
            "id": "bernstein.default-gate",
            "version": "2026.08.1",
            "bundle_hash": _fixture_digest("gate-bundle"),
        },
        "inputs_hash": _fixture_digest(f"{decision_id}-inputs"),
        "timestamp": _ISSUED_AT + offset,
        "grant_id": grant_id,
        "journal_entry_hash": _fixture_digest(f"{decision_id}-anchor"),
    }


def _private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(_SIGN_SEED)


def _public_key_b64url() -> str:
    raw = (
        _private_key()
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    )
    return _b64url(raw)


def build(dest: Path) -> Path:
    """Write the vector, its digest sidecar and the public key into *dest*.

    Returns:
        The path of the written envelope.
    """
    dest.mkdir(parents=True, exist_ok=True)
    envelope = _binding()

    header = envelope_jws_header(_KID)
    header_b64 = _b64url(canonicalize_jcs(header))
    signature = _private_key().sign(envelope_signing_input(header_b64=header_b64, envelope=envelope))
    envelope["signature"] = {
        "alg": "EdDSA",
        "typ": EVIDENCE_ENVELOPE_TYP,
        "kid": _KID,
        "jws": f"{header_b64}..{_b64url(signature)}",
    }

    payload = canonical_envelope_bytes(envelope)
    vector_path = dest / _VECTOR_NAME
    vector_path.write_bytes(payload)
    (dest / _DIGEST_NAME).write_text(
        f"{hashlib.sha256(payload).hexdigest()}  {_VECTOR_NAME}\n",
        encoding="utf-8",
    )
    (dest / _KEY_NAME).write_bytes(
        _private_key()
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return vector_path


if __name__ == "__main__":
    written = build(Path(__file__).resolve().parent)
    print(f"wrote {written}")
