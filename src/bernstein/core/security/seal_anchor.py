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

An RFC 3161 timestamp token closes the "when" question. The TSA signs its
``genTime`` together with the head digest, so the token is an independent
witness that this exact head existed before that instant, and it stays
checkable offline from the token plus an operator-pinned trust bundle - no
call back to the TSA at verify time.

A transparency-log inclusion proof closes the "anyone can check" question
(#6208). The sealed head is registered as a leaf in an external RFC 6962
append-only log; the stored audit path, signed tree head and log public key
let a stranger recompute the root and verify the log's signature offline,
with no call back to the log. A TSA that mis-issues leaves no public trace;
a log that signs a tree head does.

Design decisions
----------------

* **The messageImprint is the head digest itself.** The sealed head is already
  a SHA-256 hex digest, so it is submitted as a pre-computed imprint
  (``openssl ts -query -digest <head>``). This is the same binding the
  multi-tenant audit-chain export already uses for ``chain_anchor.head_sha256``
  (:mod:`bernstein.core.security.audit_multitenant`), so one TSA workflow
  covers both surfaces and the existing offline verifier
  (:mod:`bernstein.core.security.rfc3161_verifier`) is reused verbatim.
* **The transparency-log leaf is the bare sealed-head digest.** The leaf is
  ``SHA-256(0x00 || raw head bytes)`` using the same domain-separated hashing
  as :mod:`bernstein.core.persistence.merkle`. No run id and no timestamp
  enter the preimage, so two operators who re-derive the same sealed head
  get the same leaf from the seal alone. A statement wrapping ``run_id``
  would make the log entry self-describing, but it would also make the leaf
  depend on metadata the seal does not carry.
* **No local clock is recorded.** Storing "when we anchored" next to the token
  would put an untrusted timestamp beside a trusted one. The only time an
  RFC 3161 anchor carries is the TSA's, inside the token. A transparency-log
  anchor carries a tree size, not a wall clock.
* **Nothing here reaches the network on its own.** :func:`request_timestamp_token`
  is the only function that opens a socket and it is reached only when an
  operator names a TSA URL; the verify path never does. Transparency-log
  registration (SCITT) is a later slice.
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
    "ANCHOR_KIND_TRANSPARENCY_LOG",
    "ANCHOR_SCHEMA_VERSION",
    "AnchorStatus",
    "AnchorVerification",
    "SealAnchor",
    "SealAnchorError",
    "build_rfc3161_anchor",
    "build_timestamp_request",
    "build_transparency_log_anchor",
    "load_anchor",
    "request_timestamp_token",
    "transparency_log_leaf",
    "verify_anchor",
    "write_anchor",
]

#: File the anchor is stored in, next to the run's ``journal.jsonl``.
ANCHOR_FILENAME = "seal_anchor.json"

#: Record format version. Bumped only for an incompatible field change.
ANCHOR_SCHEMA_VERSION = "1.0.0"

#: RFC 3161 timestamping-authority token over the sealed head.
ANCHOR_KIND_RFC3161 = "rfc3161"

#: RFC 6962-style inclusion proof against an external append-only log.
ANCHOR_KIND_TRANSPARENCY_LOG = "transparency-log"

#: Anchor kinds this build can load and verify offline.
_SUPPORTED_ANCHOR_KINDS = frozenset({ANCHOR_KIND_RFC3161, ANCHOR_KIND_TRANSPARENCY_LOG})

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

    #: The token is a valid TSA chain over exactly the head presented,
    #: or the inclusion proof recomputes the signed tree head.
    VERIFIED = "verified"
    #: The anchor witnesses a different head than the one presented.
    MISMATCHED = "mismatched"
    #: The token failed to parse, chain, or imprint-match, or the
    #: inclusion proof / tree-head signature did not verify.
    INVALID = "invalid"
    #: No TSA trust anchors were supplied, so nothing was checked.
    UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True, slots=True)
class SealAnchor:
    """One run's sealed head plus the external proof that it existed.

    Attributes:
        run_id: Run whose journal head is anchored.
        head_sha256: The sealed Merkle head, lowercase hex.
        anchor_kind: :data:`ANCHOR_KIND_RFC3161` or
            :data:`ANCHOR_KIND_TRANSPARENCY_LOG`.
        token_b64: Base64 of the DER ``TimeStampResp`` / ``TimeStampToken``.
            Empty on a transparency-log anchor.
        tsa_url: Where the token came from. Recorded for provenance only -
            verification never contacts it. Empty on a transparency-log
            anchor.
        leaf_hash: RFC 6962 leaf over the bare sealed-head digest. Set on a
            transparency-log anchor.
        tree_size: Log tree size at which the leaf was included.
        audit_path: Inclusion path of ``{"hash", "left"}`` steps from leaf
            to root. Same shape as the self-hosted transparency receipt.
        signed_tree_head: Log tree head: ``tree_size``, ``root_hash``,
            ``signature_b64``.
        log_public_key: Ed25519 public key that signed the tree head,
            lowercase hex of the raw 32-byte key.
    """

    run_id: str
    head_sha256: str
    anchor_kind: str
    token_b64: str = ""
    tsa_url: str = ""
    leaf_hash: str | None = None
    tree_size: int | None = None
    audit_path: list[dict[str, Any]] | None = None
    signed_tree_head: dict[str, Any] | None = None
    log_public_key: str | None = None

    def to_record(self) -> dict[str, Any]:
        """Return the on-disk record for this anchor.

        RFC 3161 records keep the v3.19.2 field set so existing files stay
        byte-compatible. Transparency-log records omit the TSA fields.
        """
        record: dict[str, Any] = {
            "schema_version": ANCHOR_SCHEMA_VERSION,
            "run_id": self.run_id,
            "head_sha256": self.head_sha256,
            "anchor_kind": self.anchor_kind,
        }
        if self.anchor_kind == ANCHOR_KIND_TRANSPARENCY_LOG:
            record["leaf_hash"] = self.leaf_hash
            record["tree_size"] = self.tree_size
            record["audit_path"] = self.audit_path
            record["signed_tree_head"] = self.signed_tree_head
            record["log_public_key"] = self.log_public_key
            return record
        record["rfc3161_token_b64"] = self.token_b64
        record["rfc3161_tsa_url"] = self.tsa_url
        return record

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
            ``VERIFIED`` RFC 3161 verdict - an unchecked token's ``genTime``
            is not evidence of anything.
        tsa_subject: Subject DN of the signing TSA certificate, on a pass.
        tree_size: Log tree size, present only on a ``VERIFIED``
            transparency-log verdict.
    """

    status: AnchorStatus
    errors: list[str] = field(default_factory=_empty_str_list)
    gen_time: datetime | None = None
    tsa_subject: str | None = None
    tree_size: int | None = None


def _require_head(head_sha256: str) -> str:
    if not _HEAD_PATTERN.fullmatch(head_sha256):
        msg = f"sealed head must be 64 lowercase hex characters (SHA-256), got {head_sha256!r}"
        raise SealAnchorError(msg)
    return head_sha256


def _require_hex64(value: str, *, label: str) -> str:
    if not _HEAD_PATTERN.fullmatch(value):
        msg = f"{label} must be 64 lowercase hex characters, got {value!r}"
        raise SealAnchorError(msg)
    return value


def _canonical_sth_bytes(signed_tree_head: dict[str, Any]) -> bytes:
    """Canonical bytes the log signed: the tree head without the signature."""
    body = {key: value for key, value in signed_tree_head.items() if key != "signature_b64"}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def transparency_log_leaf(head_sha256: str) -> str:
    """RFC 6962 leaf over the bare sealed-head digest.

    ``SHA-256(0x00 || raw head bytes)``. The run id and any clock stay out
    of the preimage so the leaf is recomputable from the seal alone.
    """
    from bernstein.core.persistence.merkle import _leaf_digest

    return _leaf_digest(bytes.fromhex(_require_head(head_sha256)))


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


def build_transparency_log_anchor(
    *,
    run_id: str,
    head_sha256: str,
    leaf_hash: str,
    tree_size: int,
    audit_path: list[dict[str, Any]],
    signed_tree_head: dict[str, Any],
    log_public_key: str,
) -> SealAnchor:
    """Bind an RFC 6962 inclusion proof to the sealed head it witnesses.

    Args:
        run_id: Run the head belongs to.
        head_sha256: The sealed Merkle head, lowercase hex.
        leaf_hash: Domain-separated leaf over the bare head digest.
        tree_size: Log tree size at inclusion.
        audit_path: Sibling hashes from leaf to root.
        signed_tree_head: Tree head plus ``signature_b64``.
        log_public_key: Ed25519 public key, lowercase hex.

    Returns:
        The anchor, ready to :func:`write_anchor`.

    Raises:
        SealAnchorError: When a required field is missing or malformed.
    """
    head = _require_head(head_sha256)
    expected_leaf = transparency_log_leaf(head)
    leaf = _require_hex64(leaf_hash, label="leaf_hash")
    if leaf != expected_leaf:
        msg = f"leaf_hash {leaf} does not match the sealed head {head}"
        raise SealAnchorError(msg)
    if tree_size < 1:
        msg = f"tree_size must be a positive integer, got {tree_size!r}"
        raise SealAnchorError(msg)
    if not isinstance(audit_path, list):
        msg = "audit_path must be a list of inclusion steps"
        raise SealAnchorError(msg)
    if not isinstance(signed_tree_head, dict) or "signature_b64" not in signed_tree_head:
        msg = "signed_tree_head must include signature_b64"
        raise SealAnchorError(msg)
    return SealAnchor(
        run_id=run_id,
        head_sha256=head,
        anchor_kind=ANCHOR_KIND_TRANSPARENCY_LOG,
        leaf_hash=leaf,
        tree_size=tree_size,
        audit_path=audit_path,
        signed_tree_head=signed_tree_head,
        log_public_key=_require_hex64(log_public_key, label="log_public_key"),
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
    if kind not in _SUPPORTED_ANCHOR_KINDS:
        supported = ", ".join(sorted(_SUPPORTED_ANCHOR_KINDS))
        msg = f"unsupported anchor_kind {kind!r} in {path} (this build verifies {supported})"
        raise SealAnchorError(msg)

    if kind == ANCHOR_KIND_TRANSPARENCY_LOG:
        return _load_transparency_log_anchor(path, raw)
    return _load_rfc3161_anchor(path, raw)


def _require_string_field(raw: dict[str, Any], key: str, *, path: Path, default: str | None = None) -> str:
    value = raw.get(key, default)
    if not isinstance(value, str):
        msg = f"anchor record at {path} is missing a string {key!r}"
        raise SealAnchorError(msg)
    return value


def _load_rfc3161_anchor(path: Path, raw: dict[str, Any]) -> SealAnchor:
    """Load a v3.19.2 RFC 3161 record. Log fields are ignored if present."""
    return SealAnchor(
        run_id=_require_string_field(raw, "run_id", path=path),
        head_sha256=_require_head(_require_string_field(raw, "head_sha256", path=path)),
        anchor_kind=ANCHOR_KIND_RFC3161,
        token_b64=_require_string_field(raw, "rfc3161_token_b64", path=path),
        tsa_url=_require_string_field(raw, "rfc3161_tsa_url", path=path, default=""),
    )


def _load_transparency_log_anchor(path: Path, raw: dict[str, Any]) -> SealAnchor:
    """Load a transparency-log record. TSA fields are not required."""
    tree_size = raw.get("tree_size")
    if not isinstance(tree_size, int) or isinstance(tree_size, bool):
        msg = f"anchor record at {path} is missing an integer tree_size"
        raise SealAnchorError(msg)
    audit_path = raw.get("audit_path")
    if not isinstance(audit_path, list):
        msg = f"anchor record at {path} is missing an audit_path list"
        raise SealAnchorError(msg)
    signed_tree_head = raw.get("signed_tree_head")
    if not isinstance(signed_tree_head, dict):
        msg = f"anchor record at {path} is missing a signed_tree_head object"
        raise SealAnchorError(msg)
    try:
        return build_transparency_log_anchor(
            run_id=_require_string_field(raw, "run_id", path=path),
            head_sha256=_require_string_field(raw, "head_sha256", path=path),
            leaf_hash=_require_string_field(raw, "leaf_hash", path=path),
            tree_size=tree_size,
            audit_path=cast("list[dict[str, Any]]", audit_path),
            signed_tree_head=cast("dict[str, Any]", signed_tree_head),
            log_public_key=_require_string_field(raw, "log_public_key", path=path),
        )
    except SealAnchorError as exc:
        msg = f"anchor record at {path}: {exc}"
        raise SealAnchorError(msg) from exc


def verify_anchor(
    anchor: SealAnchor,
    *,
    sealed_head: str,
    trusted_tsa_certs: list[x509.Certificate],
) -> AnchorVerification:
    """Check *anchor* against the head a verifier recomputed, offline.

    Binding is checked first for every kind: the stored head must equal
    ``sealed_head``. After that the path splits on ``anchor_kind``.

    RFC 3161 (unchanged):

    1. **Trust anchors.** Without operator-pinned TSA roots there is nothing
       to chain to; the verdict is ``UNVERIFIABLE``, never a pass.
    2. **Chain.** The token is parsed, chained to those roots, its CMS
       signature checked, and its ``messageImprint`` compared with the head
       digest - all by :func:`~bernstein.core.security.rfc3161_verifier.verify_rfc3161_token`.

    Transparency-log:

    1. Recompute the leaf from the sealed head.
    2. Walk the stored audit path to a root.
    3. Check that root and tree size against the signed tree head.
    4. Verify the tree-head signature with the stored log public key.
       No network, and no TSA fallback.

    Args:
        anchor: The stored anchor.
        sealed_head: The head recomputed from the artifacts on disk.
        trusted_tsa_certs: Operator-pinned TSA roots. Empty means no
            RFC 3161 verdict. Ignored for a transparency-log anchor.

    Returns:
        The verdict and its diagnostics.
    """
    if anchor.head_sha256 != sealed_head:
        return AnchorVerification(
            status=AnchorStatus.MISMATCHED,
            errors=[f"anchor witnesses head {anchor.head_sha256}, artifacts recompute to {sealed_head}"],
        )
    if anchor.anchor_kind == ANCHOR_KIND_TRANSPARENCY_LOG:
        return _verify_transparency_log_anchor(anchor)
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


def _verify_transparency_log_anchor(anchor: SealAnchor) -> AnchorVerification:
    """Recompute the leaf, walk the inclusion proof, verify the tree head."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    from bernstein.core.security.audit_receipt import _root_from_inclusion

    errors: list[str] = []
    if not anchor.leaf_hash or anchor.tree_size is None or anchor.audit_path is None:
        return AnchorVerification(
            status=AnchorStatus.INVALID,
            errors=["transparency-log anchor is missing leaf_hash, tree_size or audit_path"],
        )
    if not anchor.signed_tree_head or not anchor.log_public_key:
        return AnchorVerification(
            status=AnchorStatus.INVALID,
            errors=["transparency-log anchor is missing signed_tree_head or log_public_key"],
        )

    try:
        recomputed_leaf = transparency_log_leaf(anchor.head_sha256)
    except SealAnchorError as exc:
        return AnchorVerification(status=AnchorStatus.INVALID, errors=[str(exc)])
    if recomputed_leaf != anchor.leaf_hash:
        errors.append(
            f"leaf_hash {anchor.leaf_hash} does not recompute from sealed head {anchor.head_sha256}",
        )

    computed_root = _root_from_inclusion(recomputed_leaf, anchor.audit_path)
    sth = anchor.signed_tree_head
    sth_root = sth.get("root_hash")
    sth_size = sth.get("tree_size")
    signature_b64 = sth.get("signature_b64")
    if not isinstance(sth_root, str) or not isinstance(signature_b64, str):
        return AnchorVerification(
            status=AnchorStatus.INVALID,
            errors=["signed_tree_head is missing root_hash or signature_b64"],
        )
    if computed_root != sth_root:
        errors.append(f"inclusion proof recomputes root {computed_root}, signed tree head has {sth_root}")
    if sth_size != anchor.tree_size:
        errors.append(
            f"signed tree head tree_size {sth_size!r} does not match stored tree_size {anchor.tree_size}",
        )

    try:
        signature = base64.b64decode(signature_b64, validate=True)
        public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(anchor.log_public_key))
        public_key.verify(signature, _canonical_sth_bytes(sth))
    except (InvalidSignature, ValueError, binascii.Error) as exc:
        errors.append(f"tree-head signature: {exc}")

    if errors:
        return AnchorVerification(status=AnchorStatus.INVALID, errors=errors)
    return AnchorVerification(status=AnchorStatus.VERIFIED, tree_size=anchor.tree_size)


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
