"""Tests for wiring the per-task token budget kill switch into the tick loop.

`check_budget_violations` (bernstein.core.observability.circuit_breaker) holds
the "hard-kill at 2x budget" backstop documented on
`OrchestratorConfig.max_tokens_per_task`, and is already exercised in
isolation by tests/unit/test_kill_switch.py. What was missing is the tick
loop actually calling it -- the function had zero callers in src/, so the
documented backstop never fired; the only path that ran every tick was
`check_token_growth`, which enforces an unrelated fixed global threshold.

These tests prove `Orchestrator._tick_internal` now calls
`check_budget_violations` once per tick.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
from bernstein.core.models import OrchestratorConfig
from bernstein.core.spawner import AgentSpawner

from bernstein.adapters.base import CLIAdapter, SpawnResult
from bernstein.core.orchestration.orchestrator import Orchestrator, TickResult

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _mock_adapter() -> MagicMock:
    adapter = MagicMock(spec=CLIAdapter)
    adapter.spawn.return_value = SpawnResult(pid=42, log_path=Path("/tmp/t.log"))
    adapter.is_alive.return_value = True
    adapter.is_rate_limited.return_value = False
    adapter.kill.return_value = None
    adapter.name.return_value = "MockCLI"
    return adapter


def _build_orch(tmp_path: Path) -> Orchestrator:
    """Build a minimal orchestrator with a no-op httpx client, no live agents."""
    cfg = OrchestratorConfig(max_agents=1, poll_interval_s=1, server_url="http://testserver")
    templates_dir = tmp_path / "templates" / "roles"
    templates_dir.mkdir(parents=True, exist_ok=True)
    spawner = AgentSpawner(_mock_adapter(), templates_dir, tmp_path)

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"tasks": [], "total": 0})

    client = httpx.Client(transport=httpx.MockTransport(_handler), base_url="http://testserver")
    return Orchestrator(cfg, spawner, tmp_path, client=client)


# ---------------------------------------------------------------------------
# TestBudgetViolationTickWiring
# ---------------------------------------------------------------------------


class TestBudgetViolationTickWiring:
    def test_tick_body_references_check_budget_violations(self) -> None:
        """`_tick_internal` source must call `check_budget_violations`.

        Regression guard for the concrete defect in issue #3374: the
        per-task token budget's "hard-killed at 2x" comment on
        `max_tokens_per_task` (models.py) described behaviour that
        `check_budget_violations` implemented but nothing invoked.
        """
        src = inspect.getsource(Orchestrator._tick_internal)
        assert "check_budget_violations" in src, (
            "Orchestrator._tick_internal does not wire up the per-task token budget kill switch"
        )

    def test_tick_invokes_check_budget_violations_once(self, tmp_path: Path) -> None:
        """A single tick calls `check_budget_violations(self, result)` exactly once."""
        orch = _build_orch(tmp_path)

        with patch("bernstein.core.orchestration.orchestrator.check_budget_violations") as mock_check:
            orch.tick()

        assert mock_check.call_count == 1
        called_orch: Any
        called_result: Any
        called_orch, called_result = mock_check.call_args[0]
        assert called_orch is orch
        assert isinstance(called_result, TickResult)
