"""Standalone verification of a Bernstein authority envelope.

This module is the heart of ``bernstein-verify-envelope``. It MUST NOT import
anything from ``bernstein.*``, and it MUST NOT open a socket. Every primitive
is re-implemented here:

  * RFC 8785 JCS canonical JSON (object names sorted as UTF-16 code units).
  * Detached compact JWS (RFC 7515 Appendix F) with EdDSA.
  * Ed25519 JWK -> public key (RFC 8037).
  * The envelope's own hash preimages: the principal id binding, the chained
    grant hashes, the decision input hashes, and the per-section digests.

Nothing here trusts a recorded hash. Each one is recomputed from material the
envelope itself carries, so a widened grant scope, an edited policy input or a
silently-dropped decision fails even when the signature is intact. The
signature says who asserted the envelope; the recomputation says the assertion
follows from its own inputs.

The envelope's embedded key is a hint, not a trust anchor: an envelope that
verifies against the key it carries is reported as trust-on-first-use.

Air-gap guarantee: no network calls. Only stdlib + ``cryptography``.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import IO, Any

# Wire-format constants. These MUST match schemas/authority-envelope-v1.json.
SCHEMA_VERSION = "1.0.0"
ENVELOPE_TYPE = "https://bernstein.run/attestations/authority-envelope/v1"
JWS_TYP = "application/vnd.bernstein.authority-envelope+jws"
JWS_ALG = "EdDSA"
SECTION_NAMES = ("principal", "grants", "decisions", "evidence", "coverage")
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    """Outcome of a single verification check."""

    name: str
    ok: bool
    detail: str = ""


@dataclass
class VerifyResult:
    """Aggregate outcome across every check that ran."""

    checks: list[CheckResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True iff every check that ran reported success."""
        return all(c.ok for c in self.checks)

    @property
    def errors(self) -> list[str]:
        """``name: detail`` for every failing check."""
        return [f"{c.name}: {c.detail}" for c in self.checks if not c.ok]

    @property
    def stats(self) -> str:
        """Human-readable summary: count of passed vs total checks."""
        n_ok = sum(1 for c in self.checks if c.ok)
        return f"{n_ok}/{len(self.checks)} checks passed"


# ---------------------------------------------------------------------------
# RFC 8785 JCS
# ---------------------------------------------------------------------------


def _utf16_code_units(name: str) -> bytes:
    """Return *name* as big-endian UTF-16 code units for RFC 8785 ordering."""
    return name.encode("utf-16-be", errors="surrogatepass")


def _sorted_by_code_units(value: Any) -> Any:
    """Rebuild *value* with every object's names in RFC 8785 order."""
    if isinstance(value, dict):
        named = sorted(value.items(), key=lambda pair: _utf16_code_units(str(pair[0])))
        return {name: _sorted_by_code_units(item) for name, item in named}
    if isinstance(value, (list, tuple)):
        return [_sorted_by_code_units(item) for item in value]
    return value


def canonicalize_jcs(value: Any) -> bytes:
    """Return the RFC 8785 canonical JSON encoding of *value* as UTF-8 bytes."""
    return json.dumps(
        _sorted_by_code_units(value),
        sort_keys=False,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _sha256_jcs(value: Any) -> str:
    """Content hash over the canonical bytes of *value*."""
    return hashlib.sha256(canonicalize_jcs(value)).hexdigest()


def _b64url(data: bytes) -> str:
    """Base64-url-encode without padding (RFC 7515 §2)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    """Base64-url-decode, restoring padding."""
    return base64.urlsafe_b64decode(data + ("=" * (-len(data) % 4)))


def _public_key_from_jwk(jwk: Any) -> Any:
    """Convert an OKP/Ed25519 JWK into an Ed25519PublicKey (RFC 8037)."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    if not isinstance(jwk, dict) or jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519":
        raise ValueError(f"expected kty=OKP, crv=Ed25519; got {jwk!r}")
    x = jwk.get("x")
    if not isinstance(x, str):
        raise ValueError("JWK 'x' missing or not a string")
    raw = _b64url_decode(x)
    if len(raw) != 32:
        raise ValueError(f"Ed25519 public key must be 32 bytes (got {len(raw)})")
    return Ed25519PublicKey.from_public_bytes(raw)


def _parse_timestamp(value: Any, what: str) -> datetime:
    """Parse an RFC 3339 UTC timestamp, raising ValueError with context."""
    if not isinstance(value, str):
        raise ValueError(f"{what} is not a string")
    try:
        return datetime.strptime(value, _TIMESTAMP_FORMAT)
    except ValueError as exc:
        raise ValueError(f"{what} {value!r} is not RFC 3339 UTC (YYYY-MM-DDThh:mm:ssZ)") from exc


# ---------------------------------------------------------------------------
# Section verifiers
# ---------------------------------------------------------------------------


def verify_envelope_type(envelope: dict[str, Any]) -> CheckResult:
    """The envelope must declare the version and type this verifier implements."""
    version = envelope.get("schema_version")
    kind = envelope.get("envelope_type")
    if version != SCHEMA_VERSION:
        return CheckResult(
            "envelope_type", ok=False, detail=f"unsupported schema_version {version!r}"
        )
    if kind != ENVELOPE_TYPE:
        return CheckResult("envelope_type", ok=False, detail=f"unexpected envelope_type {kind!r}")
    return CheckResult("envelope_type", ok=True, detail=f"{ENVELOPE_TYPE} v{SCHEMA_VERSION}")


def verify_coverage_present(envelope: dict[str, Any]) -> CheckResult:
    """Refuse an envelope that says nothing about what it does not cover.

    Silence about a gap reads as full coverage to anyone who does not run the
    verifier, so an omitted section is a refusal rather than a warning.
    """
    coverage = envelope.get("coverage")
    if not isinstance(coverage, dict):
        return CheckResult(
            "coverage",
            ok=False,
            detail=(
                "coverage section is missing; an envelope must state what it does not cover, "
                "and silence is not read as full coverage"
            ),
        )
    for member in ("covered", "uncovered", "statement"):
        if member not in coverage:
            return CheckResult(
                "coverage",
                ok=False,
                detail=f"coverage.{member} is missing from the coverage section",
            )
    return CheckResult("coverage", ok=True)


def verify_signature(envelope: dict[str, Any]) -> tuple[CheckResult, str]:
    """Verify the detached EdDSA JWS over the JCS bytes of the envelope body.

    Returns ``(check, note)`` where the note records how the verifying key was
    obtained. The embedded key is a hint, so a successful verification against
    it is reported as trust-on-first-use.
    """
    from cryptography.exceptions import InvalidSignature

    block = envelope.get("signature")
    if not isinstance(block, dict):
        return CheckResult("signature", ok=False, detail="signature section missing"), ""
    if block.get("alg") != JWS_ALG:
        return CheckResult("signature", ok=False, detail=f"unexpected alg {block.get('alg')!r}"), ""

    jws = block.get("jws")
    if not isinstance(jws, str) or jws.count(".") != 2:
        return CheckResult("signature", ok=False, detail="jws is not a compact JWS"), ""
    header_b64, payload_b64, sig_b64 = jws.split(".")
    if payload_b64:
        # Not a detached signature -- refuse rather than verify a payload that
        # is not the envelope body.
        return CheckResult("signature", ok=False, detail="jws is not detached"), ""

    try:
        header = json.loads(_b64url_decode(header_b64))
    except (ValueError, json.JSONDecodeError) as exc:
        return CheckResult(
            "signature", ok=False, detail=f"protected header decode failed: {exc}"
        ), ""
    if not isinstance(header, dict):
        return CheckResult("signature", ok=False, detail="protected header is not an object"), ""
    if header.get("alg") != JWS_ALG or header.get("typ") != JWS_TYP:
        return (
            CheckResult(
                "signature",
                ok=False,
                detail=(
                    "protected header alg/typ mismatch: "
                    f"{header.get('alg')!r}/{header.get('typ')!r}"
                ),
            ),
            "",
        )
    if header.get("kid") != block.get("kid"):
        return (
            CheckResult(
                "signature", ok=False, detail="protected header kid differs from signature.kid"
            ),
            "",
        )

    try:
        public_key = _public_key_from_jwk(block.get("public_key_jwk"))
    except ValueError as exc:
        return CheckResult("signature", ok=False, detail=f"embedded key unusable: {exc}"), ""

    body = {key: value for key, value in envelope.items() if key != "signature"}
    signing_input = f"{header_b64}.{_b64url(canonicalize_jcs(body))}".encode("ascii")
    try:
        public_key.verify(_b64url_decode(sig_b64), signing_input)
    except InvalidSignature:
        return (
            CheckResult(
                "signature",
                ok=False,
                detail="detached JWS does not verify over the canonical envelope body",
            ),
            "",
        )
    except ValueError as exc:
        return CheckResult("signature", ok=False, detail=f"signature decode failed: {exc}"), ""

    note = "trust-on-first-use (verified against the key the envelope carries)"
    return CheckResult("signature", ok=True, detail=note), note


def verify_section_digests(envelope: dict[str, Any]) -> list[CheckResult]:
    """Recompute each section digest so a failure names the section that moved."""
    digests = envelope.get("section_digests")
    if not isinstance(digests, dict):
        return [CheckResult("section_digests", ok=False, detail="section_digests missing")]

    checks: list[CheckResult] = []
    for name in SECTION_NAMES:
        recorded = digests.get(name)
        if name not in envelope:
            checks.append(CheckResult(f"section:{name}", ok=False, detail="section missing"))
            continue
        recomputed = _sha256_jcs(envelope[name])
        if recorded != recomputed:
            checks.append(
                CheckResult(
                    f"section:{name}",
                    ok=False,
                    detail=(
                        f"recomputed digest {recomputed[:16]}... != recorded "
                        f"{str(recorded)[:16]}... (the {name} section was modified)"
                    ),
                )
            )
        else:
            checks.append(CheckResult(f"section:{name}", ok=True, detail=f"{recomputed[:16]}..."))
    return checks


def verify_principal(envelope: dict[str, Any]) -> CheckResult:
    """Recompute the binding between the principal identifier and its key."""
    principal = envelope.get("principal")
    if not isinstance(principal, dict):
        return CheckResult("principal", ok=False, detail="principal section missing")

    principal_id = principal.get("id")
    key = principal.get("key")
    if not isinstance(principal_id, str) or not principal_id:
        return CheckResult("principal", ok=False, detail="principal.id missing")
    try:
        _public_key_from_jwk(key)
    except ValueError as exc:
        return CheckResult("principal", ok=False, detail=f"principal.key unusable: {exc}")

    recomputed = _sha256_jcs({"v": SCHEMA_VERSION, "id": principal_id, "key": key})
    recorded = principal.get("id_binding")
    if recorded != recomputed:
        return CheckResult(
            "principal",
            ok=False,
            detail=(
                f"recomputed id_binding {recomputed[:16]}... != recorded "
                f"{str(recorded)[:16]}... (the identifier was re-pointed at other key material)"
            ),
        )
    return CheckResult("principal", ok=True, detail=f"{principal_id} bound to its key")


def _grant_hash(grant: dict[str, Any], parent_hash: str) -> str:
    """Recompute one grant link's chained hash."""
    return _sha256_jcs(
        {
            "v": SCHEMA_VERSION,
            "grant_id": grant.get("grant_id"),
            "issuer": grant.get("issuer"),
            "subject": grant.get("subject"),
            "scope": grant.get("scope"),
            "not_after": grant.get("not_after"),
            "parent_hash": parent_hash,
        }
    )


def verify_grants(envelope: dict[str, Any]) -> CheckResult:
    """Recompute the grant chain: chained hashes, attenuation, and terminus."""
    grants = envelope.get("grants")
    if not isinstance(grants, list) or not grants:
        return CheckResult("grants", ok=False, detail="grants section missing or empty")

    seen: set[str] = set()
    parent_hash = ""
    previous: dict[str, Any] | None = None
    for index, grant in enumerate(grants):
        if not isinstance(grant, dict):
            return CheckResult("grants", ok=False, detail=f"grants[{index}] is not an object")
        grant_id = str(grant.get("grant_id", ""))
        if not grant_id or grant_id in seen:
            return CheckResult(
                "grants", ok=False, detail=f"grants[{index}] has a missing or duplicate grant_id"
            )
        seen.add(grant_id)

        scope = grant.get("scope")
        if not isinstance(scope, list) or scope != sorted(set(scope)):
            return CheckResult(
                "grants", ok=False, detail=f"grant {grant_id} scope is not a sorted, unique list"
            )

        try:
            not_after = _parse_timestamp(grant.get("not_after"), f"grant {grant_id} not_after")
        except ValueError as exc:
            return CheckResult("grants", ok=False, detail=str(exc))

        if previous is None:
            if grant.get("parent") is not None:
                return CheckResult(
                    "grants", ok=False, detail=f"root grant {grant_id} must have parent null"
                )
        else:
            if grant.get("parent") != previous.get("grant_id"):
                return CheckResult(
                    "grants",
                    ok=False,
                    detail=(
                        f"grant {grant_id} parent {grant.get('parent')!r} is not the preceding link"
                    ),
                )
            if grant.get("issuer") != previous.get("subject"):
                return CheckResult(
                    "grants",
                    ok=False,
                    detail=(
                        f"grant {grant_id} issuer {grant.get('issuer')!r} is not the subject of "
                        f"grant {previous.get('grant_id')!r}"
                    ),
                )
            widened = sorted(set(scope) - set(previous.get("scope") or []))
            if widened:
                return CheckResult(
                    "grants",
                    ok=False,
                    detail=(
                        f"grant {grant_id} widens its parent's scope with {widened}; "
                        "each link may only attenuate"
                    ),
                )
            parent_not_after = _parse_timestamp(
                previous.get("not_after"), f"grant {previous.get('grant_id')} not_after"
            )
            if not_after > parent_not_after:
                return CheckResult(
                    "grants",
                    ok=False,
                    detail=(
                        f"grant {grant_id} expires at {grant.get('not_after')}, later than its "
                        f"parent at {previous.get('not_after')}"
                    ),
                )

        recomputed = _grant_hash(grant, parent_hash)
        if grant.get("grant_hash") != recomputed:
            return CheckResult(
                "grants",
                ok=False,
                detail=(
                    f"grant {grant_id} recomputed hash {recomputed[:16]}... != recorded "
                    f"{str(grant.get('grant_hash'))[:16]}..."
                ),
            )
        parent_hash = recomputed
        previous = grant

    principal_id = (envelope.get("principal") or {}).get("id")
    if previous is not None and previous.get("subject") != principal_id:
        return CheckResult(
            "grants",
            ok=False,
            detail=(
                f"the chain ends at {previous.get('subject')!r}, which is not the acting "
                f"principal {principal_id!r}"
            ),
        )
    return CheckResult("grants", ok=True, detail=f"{len(grants)} link(s), each attenuating")


def verify_decisions(envelope: dict[str, Any]) -> CheckResult:
    """Recompute every decision from its own inputs and the grant it cites."""
    decisions = envelope.get("decisions")
    if not isinstance(decisions, list) or not decisions:
        return CheckResult("decisions", ok=False, detail="decisions section missing or empty")

    grants_by_id: dict[str, dict[str, Any]] = {
        str(g.get("grant_id")): g for g in (envelope.get("grants") or []) if isinstance(g, dict)
    }

    seen: set[str] = set()
    for index, decision in enumerate(decisions):
        if not isinstance(decision, dict):
            return CheckResult("decisions", ok=False, detail=f"decisions[{index}] is not an object")
        decision_id = str(decision.get("decision_id", ""))
        if not decision_id or decision_id in seen:
            return CheckResult(
                "decisions",
                ok=False,
                detail=f"decisions[{index}] has a missing or duplicate decision_id",
            )
        seen.add(decision_id)

        grant = grants_by_id.get(str(decision.get("grant")))
        if grant is None:
            return CheckResult(
                "decisions",
                ok=False,
                detail=(
                    f"decision {decision_id} cites grant {decision.get('grant')!r}, "
                    "which is not in the chain"
                ),
            )
        if decision.get("subject") != grant.get("subject"):
            return CheckResult(
                "decisions",
                ok=False,
                detail=(
                    f"decision {decision_id} subject {decision.get('subject')!r} is not "
                    "the subject "
                    f"of grant {grant.get('grant_id')!r}"
                ),
            )

        # The substantive violations are reported before the integrity hash, so
        # an overreaching decision is named as overreach rather than as a hash
        # mismatch it also happens to cause.
        verdict = decision.get("verdict")
        if verdict not in ("allow", "deny"):
            return CheckResult(
                "decisions", ok=False, detail=f"decision {decision_id} has verdict {verdict!r}"
            )
        scope = grant.get("scope") or []
        if verdict == "allow" and decision.get("action") not in scope:
            return CheckResult(
                "decisions",
                ok=False,
                detail=(
                    f"decision {decision_id} allows {decision.get('action')!r}, which is outside "
                    f"the scope of grant {grant.get('grant_id')!r}"
                ),
            )

        recomputed = _sha256_jcs(
            {
                "v": SCHEMA_VERSION,
                "subject": decision.get("subject"),
                "action": decision.get("action"),
                "resource": decision.get("resource"),
                "policy": decision.get("policy"),
                "inputs": decision.get("inputs"),
                "grant_hash": grant.get("grant_hash"),
            }
        )
        if decision.get("inputs_hash") != recomputed:
            return CheckResult(
                "decisions",
                ok=False,
                detail=(
                    f"decision {decision_id} recomputed inputs_hash {recomputed[:16]}... != "
                    f"recorded {str(decision.get('inputs_hash'))[:16]}..."
                ),
            )

        try:
            taken = _parse_timestamp(decision.get("timestamp"), f"decision {decision_id} timestamp")
            not_after = _parse_timestamp(
                grant.get("not_after"), f"grant {grant.get('grant_id')} not_after"
            )
        except ValueError as exc:
            return CheckResult("decisions", ok=False, detail=str(exc))
        if taken > not_after:
            return CheckResult(
                "decisions",
                ok=False,
                detail=(
                    f"decision {decision_id} was taken at {decision.get('timestamp')}, after "
                    f"grant {grant.get('grant_id')} expired at {grant.get('not_after')}"
                ),
            )

    return CheckResult("decisions", ok=True, detail=f"{len(decisions)} decision(s) recomputed")


def verify_evidence(envelope: dict[str, Any]) -> CheckResult:
    """Every evidence entry must attach to a decision the envelope carries."""
    evidence = envelope.get("evidence")
    if not isinstance(evidence, list):
        return CheckResult("evidence", ok=False, detail="evidence section missing or not a list")

    decision_ids = {
        str(d.get("decision_id")) for d in (envelope.get("decisions") or []) if isinstance(d, dict)
    }
    for index, entry in enumerate(evidence):
        if not isinstance(entry, dict):
            return CheckResult("evidence", ok=False, detail=f"evidence[{index}] is not an object")
        target = str(entry.get("decision", ""))
        if target not in decision_ids:
            return CheckResult(
                "evidence",
                ok=False,
                detail=(
                    f"evidence[{index}] names decision {target!r}, which the envelope "
                    "does not carry"
                ),
            )
        digest = (entry.get("digest") or {}).get("sha256")
        if not isinstance(digest, str) or len(digest) != 64:
            return CheckResult(
                "evidence",
                ok=False,
                detail=f"evidence[{index}] digest.sha256 is not a sha256 hex digest",
            )
    return CheckResult("evidence", ok=True, detail=f"{len(evidence)} artefact hash(es)")


def verify_coverage(envelope: dict[str, Any]) -> CheckResult:
    """Recompute the coverage statement from the decisions and the evidence."""
    coverage = envelope.get("coverage") or {}
    decisions = [d for d in (envelope.get("decisions") or []) if isinstance(d, dict)]
    decision_ids = {str(d.get("decision_id")) for d in decisions}
    actions = {str(d.get("decision_id")): d.get("action") for d in decisions}

    with_evidence = {
        str(e.get("decision"))
        for e in (envelope.get("evidence") or [])
        if isinstance(e, dict) and str(e.get("decision")) in decision_ids
    }
    declared_covered = set(coverage.get("covered") or [])
    if declared_covered != with_evidence:
        return CheckResult(
            "coverage",
            ok=False,
            detail=(
                f"coverage.covered names {sorted(declared_covered)} but the decisions carrying "
                f"evidence are {sorted(with_evidence)}"
            ),
        )

    gaps = decision_ids - with_evidence
    uncovered = coverage.get("uncovered")
    if not isinstance(uncovered, list):
        return CheckResult("coverage", ok=False, detail="coverage.uncovered is not a list")
    declared_gaps: dict[str, Any] = {}
    for index, entry in enumerate(uncovered):
        if not isinstance(entry, dict):
            return CheckResult(
                "coverage", ok=False, detail=f"coverage.uncovered[{index}] is not an object"
            )
        declared_gaps[str(entry.get("decision_id", ""))] = entry
    if set(declared_gaps) != gaps:
        return CheckResult(
            "coverage",
            ok=False,
            detail=(
                f"coverage.uncovered names {sorted(declared_gaps)} but the decisions with no "
                f"evidence are {sorted(gaps)}; a gap the envelope does not state is a refusal"
            ),
        )
    for decision_id, entry in declared_gaps.items():
        if entry.get("action") != actions.get(decision_id):
            return CheckResult(
                "coverage",
                ok=False,
                detail=(
                    f"coverage.uncovered entry for {decision_id} names action "
                    f"{entry.get('action')!r}, not the decision's {actions.get(decision_id)!r}"
                ),
            )
        if not str(entry.get("reason", "")).strip():
            return CheckResult(
                "coverage",
                ok=False,
                detail=f"coverage.uncovered entry for {decision_id} states no reason",
            )

    if not str(coverage.get("statement", "")).strip():
        return CheckResult("coverage", ok=False, detail="coverage.statement is empty")

    return CheckResult(
        "coverage",
        ok=True,
        detail=(
            f"{len(with_evidence)}/{len(decision_ids)} decisions carry evidence; "
            f"uncovered: {sorted(gaps)}"
        ),
    )


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def _print_check(check: CheckResult, *, verbose: bool, stream: IO[str]) -> None:
    """Emit one PASS/FAIL line to the output stream."""
    status = "PASS" if check.ok else "FAIL"
    line = f"[{status}] {check.name}"
    if check.detail and (not check.ok or verbose):
        line += f" - {check.detail}"
    print(line, file=stream)


def run_verify(*, envelope_path: Path, verbose: bool, stream: IO[str]) -> VerifyResult:
    """Run every check over the envelope at *envelope_path*."""
    result = VerifyResult()

    def record(check: CheckResult) -> None:
        result.checks.append(check)
        _print_check(check, verbose=verbose, stream=stream)

    try:
        envelope = json.loads(Path(envelope_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        record(CheckResult("envelope_load", ok=False, detail=str(exc)))
        print("OVERALL: FAIL", file=stream)
        return result
    if not isinstance(envelope, dict):
        record(CheckResult("envelope_load", ok=False, detail="envelope is not a JSON object"))
        print("OVERALL: FAIL", file=stream)
        return result

    record(verify_envelope_type(envelope))

    # Coverage presence is a gate, not a check among others: an envelope that
    # is silent about its gaps is refused before anything else is reported.
    presence = verify_coverage_present(envelope)
    if not presence.ok:
        record(presence)
        print("OVERALL: FAIL", file=stream)
        return result

    signature_check, _note = verify_signature(envelope)
    record(signature_check)
    for check in verify_section_digests(envelope):
        record(check)
    record(verify_principal(envelope))
    record(verify_grants(envelope))
    record(verify_decisions(envelope))
    record(verify_evidence(envelope))
    record(verify_coverage(envelope))

    print(f"OVERALL: {'PASS' if result.ok else 'FAIL'}", file=stream)
    return result


def _build_parser() -> argparse.ArgumentParser:
    """Build the argparse CLI used when this module is run directly."""
    parser = argparse.ArgumentParser(
        description="Verify a Bernstein authority envelope without importing the bernstein package."
    )
    parser.add_argument("--envelope", required=True, type=Path, help="Path to the envelope JSON.")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print PASS-line details.")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns 0 pass, 1 fail, 2 bad args."""
    args = _build_parser().parse_args(argv)
    if not args.envelope.is_file():
        print(f"ERROR: not a file: {args.envelope}", file=sys.stderr)
        return 2
    result = run_verify(envelope_path=args.envelope, verbose=args.verbose, stream=sys.stdout)
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
