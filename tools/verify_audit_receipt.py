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
        [--verbose]

Exit codes: ``0`` all enabled checks passed, ``1`` a check failed, ``2`` bad
arguments or unreadable inputs.

By default the verifier trusts the Ed25519 key embedded in the receipt
(``signing.public_key_jwk``) on first use. Pass ``--jwk`` or ``--public-key``
to pin an out-of-band key; the embedded key must then match it.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

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

    return _public_key_from_jwk(embedded_jwk), "trust-on-first-use"


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


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


_VERIFIERS = {
    "cose": verify_cose,
    "intoto": verify_intoto,
    "transparency": verify_transparency,
}


def _print_check(check: CheckResult, *, verbose: bool, stream: io.TextIOBase) -> None:
    """Emit one PASS/FAIL line."""
    status = "PASS" if check.ok else "FAIL"
    line = f"[{status}] {check.name}"
    if check.detail and (not check.ok or verbose):
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
    key_check = CheckResult("public_key", ok=True, detail=note)
    result.checks.append(key_check)
    _print_check(key_check, verbose=verbose, stream=stream)

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

    print(f"OVERALL: {'PASS' if result.ok else 'FAIL'}", file=stream)
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
    parser.add_argument("--verbose", "-v", action="store_true", help="Print PASS-line details.")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns 0 pass, 1 fail, 2 bad args."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.receipt.is_file():
        print(f"ERROR: not a file: {args.receipt}", file=sys.stderr)
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
    )
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
