"""External existence proofs for a run's sealed journal head (issue #4205).

The replay journal chains every step into a Merkle head, and run finalization
seals that head into the lineage spine
(:func:`bernstein.core.replay.journal.seal_journal_into_spine`). Both layers
are self-referential: the HMAC audit chain convinces whoever holds the key,
and the Ed25519 lineage signatures convince whoever trusts the install's own
identity. Neither pins two things a reviewer eventually asks for:

* **When** the head existed. Wall-clock fields are deliberately excluded from
  the Merkle payload hash, so the chain carries no trusted notion of time.
* **That** the head existed *before* a given moment. A key holder can rewrite
  the journal and re-seal it; the rewrite is internally consistent and nothing
  outside the install ever witnessed the original.

An RFC 3161 timestamp token closes both. The TSA signs its ``genTime``
together with the head digest, so the token is an independent witness that
this exact head existed before that instant, and it stays checkable offline
from the token plus an operator-pinned trust bundle - no call back to the TSA
at verify time.

Design decisions
----------------

* **The messageImprint is the head digest itself.** The sealed head is already
  a SHA-256 hex digest, so it is submitted as a pre-computed imprint
  (``openssl ts -query -digest <head>``). This is the same binding the
  multi-tenant audit-chain export already uses for ``chain_anchor.head_sha256``
  (:mod:`bernstein.core.security.audit_multitenant`), so one TSA workflow
  covers both surfaces and the existing offline verifier
  (:mod:`bernstein.core.security.rfc3161_verifier`) is reused verbatim.
* **No local clock is recorded.** Storing "when we anchored" next to the token
  would put an untrusted timestamp beside a trusted one. The only time an
  anchor carries is the TSA's, inside the token.
* **Nothing here reaches the network on its own.** :func:`request_timestamp_token`
  is the only function that opens a socket and it is reached only when an
  operator names a TSA URL; the verify path never does.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from cryptography import x509

__all__ = [
    "ANCHOR_FILENAME",
    "ANCHOR_KIND_RFC3161",
    "ANCHOR_SCHEMA_VERSION",
    "AnchorStatus",
    "AnchorVerification",
    "SealAnchor",
    "SealAnchorError",
    "build_rfc3161_anchor",
    "build_timestamp_request",
    "load_anchor",
    "request_timestamp_token",
    "verify_anchor",
    "write_anchor",
]

#: File the anchor is stored in, next to the run's ``journal.jsonl``.
ANCHOR_FILENAME = "seal_anchor.json"

#: Record format version. Bumped only for an incompatible field change.
ANCHOR_SCHEMA_VERSION = "1.0.0"

#: The only anchor kind this slice understands.
ANCHOR_KIND_RFC3161 = "rfc3161"

#: A sealed head is a SHA-256 digest rendered as lowercase hex.
_HEAD_PATTERN = re.compile(r"\A[0-9a-f]{64}\Z")

#: ``PKIStatus`` values that mean the TSA issued a token (RFC 3161 §2.4.2).
_GRANTED_STATUSES = frozenset({"granted", "granted_with_mods"})


class SealAnchorError(ValueError):
    """Raised when an anchor record is malformed or cannot be built."""


class AnchorStatus(StrEnum):
    """Verdict of :func:`verify_anchor`.

    Mirrors the three-way identity verdict the journal verifier already uses:
    a pass, a loud contradiction, and an honest "cannot tell", never a silent
    downgrade of one into another.
    """

    #: The token is a valid TSA chain over exactly the head presented.
    VERIFIED = "verified"
    #: The anchor witnesses a different head than the one presented.
    MISMATCHED = "mismatched"
    #: The token failed to parse, chain, or imprint-match.
    INVALID = "invalid"
    #: No TSA trust anchors were supplied, so nothing was checked.
    UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True, slots=True)
class SealAnchor:
    """One run's sealed head plus the external proof that it existed.

    Attributes:
        run_id: Run whose journal head is anchored.
        head_sha256: The sealed Merkle head, lowercase hex.
        anchor_kind: Always :data:`ANCHOR_KIND_RFC3161` in this slice.
        token_b64: Base64 of the DER ``TimeStampResp`` / ``TimeStampToken``.
        tsa_url: Where the token came from. Recorded for provenance only -
            verification never contacts it.
    """

    run_id: str
    head_sha256: str
    anchor_kind: str
    token_b64: str
    tsa_url: str

    def to_record(self) -> dict[str, Any]:
        """Return the on-disk record for this anchor."""
        return {
            "schema_version": ANCHOR_SCHEMA_VERSION,
            "run_id": self.run_id,
            "head_sha256": self.head_sha256,
            "anchor_kind": self.anchor_kind,
            "rfc3161_token_b64": self.token_b64,
            "rfc3161_tsa_url": self.tsa_url,
        }

    def token_der(self) -> bytes:
        """Decode the stored token.

        Raises:
            SealAnchorError: When the stored value is not valid base64.
        """
        try:
            return base64.b64decode(self.token_b64, validate=True)
        except (ValueError, binascii.Error) as exc:
            msg = f"rfc3161_token_b64 is not valid base64: {exc}"
            raise SealAnchorError(msg) from exc


def _empty_str_list() -> list[str]:
    return []


@dataclass(frozen=True, slots=True)
class AnchorVerification:
    """Outcome of :func:`verify_anchor`.

    Attributes:
        status: The verdict.
        errors: Human-readable reasons the verdict is not ``VERIFIED``.
        gen_time: The TSA's recorded time for the imprint, present only on a
            ``VERIFIED`` verdict - an unchecked token's ``genTime`` is not
            evidence of anything.
        tsa_subject: Subject DN of the signing TSA certificate, on a pass.
    """

    status: AnchorStatus
    errors: list[str] = field(default_factory=_empty_str_list)
    gen_time: datetime | None = None
    tsa_subject: str | None = None


def _require_head(head_sha256: str) -> str:
    if not _HEAD_PATTERN.fullmatch(head_sha256):
        msg = f"sealed head must be 64 lowercase hex characters (SHA-256), got {head_sha256!r}"
        raise SealAnchorError(msg)
    return head_sha256


def build_rfc3161_anchor(*, run_id: str, head_sha256: str, token_der: bytes, tsa_url: str) -> SealAnchor:
    """Bind a TSA timestamp token to the sealed head it was requested for.

    Args:
        run_id: Run the head belongs to.
        head_sha256: The sealed Merkle head, lowercase hex.
        token_der: DER bytes of the ``TimeStampResp`` or bare ``TimeStampToken``.
        tsa_url: URL the token came from, recorded for provenance.

    Returns:
        The anchor, ready to :func:`write_anchor`.

    Raises:
        SealAnchorError: When the head is not a SHA-256 hex digest, or the
            token is empty.
    """
    if not token_der:
        msg = "timestamp token is empty"
        raise SealAnchorError(msg)
    return SealAnchor(
        run_id=run_id,
        head_sha256=_require_head(head_sha256),
        anchor_kind=ANCHOR_KIND_RFC3161,
        token_b64=base64.b64encode(token_der).decode("ascii"),
        tsa_url=tsa_url,
    )


def write_anchor(path: Path, anchor: SealAnchor) -> None:
    """Write *anchor* to *path* as a stable, sorted JSON record."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(anchor.to_record(), indent=2, sort_keys=True) + "\n"
    path.write_text(payload, encoding="utf-8")


def load_anchor(path: Path) -> SealAnchor:
    """Read an anchor record written by :func:`write_anchor`.

    Raises:
        SealAnchorError: When the file is not a JSON object, is missing a
            required field, or declares an anchor kind this build cannot
            verify. Refusing an unknown kind is the point: a record we cannot
            check must never be checked by the wrong rules.
    """
    try:
        parsed: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        msg = f"unreadable anchor record at {path}: {exc}"
        raise SealAnchorError(msg) from exc
    if not isinstance(parsed, dict):
        msg = f"anchor record at {path} is not a JSON object"
        raise SealAnchorError(msg)
    raw = cast("dict[str, Any]", parsed)

    kind = raw.get("anchor_kind")
    if kind != ANCHOR_KIND_RFC3161:
        msg = f"unsupported anchor_kind {kind!r} in {path} (this build verifies {ANCHOR_KIND_RFC3161!r} only)"
        raise SealAnchorError(msg)

    fields: dict[str, str] = {}
    for name, key in (
        ("run_id", "run_id"),
        ("head_sha256", "head_sha256"),
        ("token_b64", "rfc3161_token_b64"),
        ("tsa_url", "rfc3161_tsa_url"),
    ):
        value = raw.get(key, "" if key == "rfc3161_tsa_url" else None)
        if not isinstance(value, str):
            msg = f"anchor record at {path} is missing a string {name!r}"
            raise SealAnchorError(msg)
        fields[name] = value

    return SealAnchor(
        run_id=fields["run_id"],
        head_sha256=_require_head(fields["head_sha256"]),
        anchor_kind=ANCHOR_KIND_RFC3161,
        token_b64=fields["token_b64"],
        tsa_url=fields["tsa_url"],
    )


def verify_anchor(
    anchor: SealAnchor,
    *,
    sealed_head: str,
    trusted_tsa_certs: list[x509.Certificate],
) -> AnchorVerification:
    """Check *anchor* against the head a verifier recomputed, offline.

    Three steps, in this order, because a later step's verdict would be
    misleading once an earlier one has failed:

    1. **Binding.** The anchor's head must equal ``sealed_head``. A run that
       was rewritten and re-sealed reaches a different head, and no token can
       be re-obtained for the past - so a rewrite surfaces here as
       ``MISMATCHED``.
    2. **Trust anchors.** Without operator-pinned TSA roots there is nothing
       to chain to; the verdict is ``UNVERIFIABLE``, never a pass.
    3. **Chain.** The token is parsed, chained to those roots, its CMS
       signature checked, and its ``messageImprint`` compared with the head
       digest - all by :func:`~bernstein.core.security.rfc3161_verifier.verify_rfc3161_token`.

    Args:
        anchor: The stored anchor.
        sealed_head: The head recomputed from the artifacts on disk.
        trusted_tsa_certs: Operator-pinned TSA roots. Empty means no verdict.

    Returns:
        The verdict and its diagnostics.
    """
    if anchor.head_sha256 != sealed_head:
        return AnchorVerification(
            status=AnchorStatus.MISMATCHED,
            errors=[f"anchor witnesses head {anchor.head_sha256}, artifacts recompute to {sealed_head}"],
        )
    if not trusted_tsa_certs:
        return AnchorVerification(
            status=AnchorStatus.UNVERIFIABLE,
            errors=["no trusted TSA certificates supplied - the timestamp chain was not checked"],
        )

    try:
        token_der = anchor.token_der()
        imprint = bytes.fromhex(anchor.head_sha256)
    except (SealAnchorError, ValueError) as exc:
        return AnchorVerification(status=AnchorStatus.INVALID, errors=[str(exc)])

    # Lazy import: keeps the asn1crypto / x509 verification stack out of the
    # import path of callers that only read or write anchor records.
    from bernstein.core.security.rfc3161_verifier import verify_rfc3161_token

    result = verify_rfc3161_token(token_der, imprint, trusted_tsa_certs)
    if not result.ok:
        return AnchorVerification(
            status=AnchorStatus.INVALID,
            errors=[f"rfc3161 chain: {err}" for err in result.errors],
        )
    return AnchorVerification(
        status=AnchorStatus.VERIFIED,
        gen_time=result.gen_time,
        tsa_subject=result.tsa_subject,
    )


def build_timestamp_request(head_sha256: str, *, nonce: int) -> bytes:
    """Build the DER ``TimeStampReq`` that asks a TSA to witness *head_sha256*.

    The request carries the head digest as a pre-computed SHA-256
    ``messageImprint`` and nothing else about the run - a TSA learns a digest,
    never the journal. ``certReq`` is set so the reply embeds the TSA's own
    certificate, which is what makes the stored token verifiable offline
    later.

    Args:
        head_sha256: The sealed head, lowercase hex.
        nonce: Fresh random integer echoed back by the TSA, so a replayed
            reply for some other request is detectable.

    Returns:
        DER bytes to POST to the TSA.

    Raises:
        SealAnchorError: When the head is not a SHA-256 hex digest.
    """
    from asn1crypto import algos, tsp  # type: ignore[reportMissingTypeStubs]

    # asn1crypto ships no type stubs; the modules are bound through ``Any`` so
    # the untyped ASN.1 surface stays contained to these two statements.
    tsp_module: Any = tsp
    algos_module: Any = algos

    imprint = bytes.fromhex(_require_head(head_sha256))
    request: Any = tsp_module.TimeStampReq(
        {
            "version": "v1",
            "message_imprint": tsp_module.MessageImprint(
                {
                    "hash_algorithm": algos_module.DigestAlgorithm({"algorithm": "sha256"}),
                    "hashed_message": imprint,
                }
            ),
            "nonce": nonce,
            "cert_req": True,
        }
    )
    return cast("bytes", request.dump())


def _require_echoed_nonce(request_der: bytes, response_der: bytes) -> None:
    """Confirm the reply answers *this* request and not an earlier one.

    RFC 3161 §2.4.2 has the TSA copy the request's nonce into ``TSTInfo``. A
    reply that omits it, or carries a different one, may be a cached or
    replayed token for some other imprint, so it is refused rather than
    stored as if it witnessed this head.

    Raises:
        SealAnchorError: When the reply does not echo the request's nonce.
    """
    from asn1crypto import tsp  # type: ignore[reportMissingTypeStubs]

    tsp_module: Any = tsp
    try:
        request: Any = tsp_module.TimeStampReq.load(request_der)
        response: Any = tsp_module.TimeStampResp.load(response_der)
        sent = request["nonce"].native
        token: Any = response["time_stamp_token"]
        tst_info: Any = token["content"]["encap_content_info"]["content"].parsed
        echoed = tst_info["nonce"].native
    except (ValueError, KeyError, TypeError) as exc:
        msg = f"could not read the nonce back out of the TSA reply: {exc}"
        raise SealAnchorError(msg) from exc
    if sent != echoed:
        msg = f"TSA reply does not echo the request nonce (sent {sent}, got {echoed})"
        raise SealAnchorError(msg)


def request_timestamp_token(tsa_url: str, request_der: bytes, *, timeout: float = 30.0) -> bytes:
    """POST *request_der* to *tsa_url* and return the DER reply.

    The only function in this module that opens a socket. It is reached only
    when an operator explicitly names a TSA, so every other path - including
    the whole of verification - stays offline.

    Args:
        tsa_url: Operator-supplied ``http(s)`` endpoint of the TSA.
        request_der: Output of :func:`build_timestamp_request`.
        timeout: Per-request timeout in seconds.

    Returns:
        DER bytes of the ``TimeStampResp``.

    Raises:
        SealAnchorError: When the URL is not http(s), the request fails,
            the TSA declined to issue a token, or the reply does not echo
            the request's nonce.
    """
    import httpx
    from asn1crypto import tsp  # type: ignore[reportMissingTypeStubs]

    from bernstein.core.security.url_allowlist import UrlSchemeError, ensure_http_url

    try:
        ensure_http_url(tsa_url, allow_http=True, source="seal_anchor.request_timestamp_token")
    except UrlSchemeError as exc:
        msg = f"refusing to contact TSA: {exc}"
        raise SealAnchorError(msg) from exc

    try:
        response = httpx.post(
            tsa_url,
            content=request_der,
            headers={"Content-Type": "application/timestamp-query"},
            timeout=timeout,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        msg = f"TSA request to {tsa_url} failed: {exc}"
        raise SealAnchorError(msg) from exc

    body = response.content
    try:
        tsp_module: Any = tsp
        parsed_response: Any = tsp_module.TimeStampResp.load(body)
        status = cast("str", parsed_response["status"]["status"].native)
    except ValueError as exc:
        msg = f"TSA at {tsa_url} returned something that is not a TimeStampResp: {exc}"
        raise SealAnchorError(msg) from exc
    if status not in _GRANTED_STATUSES:
        msg = f"TSA at {tsa_url} declined to issue a token (PKIStatus={status})"
        raise SealAnchorError(msg)
    _require_echoed_nonce(request_der, body)
    return body
