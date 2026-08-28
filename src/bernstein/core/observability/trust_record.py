"""Trust Record emitter for TRACE 0.2 format.

This module provides a deterministic emitter that constructs a TRACE 0.2
compliant Trust Record from a journal path, signs it with the install
Ed25519 identity, and returns the canonical JSON.

This is signed software evidence, not hardware attestation: the producer
has no TEE, no TPM, and no hardware root of trust. Every claim below is
derived from the run journal and the install's Ed25519 key alone.

Seal boundary: the signed seal proves the journal presented matches what
was sealed at signing time; it cannot prove that every action taken during
the run was recorded to the journal in the first place.

TRACE 0.2 schema (TR-SIG), corrected against a producer-mapping review of
agentrust-io/trace-spec#231 (issue #4692 -- five field-level corrections
to what #4684 originally shipped):

    {
      "subject": "<did:key URI, run-scoped>",
      "enforce": <bool>,
      "runtime": {"platform": "<str>", "measurement": "<sealed journal head hash>"},
      "references": [{"rel": "<str>", "id": "<str>", "resolver": "<str>"}, ...],
      "appraisal": {"status": "<str>", "verifier": "<URI>"},
      "parent_record_hash": "<sha256 hex of the parent's canonical record>" | null,
      "claims": { ... },          // key-value pairs
      "signature": {
        "alg": "EdDSA",
        "kid": "<key-id>",
        "sig": "<base64url>"
      }
    }

Subject scheme: ``did:key``, derived from the install Ed25519 public key
(multicodec ``ed25519-pub`` 0xed01, multibase base58btc) and scoped to the
run via a DID URL path (``did:key:z6Mk.../run/<run_id>``). did:key is
self-certifying: a verifier recovers the exact signing key directly from
the subject string, with no registry and no operator-configured trust
domain. This codebase also ships SPIFFE workload-identity machinery
(:mod:`bernstein.core.identity.spiffe`), which was the scheme considered
first -- but every SPIFFE derivation there requires an operator-supplied
trust domain with no default anywhere (the CLI's ``--trust-domain`` is
``required=True``, and the live SVID path needs a reachable SPIRE Workload
API that degrades to ``None`` when absent), so adopting it here would mean
either inventing a trust domain or adding a new required input to this
emitter. did:key needs only key material the emitter already carries, so
the public API this module exposes is unchanged.

Delegation: a delegated multi-agent run emits one Trust Record per
execution hop rather than nesting them. A child hop's record carries
``parent_record_hash``, the SHA-256 of the parent's own canonical signed
record (pass the parent's ``emit_trust_record`` return value back in as
``parent_record=``); ``references[]`` additionally carries a
``predecessor`` entry pointing at the parent's subject, so a verifier can
walk the chain without a side channel. A root execution's
``parent_record_hash`` is ``null``.

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

import hashlib
import json
import sys
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

#: Base58 (Bitcoin/multibase) alphabet: digits and letters with the four
#: visually-ambiguous characters (``0``, ``O``, ``I``, ``l``) removed.
_BASE58BTC_ALPHABET: str = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

#: Multibase prefix character selecting the base58btc encoding (multibase
#: table entry ``z``). Prepended to every did:key method-specific-id.
_MULTIBASE_BASE58BTC_PREFIX: str = "z"

#: Multicodec code for an Ed25519 public key (``0xed``), unsigned-varint
#: (LEB128) encoded. ``0xed`` = ``0b1110_1101`` does not fit the low 7 bits
#: of a single varint byte, so the encoding is two bytes: the continuation
#: byte ``0xed`` (low 7 bits ``0x6d`` with the continuation bit ``0x80``
#: set), then the remaining high bit as the terminal byte ``0x01``.
_MULTICODEC_ED25519_PUB: bytes = bytes([0xED, 0x01])


@dataclass(frozen=True, slots=True)
class TrustRecord:
    """TRACE 0.2 Trust Record payload.

    Attributes:
        subject: Self-certifying did:key URI, scoped to the run.
        enforce: Whether Bernstein's policy/capability enforcement applied
            to this run. Unconditionally ``True`` today: capability-scope
            and circuit-breaker enforcement are not optional per-run
            toggles in this codebase.
        runtime: ``{"platform": <software runtime id>, "measurement": <sealed
            journal head hash>}``. Software evidence only -- never a
            hardware measurement.
        references: Pointers to related evidence, each carrying ``rel``,
            ``id``, and ``resolver``. Always includes the run's own journal
            as ``rel="evidence"`` (when it has any events); a delegated
            child additionally carries a ``rel="predecessor"`` entry
            pointing at its parent.
        appraisal: ``{"status": <str>, "verifier": <URI>}``. This
            producer's only appraisal is the journal-chain check
            performed before a record can be built at all; ``verifier`` is
            therefore this record's own subject (self-attestation, not an
            independent third party).
        parent_record_hash: SHA-256 hex of the parent execution's own
            canonical signed record, for one hop of a delegated run.
            ``None`` for a root execution.
        claims: Key-value claims about the subject (run_id, event_count,
            head_hash, and optional first/last event timestamps).
        signature: Detached JWS signature metadata and bytes.
    """

    subject: str
    enforce: bool
    runtime: dict[str, str]
    references: list[dict[str, str]]
    appraisal: dict[str, str]
    parent_record_hash: str | None
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

    def _build_unsigned_record(
        self,
        journal_path: Path,
        run_id: str,
        *,
        parent_record: str | None = None,
    ) -> TrustRecord:
        """Build the unsigned Trust Record from journal data.

        Args:
            journal_path: Path to the journal.jsonl file.
            run_id: The run identifier (used to scope the subject).
            parent_record: The parent execution's own canonical signed
                record -- a prior return value of :meth:`emit_trust_record`
                -- when this call is one hop of a delegated multi-agent
                run. ``None`` (the default) for a root execution.

        Returns:
            TrustRecord with claims populated but no signature.

        Raises:
            ValueError: The journal's hash chain does not verify, or
                *parent_record* was given but is not valid JSON.
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

        # Subject: a self-certifying did:key URI over the install Ed25519
        # public key, scoped to this run via a DID URL path (issue #4692).
        public_key_raw = _ed25519_public_key_raw(self._get_private_key_pem())
        did_key = _did_key_from_ed25519_public_key(public_key_raw)
        subject_uri = f"{did_key}/run/{run_id}"

        # runtime: software-only producer. platform names the interpreter;
        # measurement is the sealed journal head hash -- never a hardware
        # measurement (see the module docstring's seal-boundary sentence).
        runtime: dict[str, str] = {
            "platform": _software_runtime_platform(),
            "measurement": head_hash,
        }

        # references[]: every record points back at the journal it was
        # derived from. Omitted when there is no head hash to point at (an
        # empty journal), the same way first/last_event_ts are omitted
        # rather than emitted hollow. A delegated child additionally points
        # at its parent.
        references: list[dict[str, str]] = []
        if head_hash:
            references.append({"rel": "evidence", "id": f"sha256:{head_hash}", "resolver": "urn:bernstein:journal"})

        parent_record_hash: str | None = None
        if parent_record is not None:
            parent_record_hash = hashlib.sha256(parent_record.encode("utf-8")).hexdigest()
            try:
                parent_doc = json.loads(parent_record)
            except json.JSONDecodeError as exc:
                raise ValueError(f"parent_record is not valid JSON: {exc}") from exc
            parent_subject = ""
            if isinstance(parent_doc, dict):
                candidate_subject = parent_doc.get("subject")
                if isinstance(candidate_subject, str):
                    parent_subject = candidate_subject
            references.append(
                {
                    "rel": "predecessor",
                    "id": f"sha256:{parent_record_hash}",
                    "resolver": parent_subject,
                }
            )

        # appraisal: the only appraisal this producer can honestly perform
        # on its own evidence is the journal-chain check above, which
        # already gates this method returning at all -- reaching this line
        # means it affirmed. Self-attestation, not independent third-party
        # appraisal: the verifier is this record's own subject.
        appraisal: dict[str, str] = {"status": "affirming", "verifier": subject_uri}

        return TrustRecord(
            subject=subject_uri,
            enforce=True,
            runtime=runtime,
            references=references,
            appraisal=appraisal,
            parent_record_hash=parent_record_hash,
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
        # Build the canonical claim body (without signature). Every field
        # introduced by issue #4692 is included here -- a verifier must be
        # able to detect tampering with any of them, not only the
        # pre-existing subject/claims.
        body = {
            "subject": record.subject,
            "enforce": record.enforce,
            "runtime": record.runtime,
            "references": record.references,
            "appraisal": record.appraisal,
            "parent_record_hash": record.parent_record_hash,
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
            enforce=record.enforce,
            runtime=record.runtime,
            references=record.references,
            appraisal=record.appraisal,
            parent_record_hash=record.parent_record_hash,
            claims=record.claims,
            signature=signature,
        )

    def emit_trust_record(self, journal_path: Path, run_id: str, *, parent_record: str | None = None) -> str:
        """Emit a TRACE 0.2 Trust Record as canonical JSON.

        Args:
            journal_path: Path to the journal.jsonl file.
            run_id: The run identifier (used to scope the subject).
            parent_record: The parent execution's own canonical signed
                record, when this call is one hop of a delegated
                multi-agent run (see :meth:`_build_unsigned_record`).
                ``None`` (the default) for a root execution -- every
                existing call site is unaffected.

        Returns:
            Canonical JSON string of the signed Trust Record.
        """
        # Build unsigned record
        record = self._build_unsigned_record(journal_path, run_id, parent_record=parent_record)

        # Get install rev as kid
        install_rev = self._get_install_rev()
        kid = f"install-{install_rev}"

        # Sign the record
        signed = self._sign_record(record, kid)

        # Build final output with signature inline
        output: dict[str, Any] = {
            "subject": signed.subject,
            "enforce": signed.enforce,
            "runtime": signed.runtime,
            "references": signed.references,
            "appraisal": signed.appraisal,
            "parent_record_hash": signed.parent_record_hash,
            "claims": signed.claims,
            "signature": signed.signature,
        }

        # Return canonical JSON
        return json.dumps(output, sort_keys=True, separators=(",", ":"))


def _base58btc_encode(data: bytes) -> str:
    """Encode *data* as base58btc, with no multibase prefix.

    Standard big-integer base58: interpret the bytes as a big-endian
    unsigned integer, repeatedly divide by 58, and prepend the remainder's
    alphabet character. A leading zero byte has no representation in the
    integer, so each one is separately re-emitted as a leading ``"1"`` (the
    zero-value alphabet character) -- the convention Bitcoin addresses and
    IPFS CIDs use, and the inverse :func:`_base58btc_decode` relies on.
    """
    n = int.from_bytes(data, "big")
    digits = ""
    while n > 0:
        n, remainder = divmod(n, 58)
        digits = _BASE58BTC_ALPHABET[remainder] + digits
    n_leading_zero_bytes = len(data) - len(data.lstrip(b"\x00"))
    return ("1" * n_leading_zero_bytes) + digits


def _base58btc_decode(encoded: str) -> bytes:  # pyright: ignore[reportUnusedFunction]
    """Decode a base58btc string (no multibase prefix) back to bytes.

    Inverse of :func:`_base58btc_encode`. The emitter itself never decodes
    (it only ever mints a did:key), so this has no caller inside this
    module -- it exists for a verifier (or a test standing in for one) to
    recover the raw key bytes from a ``subject`` string, which is the
    entire point of a self-certifying identifier. Keeping the codec pair
    together, tested against each other by round-trip, is what makes that
    round-trip test meaningful in the first place.

    Raises:
        ValueError: *encoded* contains a character outside the base58btc
            alphabet.
    """
    n = 0
    for char in encoded:
        index = _BASE58BTC_ALPHABET.find(char)
        if index == -1:
            raise ValueError(f"{char!r} is not a base58btc character")
        n = n * 58 + index
    n_leading_ones = len(encoded) - len(encoded.lstrip("1"))
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n > 0 else b""
    return (b"\x00" * n_leading_ones) + body


def _did_key_from_ed25519_public_key(raw_public_key: bytes) -> str:
    """Return the ``did:key`` URI for a raw 32-byte Ed25519 public key.

    Self-certifying: any verifier holding just this URI recovers the exact
    signing key by multibase/multicodec-decoding it -- no registry, no
    trust domain, no separate key file. See the module docstring for why
    this scheme was chosen over the codebase's existing SPIFFE machinery.
    """
    tagged = _MULTICODEC_ED25519_PUB + raw_public_key
    return f"did:key:{_MULTIBASE_BASE58BTC_PREFIX}{_base58btc_encode(tagged)}"


def _ed25519_public_key_raw(private_key_pem: bytes) -> bytes:
    """Return the raw 32-byte Ed25519 public key for a PKCS8 private key PEM."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private_key = serialization.load_pem_private_key(private_key_pem, password=None)
    if not isinstance(private_key, Ed25519PrivateKey):
        msg = "install identity key is not Ed25519"
        raise ValueError(msg)
    return private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )


def _software_runtime_platform() -> str:
    """Return the software-runtime identifier for the ``runtime.platform`` claim.

    Names the interpreter and OS family the record was produced under.
    This producer is software-only (see the module docstring): the value
    names a software runtime and must never be read as a hardware
    measurement.
    """
    import platform as _platform

    return f"{_platform.python_implementation().lower()}-{_platform.python_version()}/{sys.platform}"


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
