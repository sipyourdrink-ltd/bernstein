"""Spawner dispatch integration for per-role provider fallback chains (#2355).

With the primary provider blackholed, dispatch must resolve the role to the
first healthy fallback element and emit a routing receipt into the HMAC audit
chain before the spawn proceeds. When no chain element is healthy the spawn is
refused loudly instead of dispatching into a dead provider.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters.base import CLIAdapter, SpawnError, SpawnResult
from bernstein.core.agents.spawner_core import AgentSpawner
from bernstein.core.routing.provider_availability import ChainElement, ProbeResult
from bernstein.core.security.audit_chain import (
    EVENT_ROUTING_FAILOVER_RECEIPT,
    AuditChainStore,
)


class _NoopAdapter(CLIAdapter):
    """Stub adapter - never spawns a process; just satisfies the constructor."""

    def spawn(
        self,
        *,
        prompt: str,
        workdir: Path,
        model_config: ModelConfig,
        session_id: str,
        mcp_config: dict[str, Any] | None = None,
        timeout_seconds: int = 1800,
        task_scope: str = "medium",
        budget_multiplier: float = 1.0,
        system_addendum: str = "",
    ) -> SpawnResult:
        raise NotImplementedError

    def name(self) -> str:
        return "noop"


_AVAILABILITY_SECTION: dict[str, Any] = {
    "probe_ttl_minutes": 5,
    "roles": {
        "developer": {
            "conformance_floor": "advanced",
            "chain": [
                {"adapter": "claude", "model": "opus", "conformance": "expert"},
                {"adapter": "codex", "model": "gpt-5.2", "conformance": "advanced"},
            ],
        },
    },
}


def _spawner(tmp_path: Path, *, prober: Any, section: dict[str, Any] | None = None) -> AgentSpawner:
    workdir = tmp_path / "project"
    workdir.mkdir(parents=True, exist_ok=True)
    templates = tmp_path / "templates"
    templates.mkdir(parents=True, exist_ok=True)
    return AgentSpawner(
        adapter=_NoopAdapter(),
        templates_dir=templates,
        workdir=workdir,
        use_worktrees=False,
        provider_availability=section if section is not None else _AVAILABILITY_SECTION,
        availability_prober=prober,
    )


def _blackhole_primary(element: ChainElement) -> ProbeResult:
    healthy = element.adapter != "claude"
    return ProbeResult(adapter=element.adapter, healthy=healthy, probe_kind="test", detail="", checked_at=0.0)


def test_dispatch_fails_over_to_healthy_fallback_and_emits_receipt(tmp_path: Path) -> None:
    """AC: primary blackholed -> dispatch resolves the fallback + routing receipt."""
    spawner = _spawner(tmp_path, prober=_blackhole_primary)

    resolved = spawner._apply_provider_availability("developer", "task-1", {"effort": "high"})

    # Dispatch continues on the fallback element, not the blackholed primary.
    assert resolved["provider"] == "codex"
    assert resolved["cli"] == "codex"
    assert resolved["model"] == "gpt-5.2"
    # Unrelated role-policy fields survive the merge.
    assert resolved["effort"] == "high"

    chain = AuditChainStore(spawner._workdir / ".sdd" / "audit")
    rows = chain.query(event_type=EVENT_ROUTING_FAILOVER_RECEIPT)
    assert len(rows) == 1
    details = rows[0].details
    assert details["role"] == "developer"
    assert details["task_id"] == "task-1"
    assert details["chosen_index"] == 1
    assert details["reason"] == "failover"
    assert details["decision_hash"].startswith("sha256:")
    assert details["chain_considered"][0]["adapter"] == "claude"
    assert details["probe_results"][0]["healthy"] is False
    assert details["probe_results"][1]["healthy"] is True


def test_dispatch_keeps_primary_when_healthy_and_still_receipts(tmp_path: Path) -> None:
    def all_healthy(element: ChainElement) -> ProbeResult:
        return ProbeResult(adapter=element.adapter, healthy=True, probe_kind="test", detail="", checked_at=0.0)

    spawner = _spawner(tmp_path, prober=all_healthy)
    resolved = spawner._apply_provider_availability("developer", "task-2", {})
    assert resolved["provider"] == "claude"
    assert resolved["model"] == "opus"

    chain = AuditChainStore(spawner._workdir / ".sdd" / "audit")
    rows = chain.query(event_type=EVENT_ROUTING_FAILOVER_RECEIPT)
    assert len(rows) == 1
    assert rows[0].details["reason"] == "primary_healthy"
    assert rows[0].details["chosen_index"] == 0


def test_dispatch_refuses_spawn_when_no_chain_element_is_healthy(tmp_path: Path) -> None:
    def all_down(element: ChainElement) -> ProbeResult:
        return ProbeResult(adapter=element.adapter, healthy=False, probe_kind="test", detail="", checked_at=0.0)

    spawner = _spawner(tmp_path, prober=all_down)
    with pytest.raises(SpawnError, match="developer"):
        spawner._apply_provider_availability("developer", "task-3", {})

    # The refusal itself is receipted so the outage window is reconstructable.
    chain = AuditChainStore(spawner._workdir / ".sdd" / "audit")
    rows = chain.query(event_type=EVENT_ROUTING_FAILOVER_RECEIPT)
    assert len(rows) == 1
    assert rows[0].details["reason"] == "no_healthy_provider"
    assert rows[0].details["chosen_index"] == -1


def test_role_without_declared_chain_is_untouched(tmp_path: Path) -> None:
    spawner = _spawner(tmp_path, prober=_blackhole_primary)
    role_policy = {"cli": "gemini", "model": "gemini-3-pro"}
    resolved = spawner._apply_provider_availability("reviewer", "task-4", role_policy)
    assert resolved == role_policy

    chain = AuditChainStore(spawner._workdir / ".sdd" / "audit")
    assert chain.query(event_type=EVENT_ROUTING_FAILOVER_RECEIPT) == []


def test_spawner_without_availability_config_is_a_noop(tmp_path: Path) -> None:
    spawner = _spawner(tmp_path, prober=None, section={})
    role_policy = {"model": "opus"}
    assert spawner._apply_provider_availability("developer", "task-5", role_policy) == role_policy


def test_below_floor_chain_is_rejected_at_spawner_construction(tmp_path: Path) -> None:
    """AC: a fallback below the role's conformance floor is rejected at validation time."""
    from bernstein.core.routing.provider_availability import AvailabilityPolicyError

    bad_section = {
        "roles": {
            "developer": {
                "conformance_floor": "expert",
                "chain": [
                    {"adapter": "claude", "model": "opus", "conformance": "expert"},
                    {"adapter": "qwen", "model": "qwen3-coder", "conformance": "basic"},
                ],
            },
        },
    }
    with pytest.raises(AvailabilityPolicyError):
        _spawner(tmp_path, prober=None, section=bad_section)
