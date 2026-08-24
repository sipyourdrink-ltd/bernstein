#!/usr/bin/env python3
"""Standalone verifier for a DSSE-wrapped bernstein audit bundle.

This script intentionally has **zero dependencies on the bernstein package**.
The only third-party import is :mod:`cryptography` (already pulled in by any
modern Python tooling stack). Everything else is stdlib.

The reason for the strict isolation is auditor reproducibility:

* The previous "standalone verifier" imported
  ``bernstein.core.security.article12_bundle``. An auditor handed that script
  could not run it without the entire bernstein source tree on PYTHONPATH,
  which defeats the point.
* The DSSE / in-toto / HMAC formats are open standards. A verifier should be
  re-implementable from the spec alone, and this script is the proof.
* Tests at ``tests/integration/test_standalone_verifier.py`` run this module
  in a venv that has only stdlib + ``cryptography`` installed; if anyone ever
  adds a ``from bernstein...`` import here the test fails.

Usage::

    python tools/verify_audit_dsse.py \\
        --envelope /path/to/audit.dsse.json \\
        --bundle /path/to/article12_xxx.zip \\
        --public-key /path/to/audit.pub.pem \\
        [--hmac-key /path/to/audit.key] \\
        [--verbose]

Exit codes:

* 0 - all enabled checks passed.
* 1 - at least one check failed (details printed).
* 2 - bad CLI arguments or unreadable inputs.

The HMAC chain check is **opt-in**: an external regulator typically does not
hold the operator's HMAC key, so they verify the envelope signature + bundle
hash and stop there. Operators with the key can pass ``--hmac-key`` to add
the chain walk on top.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

# We re-implement the wire-format constants from the DSSE / in-toto specs and
# the bernstein audit module. Any drift between this file and
# ``src/bernstein/core/security/audit_dsse.py`` is caught by the dedicated
# round-trip test (``tests/unit/test_audit_dsse.py``). DO NOT replace these
# with imports from the bernstein package - see the module docstring above.

# Scheme versions matching src/bernstein/core/security/key_derivation.py
SCHEME_V1 = 1
SCHEME_V2 = 2

# Domain tags matching src/bernstein/core/security/key_derivation.py
DOMAIN_AUDIT = "audit"
_TAG_PREFIX = "bernstein"

DSSE_PAYLOAD_TYPE = "application/vnd.in-toto+json"
IN_TOTO_STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
BERNSTEIN_AUDIT_PREDICATE_TYPE = "https://bernstein.run/attestations/audit/v1"
GENESIS_HMAC = "0" * 64


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    """Outcome of a single check (envelope, hash, chain)."""

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
# HKDF key derivation - matches src/bernstein/core/security/key_derivation.py
# ---------------------------------------------------------------------------


def derive_store_key(master_key: bytes, domain: str) -> bytes:
    """Derive a deterministic 32-byte per-store HMAC key via HKDF-SHA256.

    Matches :func:`bernstein.core.security.key_derivation.derive_store_key`.
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=domain.encode("utf-8"),
    )
    return hkdf.derive(master_key)


def domain_tag(domain: str, version: int = SCHEME_V2) -> str:
    """Return the versioned domain tag string used to prefix chain preimages.

    Matches :func:`bernstein.core.security.key_derivation.domain_tag`.
    """
    if version == SCHEME_V1:
        return ""
    return f"{_TAG_PREFIX}:{domain}:v{version}"


# ---------------------------------------------------------------------------
# DSSE primitives - re-implemented from the spec (no bernstein import)
# ---------------------------------------------------------------------------


def pae(payload_type: str, payload: bytes) -> bytes:
    """Compute the DSSE Pre-Authentication Encoding for a payload."""
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


def load_envelope(path: Path) -> dict[str, Any]:
    """Read a JSON envelope from disk."""
    return json.loads(path.read_text(encoding="utf-8"))


def verify_envelope_signature(
    envelope: dict[str, Any],
    public_key_pem: bytes,
) -> CheckResult:
    """Verify an Ed25519 signature over the DSSE PAE input.

    Returns a :class:`CheckResult`; never raises on bad input - the calling
    CLI prints the message and exits with the right status.
    """
    payload_type = envelope.get("payloadType")
    payload_b64 = envelope.get("payload")
    signatures = envelope.get("signatures") or []
    if not isinstance(payload_type, str) or not isinstance(payload_b64, str) or not signatures:
        return CheckResult(
            name="envelope_format",
            ok=False,
            detail="envelope is missing payload / payloadType / signatures",
        )

    if payload_type != DSSE_PAYLOAD_TYPE:
        return CheckResult(
            name="envelope_payload_type",
            ok=False,
            detail=f"expected {DSSE_PAYLOAD_TYPE!r}, got {payload_type!r}",
        )

    try:
        payload = base64.b64decode(payload_b64)
    except (ValueError, base64.binascii.Error) as exc:  # type: ignore[attr-defined]
        return CheckResult(
            name="envelope_payload_b64",
            ok=False,
            detail=f"payload base64 decode failed: {exc}",
        )

    pae_bytes = pae(payload_type, payload)

    # Local import keeps the script importable in environments without
    # cryptography for the bare ``--help`` path.
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        public_key = serialization.load_pem_public_key(public_key_pem)
    except (ValueError, TypeError) as exc:
        return CheckResult(
            name="public_key_parse",
            ok=False,
            detail=f"failed to parse public key PEM: {exc}",
        )
    if not isinstance(public_key, Ed25519PublicKey):
        return CheckResult(
            name="public_key_type",
            ok=False,
            detail=f"expected Ed25519 public key, got {type(public_key).__name__}",
        )

    last_error = ""
    for sig_obj in signatures:
        if not isinstance(sig_obj, dict):
            continue
        try:
            sig = base64.b64decode(sig_obj.get("sig", ""))
        except (ValueError, base64.binascii.Error) as exc:  # type: ignore[attr-defined]
            last_error = f"signature base64 decode failed: {exc}"
            continue
        try:
            public_key.verify(sig, pae_bytes)
        except Exception as exc:
            last_error = f"signature verify failed (keyid={sig_obj.get('keyid', '')!r}): {exc}"
            continue
        return CheckResult(name="envelope_signature", ok=True)

    return CheckResult(
        name="envelope_signature",
        ok=False,
        detail=last_error or "no signatures verified",
    )


def verify_statement_types(envelope: dict[str, Any]) -> CheckResult:
    """Confirm the embedded in-toto statement has the expected types."""
    try:
        payload = base64.b64decode(envelope.get("payload", ""))
    except (ValueError, base64.binascii.Error) as exc:  # type: ignore[attr-defined]
        return CheckResult(name="statement_payload_b64", ok=False, detail=str(exc))
    try:
        statement = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return CheckResult(name="statement_json", ok=False, detail=str(exc))

    if statement.get("_type") != IN_TOTO_STATEMENT_TYPE:
        return CheckResult(
            name="statement_type",
            ok=False,
            detail=f"expected _type={IN_TOTO_STATEMENT_TYPE!r}, got {statement.get('_type')!r}",
        )
    if statement.get("predicateType") != BERNSTEIN_AUDIT_PREDICATE_TYPE:
        return CheckResult(
            name="statement_predicate_type",
            ok=False,
            detail=(
                f"expected predicateType={BERNSTEIN_AUDIT_PREDICATE_TYPE!r}, got {statement.get('predicateType')!r}"
            ),
        )
    return CheckResult(name="statement_types", ok=True)


def verify_subject_digest(
    envelope: dict[str, Any],
    bundle_path: Path,
) -> CheckResult:
    """Confirm the in-toto subject digest matches the on-disk bundle hash."""
    try:
        payload = base64.b64decode(envelope.get("payload", ""))
        statement = json.loads(payload.decode("utf-8"))
    except (ValueError, base64.binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:  # type: ignore[attr-defined]
        return CheckResult(name="subject_payload", ok=False, detail=str(exc))

    subjects = statement.get("subject") or []
    if not subjects:
        return CheckResult(name="subject", ok=False, detail="statement has no subject")

    bundle_bytes = bundle_path.read_bytes()
    actual = hashlib.sha256(bundle_bytes).hexdigest()
    for sub in subjects:
        digest = (sub.get("digest") or {}).get("sha256")
        if digest == actual:
            return CheckResult(name="subject_sha256", ok=True)

    return CheckResult(
        name="subject_sha256",
        ok=False,
        detail=f"bundle sha256 {actual!r} does not match any subject digest in the statement",
    )


# ---------------------------------------------------------------------------
# HMAC chain walk - re-implemented from the audit module spec
# ---------------------------------------------------------------------------


def _canonical_chain_payload(prev_hmac: str, entry: dict[str, Any]) -> bytes:
    """Match :func:`bernstein.core.security.audit._compute_hmac` exactly."""
    return (prev_hmac + json.dumps(entry, sort_keys=True)).encode("utf-8")


def verify_hmac_chain(bundle_path: Path, hmac_key: bytes) -> CheckResult:
    """Walk every event in the bundle's ``events.jsonl`` and verify HMAC links.

    Args:
        bundle_path: Path to an Article 12 bundle zip carrying
            ``events.jsonl``.
        hmac_key: Raw HMAC-SHA256 master key (caller responsibility). The
            function will derive the per-store key for scheme v2 entries.

    Returns:
        :class:`CheckResult` with ``ok=True`` when every event's HMAC matches
        ``HMAC(key, prev_hmac || canonical_json(payload-without-hmac))`` and
        ``prev_hmac`` matches the previous link. Supports both scheme v1
        (raw key, no domain prefix) and scheme v2 (HKDF-derived key, domain
        tagged preimage).
    """
    try:
        with zipfile.ZipFile(bundle_path) as zf:
            try:
                event_log = zf.read("events.jsonl").decode("utf-8")
            except KeyError:
                # Multi-tenant exports do not carry events.jsonl in a zip;
                # callers should skip --hmac-key in that case.
                return CheckResult(
                    name="hmac_chain",
                    ok=False,
                    detail="events.jsonl not present - bundle does not look like an Article 12 zip",
                )
    except zipfile.BadZipFile as exc:
        return CheckResult(name="hmac_chain", ok=False, detail=f"bundle is not a zip: {exc}")

    prev = GENESIS_HMAC
    line_no = 0
    for raw_line in event_log.splitlines():
        line_no += 1
        line = raw_line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            return CheckResult(
                name="hmac_chain",
                ok=False,
                detail=f"events.jsonl:{line_no}: invalid JSON - {exc}",
            )
        stored = entry.pop("hmac", "")
        recorded_prev = entry.get("prev_hmac", "")
        if recorded_prev != prev:
            return CheckResult(
                name="hmac_chain",
                ok=False,
                detail=(
                    f"events.jsonl:{line_no}: prev_hmac mismatch (expected {prev[:16]}…, got {recorded_prev[:16]}…)"
                ),
            )

        # Determine scheme from entry (default to v1 for backward compat)
        scheme = entry.get("scheme", SCHEME_V1)
        if scheme == SCHEME_V2:
            verify_key = derive_store_key(hmac_key, DOMAIN_AUDIT)
            verify_prefix = domain_tag(DOMAIN_AUDIT, SCHEME_V2)
        elif scheme == SCHEME_V1:
            verify_key = hmac_key
            verify_prefix = ""
        else:
            return CheckResult(
                name="hmac_chain",
                ok=False,
                detail=f"events.jsonl:{line_no}: unsupported audit scheme {scheme!r}",
            )

        expected = hmac.new(
            verify_key,
            verify_prefix.encode() + _canonical_chain_payload(prev, entry),
            hashlib.sha256,
        ).hexdigest()
        if stored != expected:
            return CheckResult(
                name="hmac_chain",
                ok=False,
                detail=f"events.jsonl:{line_no}: HMAC mismatch",
            )
        prev = stored
    if line_no == 0:
        return CheckResult(name="hmac_chain", ok=True, detail="events.jsonl is empty")
    return CheckResult(name="hmac_chain", ok=True, detail=f"verified {line_no} events")


# ---------------------------------------------------------------------------
# CLI driver
# ---------------------------------------------------------------------------


def _print_check(check: CheckResult, *, verbose: bool, stream: TextIO) -> None:
    """Emit one check result line (PASS/FAIL plus optional detail)."""
    status = "PASS" if check.ok else "FAIL"
    line = f"[{status}] {check.name}"
    if check.detail and (not check.ok or verbose):
        line += f" - {check.detail}"
    print(line, file=stream)


def run_verify(
    *,
    envelope_path: Path,
    bundle_path: Path,
    public_key_path: Path,
    hmac_key_path: Path | None,
    verbose: bool,
    stream: TextIO,
) -> VerifyResult:
    """Run every verification step and emit human-readable output."""
    result = VerifyResult()

    try:
        envelope = load_envelope(envelope_path)
    except (OSError, json.JSONDecodeError) as exc:
        check = CheckResult(name="envelope_load", ok=False, detail=str(exc))
        result.checks.append(check)
        _print_check(check, verbose=verbose, stream=stream)
        return result

    try:
        public_key_pem = public_key_path.read_bytes()
    except OSError as exc:
        check = CheckResult(name="public_key_load", ok=False, detail=str(exc))
        result.checks.append(check)
        _print_check(check, verbose=verbose, stream=stream)
        return result

    sig_check = verify_envelope_signature(envelope, public_key_pem)
    result.checks.append(sig_check)
    _print_check(sig_check, verbose=verbose, stream=stream)

    type_check = verify_statement_types(envelope)
    result.checks.append(type_check)
    _print_check(type_check, verbose=verbose, stream=stream)

    digest_check = verify_subject_digest(envelope, bundle_path)
    result.checks.append(digest_check)
    _print_check(digest_check, verbose=verbose, stream=stream)

    if hmac_key_path is not None:
        try:
            hmac_key = hmac_key_path.read_bytes().strip()
        except OSError as exc:
            check = CheckResult(name="hmac_key_load", ok=False, detail=str(exc))
            result.checks.append(check)
            _print_check(check, verbose=verbose, stream=stream)
        else:
            chain_check = verify_hmac_chain(bundle_path, hmac_key)
            result.checks.append(chain_check)
            _print_check(chain_check, verbose=verbose, stream=stream)

    summary_status = "PASS" if result.ok else "FAIL"
    print(f"OVERALL: {summary_status}", file=stream)
    return result


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser - kept top-level so ``--help`` works on import."""
    parser = argparse.ArgumentParser(
        description=("Verify a DSSE-wrapped bernstein audit bundle without importing the bernstein package."),
    )
    parser.add_argument(
        "--envelope",
        required=True,
        type=Path,
        help="Path to the DSSE envelope JSON.",
    )
    parser.add_argument(
        "--bundle",
        required=True,
        type=Path,
        help="Path to the audit bundle (Article 12 zip or multi-tenant JSON).",
    )
    parser.add_argument(
        "--public-key",
        required=True,
        type=Path,
        help="Path to the Ed25519 public key in PEM (SubjectPublicKeyInfo) format.",
    )
    parser.add_argument(
        "--hmac-key",
        type=Path,
        default=None,
        help=(
            "Optional path to the HMAC chain key. When supplied, the script "
            "also walks events.jsonl and verifies every HMAC link."
        ),
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print PASS-line details (failures always include detail).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Optional argv override (defaults to ``sys.argv[1:]``).

    Returns:
        0 on success, 1 on verification failure, 2 on argument errors.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    for required in (args.envelope, args.bundle, args.public_key):
        if not required.is_file():
            print(f"ERROR: not a file: {required}", file=sys.stderr)
            return 2

    if args.hmac_key is not None and not args.hmac_key.is_file():
        print(f"ERROR: not a file: {args.hmac_key}", file=sys.stderr)
        return 2

    result = run_verify(
        envelope_path=args.envelope,
        bundle_path=args.bundle,
        public_key_path=args.public_key,
        hmac_key_path=args.hmac_key,
        verbose=args.verbose,
        stream=sys.stdout,
    )
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
