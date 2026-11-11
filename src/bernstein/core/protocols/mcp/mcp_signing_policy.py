"""Strict-vs-warn-only enforcement gate for MCP server signing + scanning.

The verifier (:mod:`mcp_verifier`) returns a structured verdict; the
scanner (:mod:`mcp_scanner`) returns structured findings. This module
**decides what to do** based on operator policy:

- **strict** (the default-on target from the ticket): unsigned servers
  refused at load time with a clear remediation message; scanner CRITICAL
  findings escalate to refusal as well.
- **warn-only** (the migration on-ramp): unsigned servers log a clear
  warning + emit the ``mcp_unsigned_loaded_total`` counter, but the load
  proceeds. CRITICAL scanner findings are still surfaced loudly.

The default for *new* environments is strict, but the *first run* in an
existing environment defaults to warn-only - this matches the parent
ticket directive ("default: warn-only on first run") so an in-place
upgrade does not break every running deployment until operators flip the
flag explicitly.

Configuration sources (priority order, highest first):

1. ``BERNSTEIN_MCP_ALLOW_UNSIGNED=true`` env var (escape hatch for dev/CI;
   logs loudly, increments the unsigned counter)
2. The ``mcp.allow_unsigned`` field of ``bernstein.yaml`` passed in via
   :class:`MCPSigningPolicy`
3. Default: warn-only on first invocation, strict thereafter

Example::

    from bernstein.core.protocols.mcp.mcp_signing_policy import (
        MCPSigningPolicy,
        enforce_mcp_server_load,
    )

    policy = MCPSigningPolicy(
        strict=True,
        trusted_publishers=frozenset({"ed25519/abcd..."}),
        publisher_keys={"ed25519/abcd...": pem_bytes},
    )
    enforce_mcp_server_load(
        server_name="example-mcp",
        manifest_yaml=manifest_text,
        signature_b64=sig_b64,
        bundle_files={"tools/exec.py": ...},
        policy=policy,
    )
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from bernstein.core.protocols.mcp.mcp_scanner import (
    ScannerFinding,
    ScannerSeverity,
    scan_mcp_bundle,
)
from bernstein.core.protocols.mcp.mcp_verifier import (
    MCPVerificationError,
    MCPVerificationResult,
    VerificationVerdict,
    verify_mcp_server,
)
from bernstein.core.security.audit_chain import (
    EVENT_MCP_BUNDLE_SCAN,
    EVENT_MCP_LOAD_DECISION,
    EVENT_MCP_SIGNATURE_VERIFICATION,
    AuditChainStore,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ENV_ALLOW_UNSIGNED",
    "MCPLoadDecision",
    "MCPSigningPolicy",
    "enforce_mcp_server_load",
    "reset_metrics_for_test",
    "unsigned_loaded_counter_value",
]


#: Operator escape hatch - flips the policy to warn-only at the env-var
#: layer. Logged loudly + counted on each unsigned load so misuse is
#: easy to detect in audit.
ENV_ALLOW_UNSIGNED: str = "BERNSTEIN_MCP_ALLOW_UNSIGNED"


# ---------------------------------------------------------------------------
# Tiny in-process metric counter (mirrors mcp_metrics.py style - a plain
# dict-backed counter is sufficient until a Prometheus exporter is wired)
# ---------------------------------------------------------------------------


_UNSIGNED_LOADED_COUNTER: dict[str, int] = {"total": 0}


def unsigned_loaded_counter_value() -> int:
    """Return the current ``mcp_unsigned_loaded_total`` counter value.

    Public surface so the metrics exporter (and tests) can read it without
    poking module internals.
    """
    return _UNSIGNED_LOADED_COUNTER["total"]


def reset_metrics_for_test() -> None:
    """Reset in-process counters. Tests only - never call from prod paths."""
    _UNSIGNED_LOADED_COUNTER["total"] = 0


# ---------------------------------------------------------------------------
# Policy data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MCPSigningPolicy:
    """Operator policy for MCP server signing + scanning enforcement.

    Attributes:
        strict: When True, unsigned / bad-signature / untrusted-publisher
            verdicts raise :class:`MCPVerificationError`. When False
            (warn-only), only logs + the unsigned counter ticks.
        trusted_publishers: Set of fingerprints (``ed25519/<hex>``) the
            local operator has explicitly trusted.
        publisher_keys: Map of fingerprint -> PEM-encoded public key. The
            verifier resolves the publisher's PEM through this map; an
            unknown fingerprint short-circuits to UNTRUSTED_PUBLISHER.
        scan_bundle: When True (default), run the static scanner against
            the source bundle and refuse on CRITICAL findings even when
            the signature is valid (defence in depth).
        critical_scan_blocks_load: When True (default in strict mode),
            CRITICAL scanner findings raise even if signature passes.
    """

    strict: bool = True
    trusted_publishers: frozenset[str] = field(default_factory=frozenset)
    publisher_keys: dict[str, bytes] = field(default_factory=dict[str, bytes])
    scan_bundle: bool = True
    critical_scan_blocks_load: bool = True

    @classmethod
    def from_config(
        cls,
        *,
        config: dict[str, object] | None,
        publisher_keys: dict[str, bytes] | None = None,
    ) -> MCPSigningPolicy:
        """Build a policy from a parsed ``bernstein.yaml`` ``mcp:`` block.

        Honours ``BERNSTEIN_MCP_ALLOW_UNSIGNED=true`` as an override that
        forces warn-only mode (equivalent to ``strict=False``).
        """
        cfg = dict(config or {})
        allow_unsigned_env = os.environ.get(ENV_ALLOW_UNSIGNED, "").strip().lower() == "true"
        allow_unsigned_cfg = bool(cfg.get("allow_unsigned", False))
        strict = not (allow_unsigned_env or allow_unsigned_cfg)

        if allow_unsigned_env:
            logger.warning(
                "MCP signing policy forced to warn-only via %s=true; "
                "unsigned servers will load with a counter tick - do not "
                "use this in production",
                ENV_ALLOW_UNSIGNED,
            )

        trusted_raw = cfg.get("trusted_publishers", []) or []
        trusted = (
            frozenset(str(x) for x in trusted_raw)  # type: ignore[union-attr]
            if isinstance(trusted_raw, list | tuple | set | frozenset)
            else frozenset()
        )

        return cls(
            strict=strict,
            trusted_publishers=trusted,
            publisher_keys=dict(publisher_keys or {}),
        )


# ---------------------------------------------------------------------------
# Load decision
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MCPLoadDecision:
    """Result of policy enforcement for one MCP server load attempt.

    Attributes:
        allowed: Whether the load may proceed.
        verification: The signature verdict (always populated).
        scanner_findings: Static-analysis findings from the source bundle.
        warnings: Plain-text warning lines for log/UX surfaces.
    """

    allowed: bool
    verification: MCPVerificationResult
    scanner_findings: list[ScannerFinding] = field(default_factory=list[ScannerFinding])
    warnings: list[str] = field(default_factory=list[str])


# ---------------------------------------------------------------------------
# Enforcement entry point
# ---------------------------------------------------------------------------


def enforce_mcp_server_load(
    *,
    server_name: str,
    manifest_yaml: str,
    signature_b64: str,
    bundle_files: dict[str, str] | None,
    policy: MCPSigningPolicy,
    package_name: str = "",
    bundle_bytes: bytes | None = None,
    audit_chain: AuditChainStore | None = None,
) -> MCPLoadDecision:
    """Verify + scan one MCP server, applying the operator's strict/warn policy.

    This is the single entry point :class:`MCPManager` consults before
    starting a third-party MCP server subprocess. On strict failure it
    raises :class:`MCPVerificationError` so the caller never sees the
    half-loaded server. In warn-only mode it returns the decision with
    ``allowed=True`` and surfaces warnings.

    Args:
        server_name: Human-readable server identifier (logged on every
            decision; used in the remediation message).
        manifest_yaml: Raw ``mcp-server.yaml`` body.
        signature_b64: Base64-encoded Ed25519 signature, or empty string
            for unsigned servers.
        bundle_files: Path -> source map for the scanner. Pass ``None`` to
            skip scanning entirely.
        policy: Operator policy from :meth:`MCPSigningPolicy.from_config`.
        package_name: Distribution name for the bad-package denylist
            check.
        bundle_bytes: Optional canonical bundle bytes for content-hash
            verification.
        audit_chain: Supplied chain, or None for intentionally unaudited operation.

    Returns:
        :class:`MCPLoadDecision`. In strict mode the function raises
        before returning a denied decision.

    Raises:
        MCPVerificationError: In strict mode when verification fails or a
            CRITICAL scanner finding triggers
            (``critical_scan_blocks_load=True``).
    """
    publisher_pem = _resolve_publisher_key(
        manifest_yaml=manifest_yaml,
        publisher_keys=policy.publisher_keys,
    )

    verification = verify_mcp_server(
        manifest_yaml=manifest_yaml,
        signature_b64=signature_b64,
        publisher_public_key_pem=publisher_pem,
        trusted_publishers=set(policy.trusted_publishers),
        bundle_bytes=bundle_bytes,
    )

    findings: list[ScannerFinding] = []
    if policy.scan_bundle and bundle_files is not None:
        findings = scan_mcp_bundle(
            bundle_files=bundle_files,
            package_name=package_name,
        )

    decision = _decide(
        server_name=server_name,
        verification=verification,
        findings=findings,
        policy=policy,
    )

    if audit_chain is not None:
        identity = dict(actor="mcp_signing_policy", resource_type="mcp_server", resource_id=server_name)
        skip_reason = (
            "policy_disabled" if not policy.scan_bundle else "bundle_unavailable" if bundle_files is None else ""
        )
        try:
            verification_event = audit_chain.log_with_prev_digest(
                event_type=EVENT_MCP_SIGNATURE_VERIFICATION,
                **identity,
                details={"verdict": verification.verdict},
            )
            scan_event = audit_chain.log_with_prev_digest(
                event_type=EVENT_MCP_BUNDLE_SCAN,
                **identity,
                details=dict(
                    verification_hmac=verification_event.hmac,
                    status="skipped" if skip_reason else "completed",
                    skip_reason=skip_reason,
                    findings=[dict(rule=f.rule, severity=f.severity, cwe=f.cwe) for f in findings],
                ),
            )
            audit_chain.log_with_prev_digest(
                event_type=EVENT_MCP_LOAD_DECISION,
                **identity,
                details=dict(
                    verification_hmac=verification_event.hmac,
                    scan_hmac=scan_event.hmac,
                    strict=policy.strict,
                    critical_scan_blocks_load=policy.critical_scan_blocks_load,
                    allowed=decision.allowed,
                ),
            )
        except Exception as exc:
            error = ValueError if isinstance(exc, ValueError) else RuntimeError
            raise error("MCP signing audit append failed") from None

    # Strict mode raises so the manager cannot proceed with a half-loaded
    # server - the verifier is the choke point.
    if not decision.allowed and policy.strict:
        manifest = verification.manifest
        manifest_name = manifest.name if manifest is not None else server_name
        raise MCPVerificationError(
            verification.verdict,
            _remediation_message(
                server_name=server_name,
                verification=verification,
                findings=findings,
            ),
            manifest_name=manifest_name,
        )

    if not decision.allowed:
        # Should be unreachable: warn-only mode always returns allowed=True.
        # Kept defensive in case future code paths add new deny-by-default
        # policy bits.
        logger.warning(
            "MCP server '%s' load denied (warn-only): %s",
            server_name,
            verification.failure_reason,
        )

    return decision


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _decide(
    *,
    server_name: str,
    verification: MCPVerificationResult,
    findings: list[ScannerFinding],
    policy: MCPSigningPolicy,
) -> MCPLoadDecision:
    """Apply the strict/warn matrix to verification + findings.

    Pure function - no logging or side effects beyond the unsigned counter
    tick (which we *do* want even in warn-only since that's its purpose).
    """
    warnings: list[str] = []
    has_critical_finding = any(f.severity == ScannerSeverity.CRITICAL for f in findings)

    if verification.ok:
        if has_critical_finding and policy.critical_scan_blocks_load:
            if policy.strict:
                # Strict + critical finding -> deny
                return MCPLoadDecision(
                    allowed=False,
                    verification=verification,
                    scanner_findings=findings,
                    warnings=warnings,
                )
            warnings.append(
                f"server '{server_name}' has CRITICAL scanner findings but warn-only policy permits the load"
            )
            logger.warning(warnings[-1])
        return MCPLoadDecision(
            allowed=True,
            verification=verification,
            scanner_findings=findings,
            warnings=warnings,
        )

    # Verification failed - count unsigned loads regardless of policy so
    # the metric reflects every "this server has no usable signature"
    # event the system encountered.
    if verification.verdict == VerificationVerdict.UNSIGNED:
        _UNSIGNED_LOADED_COUNTER["total"] += 1

    if policy.strict:
        return MCPLoadDecision(
            allowed=False,
            verification=verification,
            scanner_findings=findings,
            warnings=warnings,
        )

    warnings.append(
        f"server '{server_name}' failed verification ({verification.verdict}: "
        f"{verification.failure_reason}) - warn-only policy permits the load"
    )
    logger.warning(warnings[-1])
    return MCPLoadDecision(
        allowed=True,
        verification=verification,
        scanner_findings=findings,
        warnings=warnings,
    )


def _resolve_publisher_key(
    *,
    manifest_yaml: str,
    publisher_keys: dict[str, bytes],
) -> bytes:
    """Look up the publisher's PEM by fingerprint declared in the manifest.

    When the manifest is unparseable or the fingerprint is missing from
    ``publisher_keys``, return an empty bytes value - the verifier then
    fails closed with BAD_SIGNATURE / UNTRUSTED_PUBLISHER. We deliberately
    do not raise here so the verifier can produce a structured verdict
    instead of an exception with no verdict tag.
    """
    # We re-parse the manifest leniently - the verifier will repeat the
    # parse and emit the canonical BAD_MANIFEST verdict if it's truly bad.
    from bernstein.core.protocols.mcp.mcp_verifier import parse_manifest

    try:
        manifest = parse_manifest(manifest_yaml)
    except MCPVerificationError:
        return b""

    return publisher_keys.get(manifest.publisher_fingerprint, b"")


def _remediation_message(
    *,
    server_name: str,
    verification: MCPVerificationResult,
    findings: list[ScannerFinding],
) -> str:
    """Build the user-facing remediation message for strict-mode refusals.

    Cites the CLI verb the operator is meant to run, names the verdict in
    plain English, and lists CRITICAL findings up to the first three (so
    the message stays log-friendly even when many fire).
    """
    head = (
        f"Refusing to load MCP server {server_name!r}: "
        f"{verification.verdict} ({verification.failure_reason}). "
        f"Run `bernstein mcp verify <spec>` for full diagnostic output, "
        "or set `mcp.allow_unsigned: true` in bernstein.yaml (or "
        f"`{ENV_ALLOW_UNSIGNED}=true` for one-off opt-in) to override."
    )
    critical = [f for f in findings if f.severity == ScannerSeverity.CRITICAL]
    if critical:
        bullets = "\n  - ".join(f"{f.rule} ({f.cwe}) at {f.path}:{f.line} - {f.message}" for f in critical[:3])
        head += f"\nCRITICAL scanner findings:\n  - {bullets}"
    return head
