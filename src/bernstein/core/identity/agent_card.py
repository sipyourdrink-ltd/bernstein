"""Agent identity cards with capabilities and scope enforcement.

Implements OWASP Top 10 for Agentic Applications (2026) requirement for
verifiable agent identity. Each spawned agent gets an identity card
declaring its capabilities, denied capabilities, scope, and budget.

A2A v1.0 dataclass migration
----------------------------

The route layer (``bernstein.core.routes.well_known``) has been emitting
A2A v1.0 fields (``protocolVersion``, ``supportedInterfaces``,
``securitySchemes``, ``signatures``) since the JWKS PR - but those fields
were synthesised at route render time, not stored on
:class:`AgentIdentityCard` itself. This module now carries them on the
dataclass so spawned agents can declare them once, sign once, and federate.

Hash compatibility is gated behind the
``BERNSTEIN_AGENT_CARD_V1_0_HASH=1`` environment flag (``card_hash`` falls
back to the legacy 16-hex SHA-256 prefix when unset). Until operators flip
the flag the legacy hash stays bit-for-bit stable so existing HMAC anchors
keep validating; once the flag flips the hash covers the full v1.0 surface
including the new fields.

For verifiers that only understand the pre-v1.0 shape, :meth:`AgentIdentityCard.to_legacy_dict`
strips the v1.0 fields and returns the original 11-key dict - that's the
shape ``agent_card_signer.sign_agent_card`` consumed before this PR.

The migration is intentionally additive - new fields default to empty
collections so older callers who construct the dataclass via positional or
plain ``AgentIdentityCard(**data)`` paths keep working without code
changes.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from pathlib import Path

#: Environment flag controlling the ``card_hash`` migration. When unset
#: (the default), ``card_hash`` keeps its pre-v1.0 semantics so existing
#: HMAC anchors and audit-chain references stay valid. Set to a truthy
#: value (``1``, ``true``, ``yes``, ``on``) once verifiers have migrated to
#: the v1.0 hash that covers ``protocol_version``, ``supported_interfaces``,
#: ``security_schemes``, and ``signatures``.
AGENT_CARD_V1_0_HASH_ENV: str = "BERNSTEIN_AGENT_CARD_V1_0_HASH"

_TRUTHY = frozenset({"1", "true", "yes", "on"})

#: A2A v1.0 protocol version string emitted on cards by default. The route
#: layer's ``_PROTOCOL_VERSION`` resolves to the same string - keeping the
#: literal here makes the dataclass self-contained when used outside the
#: HTTP server (CLI tools, federation tests).
A2A_PROTOCOL_VERSION_V1_0: str = "1.0"

#: Default wire formats a Bernstein-spawned agent speaks. The route layer
#: only emits HTTP+JSON today; cards may declare more once gRPC / JSONRPC
#: stubs ship.
DEFAULT_SUPPORTED_INTERFACES: tuple[str, ...] = ("HTTP+JSON",)

#: Pre-v1.0 dataclass field set. Used by :meth:`AgentIdentityCard.to_legacy_dict`
#: to project a card down to the original shape so verifiers that haven't
#: migrated keep validating.
_LEGACY_FIELDS: tuple[str, ...] = (
    "agent_id",
    "role",
    "adapter",
    "model",
    "capabilities",
    "denied_capabilities",
    "scope",
    "max_budget_usd",
    "max_tokens",
    "max_steps",
    "budget_mode",
    "extensions",
    "created_at",
    "expires_at",
)

#: A2A v1.0 fields added by this migration. Their presence on the card
#: distinguishes v1.0 instances from legacy ones.
_V1_0_FIELDS: tuple[str, ...] = (
    "protocol_version",
    "supported_interfaces",
    "security_schemes",
    "signatures",
)


def v1_0_hash_enabled() -> bool:
    """Return True when the v1.0 hash migration is active for this process."""
    return os.environ.get(AGENT_CARD_V1_0_HASH_ENV, "").strip().lower() in _TRUTHY


#: Claude Opus 4.7 task-budgets beta header value. Sent on the
#: ``anthropic-beta`` API header (and ``ANTHROPIC_BETA`` env var for
#: CLI-mediated calls) when the agent identity card opts in via the
#: ``task_budgets`` extension.
TASK_BUDGETS_BETA_HEADER: str = "task-budgets-2026-03-13"

#: Default per-turn token budget visible to the agent in the countdown
#: banner. 64k mirrors Anthropic's published per-task context surface for
#: Opus 4.7 - callers may override on a per-card basis.
DEFAULT_MAX_TOKENS: int = 64_000

#: Default per-turn step budget. ~30 turns matches the typical role budget
#: that ``ClaudeCodeAdapter._SCOPE_MULTIPLIERS`` resolves to for medium
#: tasks; chosen so the countdown banner has meaningful information from
#: the first turn.
DEFAULT_MAX_STEPS: int = 30

#: Valid budget modes.
#:
#: ``graceful-finish-on-low``
#:     The orchestrator lets the agent finish the in-flight tool call and
#:     emit a summary turn when the budget falls under the configured
#:     threshold. This is the default and matches Anthropic's recommended
#:     ``task-budgets-2026-03-13`` semantics.
#:
#: ``hard-stop-on-zero``
#:     The orchestrator fires the existing ``budget_actions.suggest_downgrade``
#:     path immediately at zero. Mirrors Cursor Glass's $5 arbitration
#:     pause.
BudgetMode = Literal["graceful-finish-on-low", "hard-stop-on-zero"]

DEFAULT_CAPABILITIES: dict[str, list[str]] = {
    "backend": ["read_files", "write_files", "run_tests", "network_access"],
    "frontend": ["read_files", "write_files", "run_tests", "network_access"],
    "qa": ["read_files", "run_tests"],
    "reviewer": ["read_files"],
    "security": ["read_files", "run_tests", "network_access"],
    "docs": ["read_files", "write_files"],
    "devops": ["read_files", "write_files", "run_tests", "network_access"],
}

DEFAULT_DENIED: dict[str, list[str]] = {
    "reviewer": ["write_files", "delete_files", "push_git", "access_secrets"],
    "qa": ["delete_files", "push_git", "access_secrets"],
    "docs": ["delete_files", "push_git", "access_secrets"],
}


@dataclass
class InterfaceSpec:
    """Single A2A v1.0 ``supportedInterfaces[]`` entry.

    Today the route layer only declares HTTP+JSON. Spawned agents that
    federate via gRPC or JSON-RPC will populate further entries; the field
    is a structured dataclass instead of a bare string so additional
    metadata (transport version, content-type) can land without a schema
    break.
    """

    name: str
    version: str = "1.0"
    description: str = ""


@dataclass
class SecurityScheme:
    """Single A2A v1.0 ``securitySchemes[]`` entry.

    Mirrors the route-level shape - verifiers consume the same dict either
    way, so dataclass-generated cards can be served straight to clients.
    """

    id: str
    type: str
    scheme: str = ""
    description: str = ""
    required: bool = False


@dataclass
class Signature:
    """Single A2A v1.0 ``signatures[]`` entry.

    Carries the detached JWS produced by ``agent_card_signer.sign_agent_card``
    along with the metadata verifiers need to route by ``kid`` against the
    ``/.well-known/agent.json/keys`` JWKS endpoint. The compact JWS string
    is RFC 7515 §A.5 (header..signature, empty payload) over the JCS
    canonicalisation of the card body.
    """

    kid: str
    alg: str = "EdDSA"
    typ: str = "agent-card+jws"
    jws: str = ""


@dataclass
class AgentIdentityCard:
    agent_id: str
    role: str
    adapter: str
    model: str
    capabilities: list[str] = field(default_factory=list)
    denied_capabilities: list[str] = field(default_factory=list)
    scope: list[str] = field(default_factory=list)
    max_budget_usd: float = 10.0
    #: Per-turn token cap surfaced to the agent in the countdown banner.
    max_tokens: int = DEFAULT_MAX_TOKENS
    #: Per-turn step cap surfaced to the agent in the countdown banner.
    max_steps: int = DEFAULT_MAX_STEPS
    #: Budget enforcement style (see :data:`BudgetMode`). Defaults to
    #: ``graceful-finish-on-low`` for new identity cards.
    budget_mode: BudgetMode = "graceful-finish-on-low"
    #: Free-form extension flags negotiated at spawn time. Adapters opt in
    #: to provider-specific behaviour by setting truthy values here.
    #: Recognised keys today:
    #:
    #: - ``task_budgets`` (``bool``): when truthy on Anthropic adapters,
    #:   the ``anthropic-beta: task-budgets-2026-03-13`` header is emitted.
    extensions: dict[str, str | bool | int | float] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0

    # ------------------------------------------------------------------
    # A2A v1.0 fields (additive - default to empty so legacy callers still
    # work via plain ``AgentIdentityCard(**data)`` round-trips).
    # ------------------------------------------------------------------
    #: Protocol version this card declares. Empty string keeps pre-v1.0
    #: semantics; new code should set to ``A2A_PROTOCOL_VERSION_V1_0``.
    protocol_version: str = ""
    supported_interfaces: list[InterfaceSpec] = field(default_factory=list)
    security_schemes: list[SecurityScheme] = field(default_factory=list)
    signatures: list[Signature] = field(default_factory=list)

    # ------------------------------------------------------------------
    # SPIFFE workload identity (#2363, additive - default empty).
    # ------------------------------------------------------------------
    #: SPIFFE ID of the SVID bound to this card
    #: (``spiffe://<td>/bernstein/<install>/<agent>``), or ``""`` when the
    #: card has no SPIRE-issued SVID. The binding between this reference and
    #: the SVID is anchored in the audit chain via ``record_spiffe_svid_binding``
    #: so it is verifiable after the fact (see
    #: :mod:`bernstein.core.identity.spiffe.binding`). Covered by the v1.0
    #: ``card_hash`` (and by signatures) but excluded from the legacy hash so
    #: existing HMAC anchors stay bit-stable.
    svid_reference: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    def to_legacy_dict(self) -> dict[str, Any]:
        """Return the pre-v1.0 dict shape (no ``protocol_version`` etc.).

        Use when sending the card to a verifier that only understands the
        original 11-key card shape. The result is byte-identical to what
        ``asdict(self)`` would have produced before the v1.0 migration so
        ``agent_card_signer.sign_agent_card`` can keep verifying tools that
        haven't upgraded.
        """
        full = asdict(self)
        return {key: full[key] for key in _LEGACY_FIELDS if key in full}

    def to_v1_dict(self) -> dict[str, Any]:
        """Return the full A2A v1.0 dict shape including the new fields.

        Symmetric counterpart to :meth:`to_legacy_dict`; equivalent to
        ``asdict(self)`` today but kept as an explicit alias so callers
        signal intent ("I want the v1.0 surface, not whatever asdict
        currently emits") and so future field changes can be gated here.
        """
        return asdict(self)

    @property
    def card_hash(self) -> str:
        """Stable 16-hex SHA-256 prefix over the card body.

        Default semantics keep the legacy 11-field hash so HMAC anchors and
        audit-chain references stay bit-stable. Set
        ``BERNSTEIN_AGENT_CARD_V1_0_HASH=1`` to migrate to the v1.0 hash
        that covers the new fields.
        """
        payload = self.to_v1_dict() if v1_0_hash_enabled() else self.to_legacy_dict()
        body = json.dumps(payload, sort_keys=True).encode()
        return hashlib.sha256(body).hexdigest()[:16]

    def has_capability(self, name: str) -> bool:
        if name in self.denied_capabilities:
            return False
        return name in self.capabilities

    def in_scope(self, path: str) -> bool:
        if not self.scope:
            return True  # empty scope = unrestricted
        return any(path.startswith(prefix) for prefix in self.scope)

    def is_expired(self) -> bool:
        return self.expires_at > 0 and time.time() > self.expires_at


def issue_identity_card(
    agent_id: str,
    role: str,
    adapter: str,
    model: str,
    *,
    scope: list[str] | None = None,
    max_budget_usd: float = 10.0,
    ttl_seconds: int = 3600,
) -> AgentIdentityCard:
    """Generate an identity card for a newly spawned agent."""
    now = time.time()
    return AgentIdentityCard(
        agent_id=agent_id,
        role=role,
        adapter=adapter,
        model=model,
        capabilities=list(DEFAULT_CAPABILITIES.get(role, ["read_files"])),
        denied_capabilities=list(DEFAULT_DENIED.get(role, [])),
        scope=scope or [],
        max_budget_usd=max_budget_usd,
        created_at=now,
        expires_at=now + ttl_seconds if ttl_seconds > 0 else 0.0,
    )


def save_identity_card(card: AgentIdentityCard, runtime_dir: Path) -> Path:
    """Persist card to .sdd/runtime/agents/{agent_id}/identity.json."""
    agent_dir = runtime_dir / "agents" / card.agent_id
    agent_dir.mkdir(parents=True, exist_ok=True)
    path = agent_dir / "identity.json"
    path.write_text(card.to_json())
    return path


def load_identity_card(agent_id: str, runtime_dir: Path) -> AgentIdentityCard | None:
    """Load a previously issued identity card, or None if not found.

    Coerces the v1.0 nested fields (``supported_interfaces``,
    ``security_schemes``, ``signatures``) back to their dataclass instances
    so callers can rely on attribute access. Pre-v1.0 cards on disk (those
    that pre-date the migration and lack these keys) load just as before
    because the new fields default to empty.
    """
    path = runtime_dir / "agents" / agent_id / "identity.json"
    if not path.exists():
        return None
    data: dict[str, Any] = json.loads(path.read_text())
    return _hydrate_card(data)


def _hydrate_card(data: dict[str, Any]) -> AgentIdentityCard:
    """Rebuild an :class:`AgentIdentityCard` from its JSON-serialised form.

    Coerces the v1.0 nested fields if present; ignores extra keys instead of
    raising so a forward-compatible ``identity.json`` written by a newer
    Bernstein release can still load on an older one.
    """
    interfaces_raw = data.get("supported_interfaces") or []
    schemes_raw = data.get("security_schemes") or []
    sigs_raw = data.get("signatures") or []

    payload = {key: data[key] for key in _LEGACY_FIELDS if key in data}
    if "protocol_version" in data:
        payload["protocol_version"] = data["protocol_version"]
    if "svid_reference" in data:
        payload["svid_reference"] = data["svid_reference"]
    payload["supported_interfaces"] = [
        InterfaceSpec(**entry) if isinstance(entry, dict) else entry for entry in interfaces_raw
    ]
    payload["security_schemes"] = [
        SecurityScheme(**entry) if isinstance(entry, dict) else entry for entry in schemes_raw
    ]
    payload["signatures"] = [Signature(**entry) if isinstance(entry, dict) else entry for entry in sigs_raw]
    return AgentIdentityCard(**payload)


def check_capability(card: AgentIdentityCard, capability: str) -> tuple[bool, str]:
    """Returns (allowed, reason). Used by enforcement middleware."""
    if card.is_expired():
        return False, "identity card expired"
    if capability in card.denied_capabilities:
        return False, f"capability '{capability}' explicitly denied for role '{card.role}'"
    if capability not in card.capabilities:
        return False, f"capability '{capability}' not granted to role '{card.role}'"
    return True, "allowed"
