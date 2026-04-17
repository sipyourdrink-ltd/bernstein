"""Agent identity cards with capabilities and scope enforcement.

Implements OWASP Top 10 for Agentic Applications (2026) requirement for
verifiable agent identity. Each spawned agent gets an identity card
declaring its capabilities, denied capabilities, scope, and budget.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

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
class AgentIdentityCard:
    agent_id: str
    role: str
    adapter: str
    model: str
    capabilities: list[str] = field(default_factory=list)
    denied_capabilities: list[str] = field(default_factory=list)
    scope: list[str] = field(default_factory=list)
    max_budget_usd: float = 10.0
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @property
    def card_hash(self) -> str:
        return hashlib.sha256(self.to_json().encode()).hexdigest()[:16]

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
    """Load a previously issued identity card, or None if not found."""
    path = runtime_dir / "agents" / agent_id / "identity.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    return AgentIdentityCard(**data)


def check_capability(card: AgentIdentityCard, capability: str) -> tuple[bool, str]:
    """Returns (allowed, reason). Used by enforcement middleware."""
    if card.is_expired():
        return False, "identity card expired"
    if capability in card.denied_capabilities:
        return False, f"capability '{capability}' explicitly denied for role '{card.role}'"
    if capability not in card.capabilities:
        return False, f"capability '{capability}' not granted to role '{card.role}'"
    return True, "allowed"
