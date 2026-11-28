#!/usr/bin/env python3
"""Standalone verifier for a bernstein audit receipt.

This script has **zero dependencies on the bernstein package**. Its only
third-party imports are :mod:`cryptography` and :mod:`cbor2` - both are the
off-the-shelf libraries an external auditor already runs to check COSE_Sign1
and CBOR. Everything else is stdlib.

The point of the strict isolation (mirroring ``tools/verify_audit_dsse.py``)
is auditor reproducibility: a regulator can point their own COSE / in-toto /
transparency-log tooling at the receipt and get a yes or no without installing
bernstein and without holding the operator's HMAC key.

What it checks (per format, plus the shared subject binding):

* ``cose``         - COSE_Sign1 (RFC 9052) EdDSA signature over the subject
                     digest, and payload == recomputed chain head.
* ``intoto``       - DSSE / in-toto v1 Statement signature, and the statement
                     subject digest == recomputed chain head.
* ``transparency`` - RFC 6962 style signed tree head signature, Merkle root
                     rebuilt from the embedded range, inclusion proof of the
                     chain-head leaf, and subject binding.

The subject binding is the substrate coupling: the chain ``head_sha256`` is
recomputed from the receipt's embedded event range and every format is asserted
to bind that value. Mutate one embedded entry and the recomputed head diverges,
so every format fails - the receipt is meaningless without the intact chain.

Usage::

    python tools/verify_audit_receipt.py \\
        --receipt /path/to/audit-receipt.json \\
        [--jwk /path/to/trusted.jwk.json] \\
        [--public-key /path/to/ed25519.pub.pem] \\
        [--format cose|intoto|transparency|all] \\
        [--allow-unpinned-key] [--verbose]

Exit codes: ``0`` all enabled checks passed, ``1`` a check failed, ``2`` bad
arguments or unreadable inputs.

Pass ``--jwk`` or ``--public-key`` to pin an out-of-band key; the embedded key
(``signing.public_key_jwk``) must then match it. Without a pin the receipt can
only be checked against the key it carries (trust-on-first-use), which proves
integrity but not who signed it, so the ``public_key`` check fails and the
overall result is FAIL unless ``--allow-unpinned-key`` is given. The key-trust
status is always printed.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import math
import os
import re
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    import io

# Wire-format constants re-declared from the specs / bernstein receipt module.
# Any drift is caught by tests/unit/test_audit_receipt.py (in-tree round-trip)
# and tests/integration/test_standalone_receipt_verifier.py (this script in a
# hermetic venv). DO NOT import from the bernstein package here.
DSSE_PAYLOAD_TYPE = "application/vnd.in-toto+json"
IN_TOTO_STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
RECEIPT_TYPE = "https://bernstein.run/attestations/audit-receipt/v1"
COSE_SIGN1_TAG = 18
COSE_ALG_EDDSA = -8
COSE_LABEL_ALG = 1
EMPTY_TREE_ROOT = hashlib.sha256(b"empty-tree").hexdigest()
VALID_FORMATS = ("cose", "intoto", "transparency")
TOOLCALL_ATTESTATION_EVENT = "toolcall.attestation"
TOOLCALL_DISPATCH_EVENT = "toolcall.enforced_dispatch"
TOOLCALL_EFFECT_EVENT = "toolcall.effect"
TOOLCALL_EFFECT_OUTCOMES = frozenset({"ok", "error", "timeout", "partial"})
_SHA256_PREFIXED_RE = re.compile(r"^sha256:([0-9a-f]{64})\Z")
_GENESIS_PREV_HMAC = "0" * 64

# Key-trust notes returned by ``_resolve_public_key``. Only a key pinned out of
# band proves who signed the receipt; the embedded key proves only that the
# file is internally consistent, because anyone holding the receipt can
# re-sign it under a key of their own.
KEY_TRUST_UNPINNED = "trust-on-first-use"
UNPINNED_KEY_FAIL_DETAIL = (
    "trust-on-first-use: the receipt was checked only against the key it carries, "
    "which does not show who signed it. Pin the signer's key with --public-key or "
    "--jwk, or pass --allow-unpinned-key to accept an integrity-only check."
)
UNPINNED_KEY_PASS_DETAIL = "trust-on-first-use (unpinned key accepted by --allow-unpinned-key; integrity only)"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    """Outcome of a single check."""

    name: str
    ok: bool
    detail: str = ""


@dataclass
class VerifyResult:
    """Aggregate outcome across every requested check."""

    checks: list[CheckResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True iff every check that ran reported success."""
        return all(c.ok for c in self.checks)


# ---------------------------------------------------------------------------
# Canonical primitives - re-implemented from the bernstein receipt spec
# ---------------------------------------------------------------------------


def _canonical_json_bytes(obj: Any) -> bytes:
    """Deterministic JSON bytes (sorted keys, compact separators)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _events_jsonl_bytes(events: list[dict[str, Any]]) -> bytes:
    """Canonical JSONL for the event range (matches audit_multitenant)."""
    if not events:
        return b""
    parts = [json.dumps(e, sort_keys=True, separators=(",", ":")) for e in events]
    return ("\n".join(parts) + "\n").encode("utf-8")


def recompute_head_sha256(events: list[dict[str, Any]]) -> str:
    """Recompute the chain range head from the embedded events."""
    return hashlib.sha256(_events_jsonl_bytes(events)).hexdigest()


def _leaf_digest(data: bytes) -> str:
    """RFC 6962 style leaf hash: ``H(0x00 || data)``."""
    return hashlib.sha256(b"\x00" + data).hexdigest()


def _combine_internal(left: str, right: str) -> str:
    """RFC 6962 style internal node: ``H(0x01 || left || right)``."""
    return hashlib.sha256(b"\x01" + left.encode() + right.encode()).hexdigest()


def _merkle_root(leaves: list[str]) -> str:
    """Merkle root over ``leaves`` (lone odd node promoted unchanged)."""
    if not leaves:
        return EMPTY_TREE_ROOT
    level = list(leaves)
    while len(level) > 1:
        nxt: list[str] = []
        for i in range(0, len(level), 2):
            if i + 1 < len(level):
                nxt.append(_combine_internal(level[i], level[i + 1]))
            else:
                nxt.append(level[i])
        level = nxt
    return level[0]


def _root_from_inclusion(leaf_hash: str, audit_path: list[dict[str, Any]]) -> str:
    """Fold an inclusion proof from the leaf up to the root."""
    node = leaf_hash
    for step in audit_path:
        sibling = str(step.get("hash", ""))
        # ``left`` marks the sibling as the left child, so our node is the right.
        node = _combine_internal(sibling, node) if step.get("left") else _combine_internal(node, sibling)
    return node


def pae(payload_type: str, payload: bytes) -> bytes:
    """DSSE Pre-Authentication Encoding."""
    type_bytes = payload_type.encode("utf-8")
    return (
        b"DSSEv1 "
        + str(len(type_bytes)).encode("ascii")
        + b" "
        + type_bytes
        + b" "
        + str(len(payload)).encode("ascii")
        + b" "
        + payload
    )


# ---------------------------------------------------------------------------
# Key loading
# ---------------------------------------------------------------------------


def _public_key_from_jwk(jwk: dict[str, Any]) -> Any:
    """Convert an OKP/Ed25519 JWK into an Ed25519PublicKey (RFC 8037)."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    if jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519":
        msg = f"expected kty=OKP, crv=Ed25519; got {jwk!r}"
        raise ValueError(msg)
    x = jwk.get("x")
    if not isinstance(x, str):
        msg = "JWK 'x' missing or not a string"
        raise ValueError(msg)
    padding = "=" * (-len(x) % 4)
    raw = base64.urlsafe_b64decode(x + padding)
    if len(raw) != 32:
        msg = f"Ed25519 public key must be 32 bytes (got {len(raw)})"
        raise ValueError(msg)
    return Ed25519PublicKey.from_public_bytes(raw)


def _jwk_x(jwk: dict[str, Any]) -> str:
    """Return the ``x`` member of a JWK for pinning comparison."""
    return str(jwk.get("x", ""))


def _resolve_public_key(
    receipt: dict[str, Any],
    *,
    pinned_jwk: dict[str, Any] | None,
    pinned_pem: bytes | None,
) -> tuple[Any, str]:
    """Resolve the verifying key, honouring an optional pin.

    Returns ``(public_key, note)``. Raises ``ValueError`` when the embedded
    key is absent/invalid or does not match a supplied pin.
    """
    signing = receipt.get("signing") or {}
    embedded_jwk = signing.get("public_key_jwk")
    if not isinstance(embedded_jwk, dict):
        msg = "receipt.signing.public_key_jwk missing or not an object"
        raise ValueError(msg)

    if pinned_pem is not None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        key = serialization.load_pem_public_key(pinned_pem)
        if not isinstance(key, Ed25519PublicKey):
            msg = f"pinned --public-key is not Ed25519 (got {type(key).__name__})"
            raise ValueError(msg)
        # Confirm the embedded JWK matches the pinned PEM.
        embedded_key = _public_key_from_jwk(embedded_jwk)
        raw_pin = key.public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        raw_emb = embedded_key.public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        if raw_pin != raw_emb:
            msg = "embedded receipt key does not match the pinned --public-key"
            raise ValueError(msg)
        return key, "pinned-pem"

    if pinned_jwk is not None:
        if _jwk_x(pinned_jwk) != _jwk_x(embedded_jwk):
            msg = "embedded receipt JWK does not match the pinned --jwk"
            raise ValueError(msg)
        return _public_key_from_jwk(embedded_jwk), "pinned-jwk"

    return _public_key_from_jwk(embedded_jwk), KEY_TRUST_UNPINNED


# ---------------------------------------------------------------------------
# Per-format checks
# ---------------------------------------------------------------------------


def verify_subject_binding(receipt: dict[str, Any]) -> tuple[CheckResult, str]:
    """Recompute head_sha256 from the embedded range; compare to the subject.

    Returns ``(check, recomputed_head)``. The recomputed head is the value
    every format is asserted to bind.
    """
    events = receipt.get("events")
    if not isinstance(events, list):
        return CheckResult("subject_binding", ok=False, detail="receipt.events missing or not a list"), ""
    recomputed = recompute_head_sha256(events)
    subject_digest = str(((receipt.get("subject") or {}).get("digest") or {}).get("sha256", ""))
    range_head = str((receipt.get("range") or {}).get("head_sha256", ""))
    if subject_digest != recomputed:
        return (
            CheckResult(
                "subject_binding",
                ok=False,
                detail=(f"recomputed head {recomputed[:16]}… != subject {subject_digest[:16]}… (chain tampered)"),
            ),
            recomputed,
        )
    if range_head != recomputed:
        return (
            CheckResult(
                "subject_binding",
                ok=False,
                detail=f"recomputed head {recomputed[:16]}… != range.head_sha256 {range_head[:16]}…",
            ),
            recomputed,
        )
    return CheckResult("subject_binding", ok=True, detail=f"head={recomputed[:16]}…"), recomputed


def verify_cose(receipt: dict[str, Any], public_key: Any, recomputed_head: str) -> CheckResult:
    """Verify the COSE_Sign1 signature and its payload binding."""
    import cbor2
    from cryptography.exceptions import InvalidSignature

    block = (receipt.get("formats") or {}).get("cose")
    if not isinstance(block, dict):
        return CheckResult("cose", ok=False, detail="no cose format block present")
    b64 = block.get("cose_sign1_b64")
    if not isinstance(b64, str):
        return CheckResult("cose", ok=False, detail="cose_sign1_b64 missing")
    try:
        cose_bytes = base64.b64decode(b64)
        obj = cbor2.loads(cose_bytes)
    except Exception as exc:
        return CheckResult("cose", ok=False, detail=f"COSE CBOR decode failed: {exc}")

    tag = getattr(obj, "tag", None)
    value = getattr(obj, "value", None)
    # The pure-Python cbor2 decoder yields tuples for CBOR arrays; the C
    # extension yields lists. Accept both.
    if tag != COSE_SIGN1_TAG or not isinstance(value, (list, tuple)) or len(value) != 4:
        return CheckResult("cose", ok=False, detail="not a COSE_Sign1 (tag 18, 4-element array)")
    protected_bstr, _unprotected, payload, signature = value

    try:
        protected_map = cbor2.loads(protected_bstr) if protected_bstr else {}
    except Exception as exc:
        return CheckResult("cose", ok=False, detail=f"protected header decode failed: {exc}")
    if protected_map.get(COSE_LABEL_ALG) != COSE_ALG_EDDSA:
        return CheckResult("cose", ok=False, detail=f"unexpected COSE alg: {protected_map.get(COSE_LABEL_ALG)!r}")

    sig_structure = ["Signature1", protected_bstr, b"", payload]
    to_verify = cbor2.dumps(sig_structure, canonical=True)
    try:
        public_key.verify(signature, to_verify)
    except InvalidSignature:
        return CheckResult("cose", ok=False, detail="COSE_Sign1 signature does not verify")
    except Exception as exc:
        return CheckResult("cose", ok=False, detail=f"COSE verify error: {exc}")

    # Payload binding: the signed payload IS the chain head digest bytes.
    if not isinstance(payload, (bytes, bytearray)):
        return CheckResult("cose", ok=False, detail="COSE payload is not a byte string")
    if bytes(payload).hex() != recomputed_head:
        return CheckResult(
            "cose",
            ok=False,
            detail=(f"COSE payload {bytes(payload).hex()[:16]}… != recomputed head {recomputed_head[:16]}…"),
        )
    return CheckResult("cose", ok=True, detail="COSE_Sign1 EdDSA verified, payload bound to head")


def verify_intoto(receipt: dict[str, Any], public_key: Any, recomputed_head: str) -> CheckResult:
    """Verify the DSSE / in-toto envelope signature and subject binding."""
    from cryptography.exceptions import InvalidSignature

    env = (receipt.get("formats") or {}).get("intoto")
    if not isinstance(env, dict):
        return CheckResult("intoto", ok=False, detail="no intoto format block present")
    payload_type = env.get("payloadType")
    payload_b64 = env.get("payload")
    signatures = env.get("signatures") or []
    if payload_type != DSSE_PAYLOAD_TYPE:
        return CheckResult("intoto", ok=False, detail=f"unexpected payloadType {payload_type!r}")
    if not isinstance(payload_b64, str) or not signatures:
        return CheckResult("intoto", ok=False, detail="envelope missing payload/signatures")
    try:
        payload = base64.b64decode(payload_b64)
    except Exception as exc:
        return CheckResult("intoto", ok=False, detail=f"payload base64 decode failed: {exc}")

    pae_bytes = pae(payload_type, payload)
    verified = False
    for sig_obj in signatures:
        if not isinstance(sig_obj, dict):
            continue
        try:
            sig = base64.b64decode(sig_obj.get("sig", ""))
            public_key.verify(sig, pae_bytes)
            verified = True
            break
        except (InvalidSignature, ValueError, TypeError):
            continue
    if not verified:
        return CheckResult("intoto", ok=False, detail="no DSSE signature verified")

    try:
        statement = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return CheckResult("intoto", ok=False, detail=f"statement JSON decode failed: {exc}")
    if statement.get("_type") != IN_TOTO_STATEMENT_TYPE:
        return CheckResult("intoto", ok=False, detail=f"unexpected statement _type {statement.get('_type')!r}")
    subjects = statement.get("subject") or []
    digests = [str((s.get("digest") or {}).get("sha256", "")) for s in subjects if isinstance(s, dict)]
    if recomputed_head not in digests:
        return CheckResult(
            "intoto",
            ok=False,
            detail=(f"recomputed head {recomputed_head[:16]}… not in statement subject digests"),
        )
    return CheckResult("intoto", ok=True, detail="DSSE/in-toto verified, subject bound to head")


def verify_transparency(receipt: dict[str, Any], public_key: Any, recomputed_head: str) -> CheckResult:
    """Verify the transparency STH signature, Merkle root, and inclusion proof."""
    from cryptography.exceptions import InvalidSignature

    block = (receipt.get("formats") or {}).get("transparency")
    if not isinstance(block, dict):
        return CheckResult("transparency", ok=False, detail="no transparency format block present")
    sth = block.get("signed_tree_head") or {}
    proof = block.get("inclusion_proof") or {}
    root_hash = str(sth.get("root_hash", ""))
    subject_sha256 = str(sth.get("subject_sha256", ""))
    tree_size = sth.get("tree_size")

    # 1. STH signature over the canonical signed-tree-head fields.
    sig_b64 = sth.get("signature_b64")
    if not isinstance(sig_b64, str):
        return CheckResult("transparency", ok=False, detail="signed_tree_head.signature_b64 missing")
    signed = {
        "tree_size": tree_size,
        "root_hash": root_hash,
        "subject_sha256": subject_sha256,
    }
    try:
        public_key.verify(base64.b64decode(sig_b64), _canonical_json_bytes(signed))
    except InvalidSignature:
        return CheckResult("transparency", ok=False, detail="signed tree head signature does not verify")
    except Exception as exc:
        return CheckResult("transparency", ok=False, detail=f"STH verify error: {exc}")

    # 2. Subject binding.
    if subject_sha256 != recomputed_head:
        return CheckResult(
            "transparency",
            ok=False,
            detail=(f"STH subject {subject_sha256[:16]}… != recomputed head {recomputed_head[:16]}…"),
        )

    # 3. Rebuild the Merkle root from the embedded range and compare.
    events = receipt.get("events") or []
    leaves = [_leaf_digest(_canonical_json_bytes(e)) for e in events]
    if len(leaves) != tree_size:
        return CheckResult(
            "transparency",
            ok=False,
            detail=f"tree_size {tree_size} != embedded leaf count {len(leaves)}",
        )
    rebuilt_root = _merkle_root(leaves)
    if rebuilt_root != root_hash:
        return CheckResult(
            "transparency",
            ok=False,
            detail=f"rebuilt Merkle root {rebuilt_root[:16]}… != STH root {root_hash[:16]}… (chain tampered)",
        )

    # 4. Inclusion proof of the chain-head (last) leaf.
    if leaves:
        leaf_index = proof.get("leaf_index")
        leaf_hash = str(proof.get("leaf_hash", ""))
        audit_path = proof.get("audit_path") or []
        if leaf_index != len(leaves) - 1 or leaf_hash != leaves[-1]:
            return CheckResult("transparency", ok=False, detail="inclusion proof does not target the chain-head leaf")
        proven_root = _root_from_inclusion(leaf_hash, audit_path)
        if proven_root != root_hash:
            return CheckResult(
                "transparency",
                ok=False,
                detail=f"inclusion proof root {proven_root[:16]}… != STH root {root_hash[:16]}…",
            )
    return CheckResult("transparency", ok=True, detail="RFC6962 STH signed, root + inclusion proof verified")


def _read_regular_effect_file(
    effects_dir: Path,
    digest_hex: str,
    intent_digest: str,
) -> tuple[bytes | None, str | None]:
    """Read ``<effects_dir>/<digest_hex>.effect`` rejecting symlinks and non-regular files."""
    artifact_path = effects_dir / f"{digest_hex}.effect"
    intent_short = intent_digest[:19]
    try:
        lstat_res = os.lstat(artifact_path)
    except FileNotFoundError:
        return None, f"effect payload missing in --effects-dir for intent {intent_short}"
    except OSError as exc:
        return None, f"effect payload in --effects-dir unreadable for intent {intent_short}: {exc}"

    if stat.S_ISLNK(lstat_res.st_mode):
        return None, f"effect payload in --effects-dir must not be a symlink for intent {intent_short}"
    if not stat.S_ISREG(lstat_res.st_mode):
        return None, f"effect payload in --effects-dir is not a regular file for intent {intent_short}"

    flags = (
        getattr(os, "O_RDONLY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0)
    )
    try:
        fd = os.open(artifact_path, flags)
    except OSError as exc:
        return None, f"effect payload in --effects-dir unreadable or symlink for intent {intent_short}: {exc}"

    try:
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            fstat_res = os.fstat(handle.fileno())
            if not stat.S_ISREG(fstat_res.st_mode):
                return None, f"effect payload in --effects-dir is not a regular file for intent {intent_short}"
            if (fstat_res.st_dev, fstat_res.st_ino) != (lstat_res.st_dev, lstat_res.st_ino):
                return (
                    None,
                    f"effect payload in --effects-dir changed between stat and open for intent {intent_short}",
                )
            return handle.read(), None
    except OSError as exc:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)
        return None, f"effect payload in --effects-dir unreadable for intent {intent_short}: {exc}"


def _request_id_key(raw_request_id: Any) -> str | None:
    """Return a canonical JSON key for duplicate detection, or ``None`` when absent/blank."""
    if raw_request_id is None:
        return None
    if isinstance(raw_request_id, str) and not raw_request_id.strip():
        return None
    try:
        return json.dumps(raw_request_id, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        return repr(raw_request_id)


def _range_starts_at_genesis(receipt: dict[str, Any], events: list[Any]) -> bool:
    """Return ``True`` when the receipt's embedded range starts at the genesis sentinel."""
    for raw_event in events:
        if isinstance(raw_event, dict):
            typed_event = cast("dict[str, Any]", raw_event)
            prev_val = typed_event.get("prev_hmac")
            if isinstance(prev_val, str):
                return prev_val == _GENESIS_PREV_HMAC
            break
    raw_range = receipt.get("range")
    if isinstance(raw_range, dict):
        typed_range = cast("dict[str, Any]", raw_range)
        return typed_range.get("genesis_prev_hmac") == _GENESIS_PREV_HMAC
    return False


def verify_toolcall_effects(
    receipt: dict[str, Any],
    *,
    effects_dir: Path | None = None,
) -> tuple[CheckResult, list[str]]:
    """Verify tool-call decision -> dispatch -> effect triples and retry_of links (#6270).

    Pre-effect chains (with no ``toolcall.effect`` rows, or no tool-call events
    at all) remain valid and report ``effect unobserved``.  Broken bindings,
    duplicate dispatches/effects, malformed records, invalid ``retry_of`` links,
    or tampered/missing payload artifacts under ``effects_dir`` fail the effect
    leg.  Duplicate-request detection enforces the contract that JSON-RPC
    ``request_id`` values are unique per ``scope_id`` (keyed by raw JSON value).
    """
    events = receipt.get("events")
    if not isinstance(events, list):
        return CheckResult("toolcall_effects", ok=False, detail="receipt.events missing or not a list"), []
    typed_events = cast("list[Any]", events)

    attestations: dict[str, dict[str, Any]] = {}
    attestations_by_intent: dict[str, dict[str, Any]] = {}
    seen_scope_requests: set[tuple[str, str]] = set()
    dispatches: dict[str, dict[str, Any]] = {}
    effects: dict[str, dict[str, Any]] = {}
    ordered_refs: list[str] = []
    errors: list[str] = []
    per_ref_errors: dict[str, list[str]] = {}
    saw_toolcall = False

    for raw_event in typed_events:
        if not isinstance(raw_event, dict):
            continue
        event = cast("dict[str, Any]", raw_event)
        event_type = str(event.get("event_type", ""))
        raw_details = event.get("details")
        payload: dict[str, Any] = cast("dict[str, Any]", raw_details) if isinstance(raw_details, dict) else {}

        if event_type == TOOLCALL_ATTESTATION_EVENT:
            saw_toolcall = True
            raw_att_ref = payload.get("attestation_ref")
            raw_intent_digest = payload.get("intent_digest")
            att_ref = raw_att_ref.strip() if isinstance(raw_att_ref, str) else ""
            intent_digest = raw_intent_digest.strip() if isinstance(raw_intent_digest, str) else ""
            raw_req_id = payload.get("request_id")
            req_id = str(raw_req_id).strip() if raw_req_id is not None else ""
            req_id_key = _request_id_key(raw_req_id)
            scope_id = str(payload.get("scope_id", event.get("resource_id", ""))).strip()
            server_name = str(payload.get("server_name", "")).strip()
            tool_name = str(payload.get("tool_name", "")).strip()
            raw_retry_of = payload.get("retry_of")

            if not att_ref or not intent_digest:
                errors.append("toolcall.attestation missing attestation_ref or intent_digest")
                continue
            if att_ref in attestations:
                msg = f"duplicate toolcall.attestation for {att_ref[:19]}"
                errors.append(msg)
                per_ref_errors.setdefault(att_ref, []).append(msg)
                continue

            if not isinstance(raw_intent_digest, str) or _SHA256_PREFIXED_RE.match(raw_intent_digest) is None:
                msg = f"toolcall.attestation malformed intent_digest for {att_ref[:19]}"
                errors.append(msg)
                per_ref_errors.setdefault(att_ref, []).append(msg)
            elif intent_digest in attestations_by_intent:
                msg = f"duplicate intent_digest {intent_digest[:19]} on attestation {att_ref[:19]}"
                errors.append(msg)
                per_ref_errors.setdefault(att_ref, []).append(msg)

            if req_id_key is not None:
                scope_req_key = (scope_id, req_id_key)
                if scope_req_key in seen_scope_requests:
                    msg = f"duplicate request_id {raw_req_id!r} in scope {scope_id!r} on attestation {att_ref[:19]}"
                    errors.append(msg)
                    per_ref_errors.setdefault(att_ref, []).append(msg)
                seen_scope_requests.add(scope_req_key)

            retry_of: str | None = None
            retry_status: str | None = None
            if raw_retry_of is not None:
                if not isinstance(raw_retry_of, str) or _SHA256_PREFIXED_RE.match(raw_retry_of) is None:
                    msg = f"invalid retry_of on attestation {att_ref[:19]}"
                    errors.append(msg)
                    per_ref_errors.setdefault(att_ref, []).append(msg)
                else:
                    retry_of = raw_retry_of
                    if retry_of == intent_digest:
                        msg = f"self-referential retry_of on intent {intent_digest[:19]}"
                        errors.append(msg)
                        per_ref_errors.setdefault(att_ref, []).append(msg)
                    elif retry_of in attestations_by_intent:
                        prior = attestations_by_intent[retry_of]
                        retry_status = "in-range"
                        if (
                            req_id_key is not None
                            and prior["request_id_key"] is not None
                            and req_id_key == prior["request_id_key"]
                        ):
                            msg = f"retry {intent_digest[:19]} reuses prior request_id {raw_req_id!r}"
                            errors.append(msg)
                            per_ref_errors.setdefault(att_ref, []).append(msg)
                        current_target = (scope_id, server_name, tool_name)
                        prior_target = (prior["scope_id"], prior["server_name"], prior["tool_name"])
                        if not all(current_target) or current_target != prior_target:
                            msg = (
                                f"retry {intent_digest[:19]} target {current_target!r} does not match "
                                f"prior target {prior_target!r}"
                            )
                            errors.append(msg)
                            per_ref_errors.setdefault(att_ref, []).append(msg)
                    else:
                        retry_status = "outside-range"

            entry = {
                "attestation_ref": att_ref,
                "intent_digest": intent_digest,
                "request_id": req_id,
                "request_id_key": req_id_key,
                "scope_id": scope_id,
                "server_name": server_name,
                "tool_name": tool_name,
                "retry_of": retry_of,
                "retry_status": retry_status,
            }
            attestations[att_ref] = entry
            attestations_by_intent.setdefault(intent_digest, entry)
            ordered_refs.append(att_ref)
            continue

        if event_type == TOOLCALL_DISPATCH_EVENT:
            saw_toolcall = True
            att_ref = str(payload.get("attestation_ref", "")).strip()
            intent_digest = str(payload.get("intent_digest", "")).strip()
            if not att_ref or att_ref not in attestations:
                errors.append(f"toolcall.enforced_dispatch without preceding attestation ({att_ref[:19] or 'missing'})")
                continue
            if intent_digest != attestations[att_ref]["intent_digest"]:
                msg = f"toolcall.enforced_dispatch intent_digest mismatch for {att_ref[:19]}"
                errors.append(msg)
                per_ref_errors.setdefault(att_ref, []).append(msg)
                continue
            if att_ref in dispatches:
                msg = f"duplicate toolcall.enforced_dispatch for {att_ref[:19]} (violates one-attestation-one-dispatch)"
                errors.append(msg)
                per_ref_errors.setdefault(att_ref, []).append(msg)
                continue
            orig_hmac = str(payload.get("_original_hmac", "")).strip()
            event_hmac = str(event.get("hmac", "")).strip()
            expected_handle = f"hmac:{orig_hmac}" if orig_hmac else (f"hmac:{event_hmac}" if event_hmac else "")
            dispatches[att_ref] = {"expected_handle": expected_handle, "intent_digest": intent_digest}
            continue

        if event_type == TOOLCALL_EFFECT_EVENT:
            saw_toolcall = True
            att_ref = str(payload.get("attestation_ref", "")).strip()
            intent_digest = str(payload.get("intent_digest", "")).strip()
            dispatch_ref = str(payload.get("dispatch_ref", "")).strip()
            raw_effect_digest = payload.get("effect_digest")
            effect_digest = raw_effect_digest.strip() if isinstance(raw_effect_digest, str) else ""
            outcome = payload.get("outcome")
            duration_ms = payload.get("duration_ms")

            if not att_ref or att_ref not in dispatches:
                msg = f"toolcall.effect without preceding toolcall.enforced_dispatch ({att_ref[:19] or 'missing'})"
                errors.append(msg)
                if att_ref:
                    per_ref_errors.setdefault(att_ref, []).append(msg)
                continue
            if att_ref in effects:
                msg = f"duplicate toolcall.effect for {att_ref[:19]}"
                errors.append(msg)
                per_ref_errors.setdefault(att_ref, []).append(msg)
                continue

            eff_errs: list[str] = []
            if intent_digest != attestations[att_ref]["intent_digest"]:
                eff_errs.append(f"toolcall.effect intent_digest mismatch for {att_ref[:19]}")
            expected_handle = dispatches[att_ref]["expected_handle"]
            if expected_handle:
                if dispatch_ref != expected_handle:
                    eff_errs.append(f"toolcall.effect dispatch_ref mismatch for {att_ref[:19]}")
            elif not dispatch_ref:
                eff_errs.append(f"toolcall.effect missing dispatch_ref for {att_ref[:19]}")
            if not isinstance(outcome, str) or outcome not in TOOLCALL_EFFECT_OUTCOMES:
                eff_errs.append(f"toolcall.effect invalid outcome {outcome!r} for {att_ref[:19]}")
            if not isinstance(raw_effect_digest, str) or not _SHA256_PREFIXED_RE.match(raw_effect_digest):
                eff_errs.append(f"toolcall.effect malformed effect_digest for {att_ref[:19]}")
            if (
                isinstance(duration_ms, bool)
                or not isinstance(duration_ms, (int, float))
                or not math.isfinite(duration_ms)
                or duration_ms < 0
            ):
                eff_errs.append(f"toolcall.effect invalid duration_ms for {att_ref[:19]}")

            payload_verified = False
            if effects_dir is not None and not eff_errs and not per_ref_errors.get(att_ref):
                match = _SHA256_PREFIXED_RE.match(attestations[att_ref]["intent_digest"])
                if match is None:
                    eff_errs.append(
                        f"toolcall.effect malformed intent_digest for payload lookup ({intent_digest[:19]})"
                    )
                else:
                    raw_bytes, read_err = _read_regular_effect_file(effects_dir, match.group(1), intent_digest)
                    if read_err is not None or raw_bytes is None:
                        eff_errs.append(
                            read_err or f"effect payload missing in --effects-dir for intent {intent_digest[:19]}"
                        )
                    else:
                        recomputed = "sha256:" + hashlib.sha256(raw_bytes).hexdigest()
                        if recomputed != effect_digest:
                            eff_errs.append(
                                f"effect_digest mismatch for intent {intent_digest[:19]}: "
                                f"recomputed {recomputed[:19]} != recorded {effect_digest[:19]}"
                            )
                        else:
                            payload_verified = True

            if eff_errs:
                errors.extend(eff_errs)
                per_ref_errors.setdefault(att_ref, []).extend(eff_errs)
            effects[att_ref] = {
                "effect_digest": effect_digest,
                "outcome": outcome,
                "duration_ms": duration_ms,
                "payload_verified": payload_verified,
            }

    starts_at_genesis = _range_starts_at_genesis(receipt, typed_events)
    for att_ref in ordered_refs:
        att = attestations[att_ref]
        if att["retry_of"] is not None and att["retry_status"] == "outside-range":
            if att["retry_of"] in attestations_by_intent:
                msg = f"retry_of on {att['intent_digest'][:19]} points forward to a later attestation"
                errors.append(msg)
                per_ref_errors.setdefault(att_ref, []).append(msg)
            elif starts_at_genesis:
                msg = (
                    f"retry_of on {att['intent_digest'][:19]} references missing prior intent "
                    f"{att['retry_of'][:19]} in receipt starting at genesis"
                )
                errors.append(msg)
                per_ref_errors.setdefault(att_ref, []).append(msg)

    if not saw_toolcall:
        return (
            CheckResult(
                "toolcall_effects",
                ok=True,
                detail="no toolcall effect records in range (effect unobserved)",
            ),
            [],
        )

    closed_count = 0
    unobserved_count = 0
    detail_lines: list[str] = []
    for att_ref in ordered_refs:
        att = attestations[att_ref]
        intent_short = att["intent_digest"][:19]
        att_short = att_ref[:19]
        req_id = att["request_id"]
        ref_errs = per_ref_errors.get(att_ref, [])
        if ref_errs:
            detail_lines.append(
                f"  toolcall attestation={att_short} intent={intent_short} request_id={req_id} "
                f"status=FAIL reason={'; '.join(ref_errs)}"
            )
        elif att_ref in effects:
            closed_count += 1
            eff = effects[att_ref]
            payload_note = " payload=verified" if eff["payload_verified"] else ""
            detail_lines.append(
                f"  toolcall attestation={att_short} intent={intent_short} request_id={req_id} "
                f"status=closed outcome={eff['outcome']} effect_digest={eff['effect_digest'][:19]}{payload_note}"
            )
        elif att_ref in dispatches:
            unobserved_count += 1
            detail_lines.append(
                f"  toolcall attestation={att_short} intent={intent_short} request_id={req_id} "
                f'status="dispatched, effect unobserved"'
            )
        else:
            unobserved_count += 1
            detail_lines.append(
                f"  toolcall attestation={att_short} intent={intent_short} request_id={req_id} "
                f'status="attested, effect unobserved"'
            )
        if att["retry_of"] is not None:
            scope_note = "in-range" if att["retry_status"] == "in-range" else "prior intent outside range"
            detail_lines.append(
                f"  toolcall retry intent={intent_short} retry_of={att['retry_of'][:19]} "
                f"request_id={req_id} ({scope_note})"
            )

    total = len(ordered_refs)
    if errors:
        summary = (
            f"{closed_count}/{total} closed, {unobserved_count} effect unobserved, "
            f"{len(errors)} error(s): {'; '.join(errors[:3])}"
        )
        return CheckResult("toolcall_effects", ok=False, detail=summary), detail_lines

    parts = [f"{closed_count}/{total} closed"]
    if unobserved_count:
        parts.append(f"{unobserved_count} effect unobserved")
    if effects_dir is not None and closed_count > 0:
        parts.append("payload digests verified")
    return CheckResult("toolcall_effects", ok=True, detail=", ".join(parts)), detail_lines


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


_VERIFIERS = {
    "cose": verify_cose,
    "intoto": verify_intoto,
    "transparency": verify_transparency,
}


def _print_check(check: CheckResult, *, verbose: bool, always_detail: bool = False, stream: io.TextIOBase) -> None:
    """Emit one PASS/FAIL line."""
    status = "PASS" if check.ok else "FAIL"
    line = f"[{status}] {check.name}"
    if check.detail and (not check.ok or verbose or always_detail):
        line += f" - {check.detail}"
    print(line, file=stream)


def run_verify(
    *,
    receipt_path: Path,
    which: str,
    pinned_jwk: dict[str, Any] | None,
    pinned_pem: bytes | None,
    verbose: bool,
    stream: io.TextIOBase,
    allow_unpinned_key: bool = False,
    effects_dir: Path | None = None,
) -> VerifyResult:
    """Run every enabled check and emit human-readable output."""
    result = VerifyResult()

    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        check = CheckResult("receipt_load", ok=False, detail=str(exc))
        result.checks.append(check)
        _print_check(check, verbose=verbose, stream=stream)
        return result

    try:
        public_key, note = _resolve_public_key(receipt, pinned_jwk=pinned_jwk, pinned_pem=pinned_pem)
    except ValueError as exc:
        check = CheckResult("public_key", ok=False, detail=str(exc))
        result.checks.append(check)
        _print_check(check, verbose=verbose, stream=stream)
        return result
    # An unpinned key is a failing check unless the caller opted in, and the
    # key-trust status is always printed so a PASS is never read as provenance.
    unpinned = note == KEY_TRUST_UNPINNED
    if unpinned and not allow_unpinned_key:
        key_check = CheckResult("public_key", ok=False, detail=UNPINNED_KEY_FAIL_DETAIL)
    elif unpinned:
        key_check = CheckResult("public_key", ok=True, detail=UNPINNED_KEY_PASS_DETAIL)
    else:
        key_check = CheckResult("public_key", ok=True, detail=note)
    result.checks.append(key_check)
    _print_check(key_check, verbose=verbose, stream=stream, always_detail=True)

    binding_check, recomputed_head = verify_subject_binding(receipt)
    result.checks.append(binding_check)
    _print_check(binding_check, verbose=verbose, stream=stream)

    present = list((receipt.get("formats") or {}).keys())
    targets = VALID_FORMATS if which == "all" else (which,)
    ran_any = False
    for fmt in targets:
        if fmt not in present:
            if which != "all":
                check = CheckResult(fmt, ok=False, detail="requested format not present in receipt")
                result.checks.append(check)
                _print_check(check, verbose=verbose, stream=stream)
            continue
        ran_any = True
        check = _VERIFIERS[fmt](receipt, public_key, recomputed_head)
        result.checks.append(check)
        _print_check(check, verbose=verbose, stream=stream)

    if which == "all" and not ran_any:
        check = CheckResult("formats", ok=False, detail="receipt carries no recognised formats")
        result.checks.append(check)
        _print_check(check, verbose=verbose, stream=stream)

    effects_check, effect_lines = verify_toolcall_effects(receipt, effects_dir=effects_dir)
    result.checks.append(effects_check)
    _print_check(effects_check, verbose=verbose, stream=stream, always_detail=True)
    for line in effect_lines:
        print(line, file=stream)

    overall = "PASS" if result.ok else "FAIL"
    if result.ok and unpinned:
        overall += " (unpinned key: integrity only)"
    print(f"OVERALL: {overall}", file=stream)
    return result


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser."""
    parser = argparse.ArgumentParser(
        description="Verify a bernstein audit receipt without importing the bernstein package.",
    )
    parser.add_argument("--receipt", required=True, type=Path, help="Path to the audit receipt JSON.")
    parser.add_argument(
        "--jwk",
        type=Path,
        default=None,
        help="Optional trusted Ed25519 JWK (OKP) to pin; embedded key must match.",
    )
    parser.add_argument(
        "--public-key",
        type=Path,
        default=None,
        help="Optional trusted Ed25519 public key PEM to pin; embedded key must match.",
    )
    parser.add_argument(
        "--format",
        choices=[*VALID_FORMATS, "all"],
        default="all",
        help="Which format(s) to verify (default: all present).",
    )
    parser.add_argument(
        "--allow-unpinned-key",
        action="store_true",
        help=(
            "Accept a receipt checked only against its embedded key (trust-on-first-use). "
            "Without a pinned --public-key/--jwk this proves integrity, not who signed it; "
            "the default is to report such a receipt as FAIL."
        ),
    )
    parser.add_argument(
        "--effects-dir",
        type=Path,
        default=None,
        help=(
            "Optional directory of recorded tool-call effect payloads (<intent_sha256>.effect) "
            "used to recompute and verify toolcall.effect effect_digest values offline."
        ),
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Print PASS-line details.")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns 0 pass, 1 fail, 2 bad args."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.receipt.is_file():
        print(f"ERROR: not a file: {args.receipt}", file=sys.stderr)
        return 2

    if args.effects_dir is not None and not args.effects_dir.is_dir():
        print(f"ERROR: not a directory: {args.effects_dir}", file=sys.stderr)
        return 2

    pinned_jwk: dict[str, Any] | None = None
    if args.jwk is not None:
        if not args.jwk.is_file():
            print(f"ERROR: not a file: {args.jwk}", file=sys.stderr)
            return 2
        try:
            pinned_jwk = json.loads(args.jwk.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"ERROR: cannot read --jwk: {exc}", file=sys.stderr)
            return 2
        if not isinstance(pinned_jwk, dict):
            print("ERROR: --jwk must be a JSON object", file=sys.stderr)
            return 2

    pinned_pem: bytes | None = None
    if args.public_key is not None:
        if not args.public_key.is_file():
            print(f"ERROR: not a file: {args.public_key}", file=sys.stderr)
            return 2
        pinned_pem = args.public_key.read_bytes()

    result = run_verify(
        receipt_path=args.receipt,
        which=args.format,
        pinned_jwk=pinned_jwk,
        pinned_pem=pinned_pem,
        verbose=args.verbose,
        stream=sys.stdout,
        allow_unpinned_key=args.allow_unpinned_key,
        effects_dir=args.effects_dir,
    )
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
