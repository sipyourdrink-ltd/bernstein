"""Trust Record emitter for TRACE 0.2 format.

This module provides a deterministic emitter that constructs a TRACE 0.2
compliant Trust Record from a journal path, signs it with the install
Ed25519 identity, and returns the canonical JSON.

TRACE 0.2 schema (TR-SIG):

    {
      "subject": "<string>",
      "delegation": "<string>",   // optional
      "claims": { ... },          // key-value pairs
      "signature": {
        "alg": "EdDSA",
        "kid": "<key-id>",
        "sig": "<base64url>"
      }
    }

The emitter:

- Takes a journal path and reads its events
- Maps events to TRACE 0.2 claims (run_id, event_count, head hash, timestamps)
- Signs with the install identity via existing signing infrastructure
- Returns canonical JSON via json.dumps(..., sort_keys=True, separators=(",", ":"))
- Uses import guards to avoid pulling agentrust_trace when [trace] extra is absent

Public surface:

- :class:`TrustRecordEmitter` -- ``emit_trust_record`` method.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

__all__ = ["TrustRecordEmitter"]

#: TRACE 0.2 signature algorithm identifier for EdDSA (Ed25519).
_TRACE_SIG_ALG: str = "EdDSA"

#: TRACE 0.2 type binding for Trust Records.
_TRACE_TR_TYP: str = "trust-record+jws"


@dataclass(frozen=True, slots=True)
class TrustRecord:
    """TRACE 0.2 Trust Record payload.

    Attributes:
        subject: Primary subject identifier (e.g. run_id).
        delegation: Optional delegation chain reference.
        claims: Key-value claims about the subject.
        signature: Detached JWS signature metadata and bytes.
    """

    subject: str
    delegation: str | None
    claims: dict[str, Any]
    signature: dict[str, Any]


class TrustRecordEmitter:
    """Emitter for TRACE 0.2 compliant Trust Records from journal data.

    The emitter reads a journal file, extracts the chain head and event
    count, maps them to TRACE 0.2 claims, signs with the install identity,
    and returns canonical JSON.
    """

    def __init__(
        self,
        *,
        install_rev_getter: Callable[[], str] | None = None,
        get_private_key_pem: Callable[[], bytes] | None = None,
    ) -> None:
        """Initialize emitter with optional injectable dependencies.

        Args:
            install_rev_getter: Callable returning the install revision
                token. Defaults to :func:`bernstein.core.identity.install_rev.get_install_rev`.
            get_private_key_pem: Callable returning the install Ed25519
                private key PEM. Defaults to loading from the install
                keystore via :func:`default_keystore`.
        """
        self._install_rev_getter = install_rev_getter
        self._private_key_provider = get_private_key_pem

    def _get_install_rev(self) -> str:
        """Return the install revision token."""
        if self._install_rev_getter is not None:
            return self._install_rev_getter()
        from bernstein.core.identity.install_rev import get_install_rev

        return get_install_rev()

    def _get_private_key_pem(self) -> bytes:
        """Return the install Ed25519 private key PEM."""
        if self._private_key_provider is not None:
            return self._private_key_provider()
        from bernstein.core.identity.http_signing import default_keystore

        private_pem, _ = default_keystore().load_or_generate()
        return private_pem

    def _build_unsigned_record(self, journal_path: Path, run_id: str) -> TrustRecord:
        """Build the unsigned Trust Record from journal data.

        Args:
            journal_path: Path to the journal.jsonl file.
            run_id: The run identifier (used as primary subject).

        Returns:
            TrustRecord with claims populated but no signature.
        """
        # Read journal file
        try:
            lines = journal_path.read_text(encoding="utf-8").strip().splitlines()
        except OSError:
            lines = []

        events = []
        for line in lines:
            if line.strip():
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

        # Verify the journal's hash chain before trusting its head. A
        # tampered journal (reordered or mutated events) must not produce a
        # record; the error names the divergent step so a repairer can find it
        # (R12: verifiers name the diverging element, never a bare true/false).
        from bernstein.core.replay.journal import JournalVerifyResult, verify_events

        verdict: JournalVerifyResult = verify_events(events)
        if not verdict.chain_consistent:
            reason = verdict.errors[0] if verdict.errors else f"step {verdict.divergent_index}"
            raise ValueError(f"journal chain broken: {reason}")

        event_count = len(events)
        head_hash = ""
        first_ts: float | None = None
        last_ts: float | None = None

        if events:
            head_hash = events[-1].get("event_hash", "")
            first_ts = events[0].get("ts")
            last_ts = events[-1].get("ts")

        claims: dict[str, Any] = {
            "run_id": run_id,
            "event_count": event_count,
            "head_hash": head_hash,
        }
        if first_ts is not None:
            claims["first_event_ts"] = first_ts
        if last_ts is not None:
            claims["last_event_ts"] = last_ts

        # Subject: run_id as primary
        # Secondary claim: install identity URI
        install_rev = self._get_install_rev()
        subject_uri = f"urn:bernstein:run:{run_id}"
        delegation = f"urn:bernstein:install:{install_rev}"

        return TrustRecord(
            subject=subject_uri,
            delegation=delegation,
            claims=claims,
            signature={},
        )

    def _sign_record(self, record: TrustRecord, kid: str) -> TrustRecord:
        """Sign a Trust Record using Ed25519.

        Args:
            record: Unsigned Trust Record.
            kid: Key identifier for the signing key.

        Returns:
            TrustRecord with signature populated.
        """
        # Build the canonical claim body (without signature)
        body = {
            "subject": record.subject,
            "delegation": record.delegation,
            "claims": record.claims,
        }

        # Canonical bytes: sorted keys, minimal separators
        canonical_bytes = json.dumps(
            body,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

        # Sign using existing infrastructure (Ed25519 via sign_detached_jws_over_canonical)
        private_key_pem = self._get_private_key_pem()
        detached_jws = _sign_canonical_bytes_detached(canonical_bytes, private_key_pem, _TRACE_TR_TYP, kid)

        # Parse compact JWS to extract signature bytes
        # Format: base64url(header)..base64url(signature)
        parts = detached_jws.split(".")
        sig_b64 = parts[2] if len(parts) == 3 else ""

        # Build signature object per TR-SIG
        signature = {
            "alg": _TRACE_SIG_ALG,
            "kid": kid,
            "sig": sig_b64,
        }

        return TrustRecord(
            subject=record.subject,
            delegation=record.delegation,
            claims=record.claims,
            signature=signature,
        )

    def emit_trust_record(self, journal_path: Path, run_id: str) -> str:
        """Emit a TRACE 0.2 Trust Record as canonical JSON.

        Args:
            journal_path: Path to the journal.jsonl file.
            run_id: The run identifier (used as primary subject).

        Returns:
            Canonical JSON string of the signed Trust Record.
        """
        # Build unsigned record
        record = self._build_unsigned_record(journal_path, run_id)

        # Get install rev as kid
        install_rev = self._get_install_rev()
        kid = f"install-{install_rev}"

        # Sign the record
        signed = self._sign_record(record, kid)

        # Build final output with signature inline
        output: dict[str, Any] = {
            "subject": signed.subject,
            "delegation": signed.delegation,
            "claims": signed.claims,
            "signature": signed.signature,
        }

        # Return canonical JSON
        return json.dumps(output, sort_keys=True, separators=(",", ":"))


def _sign_canonical_bytes_detached(
    canonical_bytes: bytes,
    private_key_pem: bytes,
    typ: str,
    kid: str,
) -> str:
    """Sign canonical bytes as a detached JWS (RFC 7515 §A.5).

    This is a copy of the local helper pattern from agent_card_signer
    to avoid circular import issues when this module is loaded.
    """
    from bernstein.core.security.agent_card_signer import (
        _b64url,
        canonicalize_jcs,
    )

    header = {"alg": "EdDSA", "typ": typ, "kid": kid}
    header_b64 = _b64url(canonicalize_jcs(header))
    body_b64 = _b64url(canonical_bytes)
    signing_input = f"{header_b64}.{body_b64}".encode("ascii")

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private_key = serialization.load_pem_private_key(private_key_pem, password=None)
    if not isinstance(private_key, Ed25519PrivateKey):
        msg = "_sign_canonical_bytes_detached requires an Ed25519 (EdDSA) private key"
        raise ValueError(msg)
    sig_b64 = _b64url(private_key.sign(signing_input))
    return f"{header_b64}..{sig_b64}"
