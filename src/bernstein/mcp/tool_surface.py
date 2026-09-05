"""Tool surface risk scoring and capability receipts for MCP servers.

Evaluates an MCP server's declared and discoverable tool surface for security
risk factors:
  - Untrusted input ingestion (e.g. prompt injection attack surface)
  - Sensitive data reach (e.g. credentials, customer data, private database)
  - Egress channels (e.g. webhooks, external HTTP calls, sockets)
  - Wildcard / over-privileged permissions (e.g. '*' scope)
  - Auth posture (none / anonymous vs static bearer token vs OAuth2 PKCE)

The lethal trifecta ("Risky Triple") occurs when a single tool server combines
untrusted input, sensitive data reach, and an external egress channel. Any
server exhibiting the risky triple is classified as CRITICAL and MUST force
an approval gate before invocation. When no approver is configured in the
environment, execution is denied by default.

A verifiable, deterministic CapabilityReceipt is computed with canonical JSON
hashing, allowing offline verification and inclusion in audit chains.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml


class RiskClass(StrEnum):
    """Risk classification for an MCP tool server surface."""

    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    MINIMAL = "MINIMAL"


class AuthPosture(StrEnum):
    """Authentication posture of the tool server."""

    NONE = "none"
    ANONYMOUS = "anonymous"
    STATIC_BEARER_TOKEN = "static_bearer_token"
    BEARER = "bearer"
    OAUTH2_PKCE = "oauth2_pkce"
    OIDC = "oidc"


@dataclass
class ServerManifest:
    """Declared tool surface and security properties of an MCP server."""

    server_id: str
    name: str
    description: str = ""
    auth_posture: str = AuthPosture.NONE.value
    has_untrusted_input: bool = False
    has_sensitive_reach: bool = False
    has_egress_channel: bool = False
    has_wildcard_permissions: bool = False
    tools: list[dict[str, Any]] = field(default_factory=list)
    resources: list[dict[str, Any]] = field(default_factory=list)
    prompts: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ServerManifest:
        """Construct a ServerManifest from a dictionary representation."""
        server_id = str(data.get("id") or data.get("server_id") or "unknown_server")
        name = str(data.get("name") or server_id)
        description = str(data.get("description") or "")
        auth_posture = str(data.get("auth_posture") or data.get("auth") or AuthPosture.NONE.value)
        has_untrusted_input = bool(data.get("has_untrusted_input", False))
        has_sensitive_reach = bool(data.get("has_sensitive_reach", False))
        has_egress_channel = bool(data.get("has_egress_channel", False))
        has_wildcard_permissions = bool(data.get("has_wildcard_permissions", False))
        tools = list(data.get("tools") or [])
        resources = list(data.get("resources") or [])
        prompts = list(data.get("prompts") or [])
        metadata = dict(data.get("metadata") or {})

        return cls(
            server_id=server_id,
            name=name,
            description=description,
            auth_posture=auth_posture,
            has_untrusted_input=has_untrusted_input,
            has_sensitive_reach=has_sensitive_reach,
            has_egress_channel=has_egress_channel,
            has_wildcard_permissions=has_wildcard_permissions,
            tools=tools,
            resources=resources,
            prompts=prompts,
            metadata=metadata,
        )

    @classmethod
    def from_yaml(cls, path_or_content: str | Path) -> ServerManifest:
        """Load a ServerManifest from a YAML file path or YAML string."""
        if isinstance(path_or_content, Path) or (
            isinstance(path_or_content, str) and ("\n" not in path_or_content and Path(path_or_content).is_file())
        ):
            content = Path(path_or_content).read_text(encoding="utf-8")
        else:
            content = str(path_or_content)
        data = yaml.safe_load(content) or {}
        return cls.from_dict(data)


@dataclass(frozen=True)
class CapabilityReceipt:
    """Verifiable cryptographic receipt of evaluated tool surface risk."""

    server_id: str
    risk_class: RiskClass
    has_risky_triple: bool
    forced_approval: bool
    risk_factors: dict[str, bool]
    auth_posture: str
    receipt_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-serializable dictionary with receipt hash."""
        return {
            "server_id": self.server_id,
            "risk_class": self.risk_class.value if isinstance(self.risk_class, RiskClass) else str(self.risk_class),
            "has_risky_triple": self.has_risky_triple,
            "forced_approval": self.forced_approval,
            "risk_factors": self.risk_factors,
            "auth_posture": self.auth_posture,
            "receipt_hash": self.receipt_hash or self.compute_hash(),
        }

    def compute_hash(self) -> str:
        """Deterministic canonical SHA-256 hash over receipt fields."""
        canonical_data = {
            "auth_posture": self.auth_posture,
            "forced_approval": self.forced_approval,
            "has_risky_triple": self.has_risky_triple,
            "risk_class": self.risk_class.value if isinstance(self.risk_class, RiskClass) else str(self.risk_class),
            "risk_factors": dict(sorted(self.risk_factors.items())),
            "server_id": self.server_id,
        }
        payload = json.dumps(canonical_data, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


def _is_sensitive(tool: dict[str, Any]) -> bool:
    """Return True if tool reaches sensitive data."""
    if tool.get("sensitive_reach") or tool.get("sensitive"):
        return True
    sensitivity = str(tool.get("data_sensitivity") or "").lower()
    return sensitivity in ("critical", "sensitive", "private", "confidential", "secret", "restricted")


def _is_egress(tool: dict[str, Any]) -> bool:
    """Return True if tool provides external network/egress channel."""
    return bool(tool.get("egress") or tool.get("has_egress") or tool.get("network_egress"))


def _is_untrusted(tool: dict[str, Any]) -> bool:
    """Return True if tool ingests untrusted or remote input."""
    return bool(tool.get("untrusted_input") or tool.get("has_untrusted_input"))


def _is_wildcard(tool: dict[str, Any]) -> bool:
    """Return True if tool has wildcard scope or unrestricted permissions."""
    if tool.get("wildcard") or tool.get("has_wildcard"):
        return True
    scope = str(tool.get("scope") or "")
    permission = str(tool.get("permission") or "")
    return scope == "*" or permission == "*"


def evaluate_tool_surface_risk(manifest_or_data: ServerManifest | dict[str, Any]) -> CapabilityReceipt:
    """Evaluate an MCP server manifest and emit a verifiable CapabilityReceipt.

    Scoring Invariants:
      1. Risky Triple (sensitive reach + untrusted input + egress channel) -> CRITICAL, forced approval = True.
      2. Wildcard permissions with weak auth (none/anonymous) -> CRITICAL, forced approval = True.
      3. Wildcard permissions with strong auth (OAuth/bearer) -> HIGH, forced approval = True.
      4. Sensitive reach with egress or untrusted input -> HIGH, forced approval = True.
      5. Sensitive reach alone, egress alone, or untrusted input alone -> MEDIUM, forced approval = False.
      6. Read-only without sensitive reach or egress:
         - Anonymous / no auth -> LOW, forced approval = False.
         - Authenticated -> MINIMAL, forced approval = False.
    """
    if isinstance(manifest_or_data, ServerManifest):
        manifest = manifest_or_data
    else:
        manifest = ServerManifest.from_dict(manifest_or_data)

    # Resolve aggregate risk factors
    tools = manifest.tools
    has_untrusted_input = manifest.has_untrusted_input or any(_is_untrusted(t) for t in tools)
    has_sensitive_reach = manifest.has_sensitive_reach or any(_is_sensitive(t) for t in tools)
    has_egress_channel = manifest.has_egress_channel or any(_is_egress(t) for t in tools)
    has_wildcard_permissions = manifest.has_wildcard_permissions or any(_is_wildcard(t) for t in tools)

    is_weak_auth = manifest.auth_posture.lower() in (
        AuthPosture.NONE.value,
        AuthPosture.ANONYMOUS.value,
        "",
        "none",
        "anonymous",
    )

    # 1. Risky Triple
    has_risky_triple = bool(has_sensitive_reach and has_untrusted_input and has_egress_channel)

    if has_risky_triple:
        risk_class = RiskClass.CRITICAL
        forced_approval = True
    elif has_wildcard_permissions:
        if is_weak_auth:
            risk_class = RiskClass.CRITICAL
            forced_approval = True
        else:
            risk_class = RiskClass.HIGH
            forced_approval = True
    elif has_sensitive_reach and (has_egress_channel or has_untrusted_input):
        risk_class = RiskClass.HIGH
        forced_approval = True
    elif has_sensitive_reach or has_egress_channel or has_untrusted_input:
        risk_class = RiskClass.MEDIUM
        forced_approval = False
    else:
        if is_weak_auth:
            risk_class = RiskClass.LOW
            forced_approval = False
        else:
            risk_class = RiskClass.MINIMAL
            forced_approval = False

    risk_factors = {
        "egress_channel": has_egress_channel,
        "sensitive_reach": has_sensitive_reach,
        "untrusted_input": has_untrusted_input,
        "wildcard_permissions": has_wildcard_permissions,
    }

    receipt = CapabilityReceipt(
        server_id=manifest.server_id,
        risk_class=risk_class,
        has_risky_triple=has_risky_triple,
        forced_approval=forced_approval,
        risk_factors=risk_factors,
        auth_posture=manifest.auth_posture,
    )
    # Compute receipt hash and return frozen instance with populated hash
    receipt_hash = receipt.compute_hash()
    return CapabilityReceipt(
        server_id=receipt.server_id,
        risk_class=receipt.risk_class,
        has_risky_triple=receipt.has_risky_triple,
        forced_approval=receipt.forced_approval,
        risk_factors=receipt.risk_factors,
        auth_posture=receipt.auth_posture,
        receipt_hash=receipt_hash,
    )


def is_approval_forced(
    receipt_or_manifest: CapabilityReceipt | ServerManifest | dict[str, Any],
    approver_configured: bool = True,
) -> tuple[bool, str]:
    """Determine whether invocation requires approval, enforcing deny-by-default when unconfigured.

    Returns:
      (forced: bool, reason: str)

    Invariants:
      - If forced approval is required and approver_configured is False, access is denied (deny-by-default).
      - If forced approval is required and approver_configured is True, returns forced=True with approval reason.
      - If forced approval is not required, returns forced=False.
    """
    if isinstance(receipt_or_manifest, CapabilityReceipt):
        receipt = receipt_or_manifest
    else:
        receipt = evaluate_tool_surface_risk(receipt_or_manifest)

    server_id = receipt.server_id
    risk_class_val = receipt.risk_class.value if isinstance(receipt.risk_class, RiskClass) else str(receipt.risk_class)

    if receipt.forced_approval:
        if not approver_configured:
            return (
                True,
                f"DENIED: forced approval required for server '{server_id}' (risk_class={risk_class_val}) "
                f"but no approver is configured in the environment.",
            )
        return (
            True,
            f"APPROVAL_REQUIRED: server '{server_id}' requires operator approval before execution "
            f"(risk_class={risk_class_val}, risky_triple={receipt.has_risky_triple}).",
        )

    return (
        False,
        f"ALLOWED: server '{server_id}' does not require forced approval (risk_class={risk_class_val}).",
    )


def verify_capability_receipt(receipt: CapabilityReceipt | dict[str, Any]) -> bool:
    """Verify cryptographic integrity of a capability receipt."""
    if isinstance(receipt, CapabilityReceipt):
        stored_hash = receipt.receipt_hash
        expected_hash = receipt.compute_hash()
        return bool(stored_hash and stored_hash == expected_hash)
    if isinstance(receipt, dict):
        stored_hash = str(receipt.get("receipt_hash") or "")
        risk_class_str = str(receipt.get("risk_class") or "")
        try:
            rc = RiskClass(risk_class_str)
        except ValueError:
            return False
        temp = CapabilityReceipt(
            server_id=str(receipt.get("server_id") or ""),
            risk_class=rc,
            has_risky_triple=bool(receipt.get("has_risky_triple", False)),
            forced_approval=bool(receipt.get("forced_approval", False)),
            risk_factors=dict(receipt.get("risk_factors") or {}),
            auth_posture=str(receipt.get("auth_posture") or ""),
        )
        return bool(stored_hash and stored_hash == temp.compute_hash())
    return False
