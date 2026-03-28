"""Tests for the evolve mode cycle flow in the orchestrator."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from bernstein.adapters.base import CLIAdapter, SpawnResult
from bernstein.core.models import (
    OrchestratorConfig,
)
from bernstein.core.orchestrator import Orchestrator
from bernstein.core.spawner import AgentSpawner


@pytest.fixture(autouse=True)
def _no_subprocess_in_evolve(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent evolve methods from spawning real subprocesses (pytest, git, research).

    Without this, tests that trigger an evolve cycle run the full test suite
    recursively, eating 50+ GB of RAM.
    """
    monkeypatch.setattr(
        Orchestrator,
        "_evolve_run_tests",
        lambda self: {"passed": 0, "failed": 0, "summary": "mocked"},
    )
    monkeypatch.setattr(
        Orchestrator,
        "_evolve_auto_commit",
        lambda self: False,
    )
    # Prevent Tavily API calls from _evolve_spawn_manager
    monkeypatch.setattr(
        "bernstein.core.researcher.run_research_sync",
        lambda workdir: None,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_spawner(tmp_path: Path) -> AgentSpawner:
    adapter = MagicMock(spec=CLIAdapter)
    adapter.spawn.return_value = SpawnResult(pid=42, log_path=tmp_path / "test.log")
    adapter.is_alive.return_value = False
    adapter.name.return_value = "MockCLI"
    templates_dir = tmp_path / "templates" / "roles"
    templates_dir.mkdir(parents=True, exist_ok=True)
    return AgentSpawner(adapter, templates_dir, tmp_path)


def _make_config(**kwargs: object) -> OrchestratorConfig:
    defaults = {
        "server_url": "http://127.0.0.1:8052",
        "max_agents": 4,
        "max_tasks_per_agent": 2,
        "poll_interval_s": 1,
    }
    defaults.update(kwargs)
    return OrchestratorConfig(**defaults)


def _write_evolve_json(workdir: Path, **overrides: object) -> Path:
    """Write an evolve.json config and return its path."""
    cfg = {
        "enabled": True,
        "max_cycles": 0,
        "budget_usd": 0,
        "interval_s": 0,
        "_cycle_count": 0,
        "_spent_usd": 0.0,
        "_last_cycle_ts": 0,
        "_consecutive_empty": 0,
    }
    cfg.update(overrides)
    evolve_path = workdir / ".sdd" / "runtime" / "evolve.json"
    evolve_path.parent.mkdir(parents=True, exist_ok=True)
    evolve_path.write_text(json.dumps(cfg))
    return evolve_path


def _mock_client_idle() -> MagicMock:
    """Create a mock httpx client that returns empty task lists (idle state)."""
    client = MagicMock(spec=httpx.Client)

    def _get(url: str, **kwargs: object) -> MagicMock:
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = []
        resp.raise_for_status.return_value = None
        return resp

    def _post(url: str, **kwargs: object) -> MagicMock:
        resp = MagicMock()
        resp.status_code = 201
        resp.json.return_value = {"id": "evolve-task-1"}
        resp.raise_for_status.return_value = None
        return resp

    client.get.side_effect = _get
    client.post.side_effect = _post
    return client


# ---------------------------------------------------------------------------
# Evolve cycle triggering
# ---------------------------------------------------------------------------


class TestEvolveTriggering:
    """Tests that evolve cycles trigger when idle."""

    def test_triggers_when_idle(self, tmp_path: Path) -> None:
        _write_evolve_json(tmp_path)
        spawner = _make_spawner(tmp_path)
        config = _make_config()
        client = _mock_client_idle()
        orch = Orchestrator(config, spawner, tmp_path, client=client)

        result = orch.tick()

        # Should have posted a manager task
        assert client.post.called

    def test_does_not_trigger_when_disabled(self, tmp_path: Path) -> None:
        _write_evolve_json(tmp_path, enabled=False)
        spawner = _make_spawner(tmp_path)
        config = _make_config()
        client = _mock_client_idle()
        orch = Orchestrator(config, spawner, tmp_path, client=client)

        orch.tick()

        # The only posts should be from task claiming, not from evolve
        for call in client.post.call_args_list:
            url = call.args[0] if call.args else call.kwargs.get("url", "")
            assert "tasks" not in url or "/claim" in url or "complete" in url

    def test_does_not_trigger_when_no_evolve_json(self, tmp_path: Path) -> None:
        (tmp_path / ".sdd" / "runtime").mkdir(parents=True, exist_ok=True)
        spawner = _make_spawner(tmp_path)
        config = _make_config()
        client = _mock_client_idle()
        orch = Orchestrator(config, spawner, tmp_path, client=client)

        orch.tick()

        # No evolve-related posts
        for call in client.post.call_args_list:
            url = call.args[0] if call.args else ""
            if "/tasks" in url and "json" in call.kwargs:
                body = call.kwargs["json"]
                assert "Evolve" not in body.get("title", "")


# ---------------------------------------------------------------------------
# Budget cap
# ---------------------------------------------------------------------------


class TestEvolveBudgetCap:
    """Tests that evolve stops when budget is exceeded."""

    def test_stops_at_budget_cap(self, tmp_path: Path) -> None:
        _write_evolve_json(tmp_path, budget_usd=10.0, _spent_usd=10.0)
        spawner = _make_spawner(tmp_path)
        config = _make_config()
        client = _mock_client_idle()
        orch = Orchestrator(config, spawner, tmp_path, client=client)

        orch.tick()

        # Should NOT have posted a new evolve manager task
        evolve_posts = [
            c
            for c in client.post.call_args_list
            if c.args and "/tasks" in c.args[0] and "json" in c.kwargs and "Evolve" in c.kwargs["json"].get("title", "")
        ]
        assert len(evolve_posts) == 0

    def test_continues_under_budget(self, tmp_path: Path) -> None:
        _write_evolve_json(tmp_path, budget_usd=50.0, _spent_usd=10.0)
        spawner = _make_spawner(tmp_path)
        config = _make_config()
        client = _mock_client_idle()
        orch = Orchestrator(config, spawner, tmp_path, client=client)

        orch.tick()

        # Should have posted an evolve task
        evolve_posts = [
            c
            for c in client.post.call_args_list
            if c.args and "/tasks" in c.args[0] and "json" in c.kwargs and "Evolve" in c.kwargs["json"].get("title", "")
        ]
        assert len(evolve_posts) == 1


# ---------------------------------------------------------------------------
# Max cycles
# ---------------------------------------------------------------------------


class TestEvolveMaxCycles:
    """Tests that evolve stops after max_cycles."""

    def test_stops_at_max_cycles(self, tmp_path: Path) -> None:
        _write_evolve_json(tmp_path, max_cycles=5, _cycle_count=5)
        spawner = _make_spawner(tmp_path)
        config = _make_config()
        client = _mock_client_idle()
        orch = Orchestrator(config, spawner, tmp_path, client=client)

        orch.tick()

        evolve_posts = [
            c
            for c in client.post.call_args_list
            if c.args and "/tasks" in c.args[0] and "json" in c.kwargs and "Evolve" in c.kwargs["json"].get("title", "")
        ]
        assert len(evolve_posts) == 0

    def test_unlimited_when_zero(self, tmp_path: Path) -> None:
        _write_evolve_json(tmp_path, max_cycles=0, _cycle_count=100)
        spawner = _make_spawner(tmp_path)
        config = _make_config()
        client = _mock_client_idle()
        orch = Orchestrator(config, spawner, tmp_path, client=client)

        orch.tick()

        evolve_posts = [
            c
            for c in client.post.call_args_list
            if c.args and "/tasks" in c.args[0] and "json" in c.kwargs and "Evolve" in c.kwargs["json"].get("title", "")
        ]
        assert len(evolve_posts) == 1


# ---------------------------------------------------------------------------
# Diminishing returns backoff
# ---------------------------------------------------------------------------


class TestEvolveDiminishingReturns:
    """Tests the backoff mechanism for consecutive empty cycles."""

    def test_no_backoff_below_threshold(self, tmp_path: Path) -> None:
        """2 consecutive empty cycles should NOT trigger backoff."""
        _write_evolve_json(tmp_path, _consecutive_empty=2, interval_s=10)
        spawner = _make_spawner(tmp_path)
        config = _make_config()
        client = _mock_client_idle()
        orch = Orchestrator(config, spawner, tmp_path, client=client)

        orch.tick()

        evolve_posts = [
            c
            for c in client.post.call_args_list
            if c.args and "/tasks" in c.args[0] and "json" in c.kwargs and "Evolve" in c.kwargs["json"].get("title", "")
        ]
        assert len(evolve_posts) == 1

    def test_backoff_at_threshold(self, tmp_path: Path) -> None:
        """3+ consecutive empty cycles should multiply interval by 2^N (capped at 8x)."""
        # With 3 empty cycles, backoff = 2^3 = 8
        # interval_s=100, effective = 800
        # _last_cycle_ts = now - 400 (< 800), so should NOT trigger
        _write_evolve_json(
            tmp_path,
            _consecutive_empty=3,
            interval_s=100,
            _last_cycle_ts=time.time() - 400,
        )
        spawner = _make_spawner(tmp_path)
        config = _make_config()
        client = _mock_client_idle()
        orch = Orchestrator(config, spawner, tmp_path, client=client)

        orch.tick()

        evolve_posts = [
            c
            for c in client.post.call_args_list
            if c.args and "/tasks" in c.args[0] and "json" in c.kwargs and "Evolve" in c.kwargs["json"].get("title", "")
        ]
        assert len(evolve_posts) == 0  # Backoff prevents triggering

    def test_backoff_capped_at_8x(self, tmp_path: Path) -> None:
        """Backoff factor should never exceed 8x."""
        # 10 consecutive empty: 2^10 = 1024, but capped at 8
        # So effective_interval = 100 * 8 = 800
        # _last_cycle_ts = now - 900 (> 800), so should trigger
        _write_evolve_json(
            tmp_path,
            _consecutive_empty=10,
            interval_s=100,
            _last_cycle_ts=time.time() - 900,
        )
        spawner = _make_spawner(tmp_path)
        config = _make_config()
        client = _mock_client_idle()
        orch = Orchestrator(config, spawner, tmp_path, client=client)

        orch.tick()

        evolve_posts = [
            c
            for c in client.post.call_args_list
            if c.args and "/tasks" in c.args[0] and "json" in c.kwargs and "Evolve" in c.kwargs["json"].get("title", "")
        ]
        assert len(evolve_posts) == 1


# ---------------------------------------------------------------------------
# Priority rotation
# ---------------------------------------------------------------------------


class TestEvolvePriorityRotation:
    """Tests that focus area rotates across cycles."""

    def test_rotates_focus_areas(self, tmp_path: Path) -> None:
        """Each cycle should pick a different focus area based on cycle count."""
        focus_areas_seen: list[str] = []

        for cycle in range(6):
            evolve_path = _write_evolve_json(tmp_path, _cycle_count=cycle)
            spawner = _make_spawner(tmp_path)
            config = _make_config()
            client = _mock_client_idle()
            orch = Orchestrator(config, spawner, tmp_path, client=client)

            orch.tick()

            # Find the evolve manager task post
            for call in client.post.call_args_list:
                if call.args and "/tasks" in call.args[0] and "json" in call.kwargs:
                    title = call.kwargs["json"].get("title", "")
                    if "Evolve" in title:
                        # Extract focus area from title like "Evolve cycle N: focus area"
                        focus = title.split(": ", 1)[1] if ": " in title else ""
                        focus_areas_seen.append(focus)

        # Should have 6 different focus areas (one per element in _EVOLVE_FOCUS_AREAS)
        assert len(focus_areas_seen) == 6
        expected = [
            "new features",
            "user interface",
            "test coverage",
            "code quality",
            "performance",
            "documentation",
        ]
        assert focus_areas_seen == expected


# ---------------------------------------------------------------------------
# Cycle metrics logging
# ---------------------------------------------------------------------------


class TestEvolveCycleLogging:
    """Tests that cycle metrics are logged to evolve_cycles.jsonl."""

    def test_logs_cycle_to_jsonl(self, tmp_path: Path) -> None:
        _write_evolve_json(tmp_path)
        spawner = _make_spawner(tmp_path)
        config = _make_config()
        client = _mock_client_idle()
        orch = Orchestrator(config, spawner, tmp_path, client=client)

        orch.tick()

        log_path = tmp_path / ".sdd" / "metrics" / "evolve_cycles.jsonl"
        assert log_path.exists()

        lines = log_path.read_text().strip().split("\n")
        assert len(lines) >= 1

        entry = json.loads(lines[-1])
        assert "cycle" in entry
        assert "timestamp" in entry
        assert entry["cycle"] == 1

    def test_increments_cycle_count(self, tmp_path: Path) -> None:
        evolve_path = _write_evolve_json(tmp_path, _cycle_count=3)
        spawner = _make_spawner(tmp_path)
        config = _make_config()
        client = _mock_client_idle()
        orch = Orchestrator(config, spawner, tmp_path, client=client)

        orch.tick()

        updated = json.loads(evolve_path.read_text())
        assert updated["_cycle_count"] == 4


# ---------------------------------------------------------------------------
# Evolve state updates
# ---------------------------------------------------------------------------


class TestEvolveStateUpdates:
    """Tests that evolve.json state is properly updated after each cycle."""

    def test_updates_last_cycle_timestamp(self, tmp_path: Path) -> None:
        evolve_path = _write_evolve_json(tmp_path, _last_cycle_ts=0)
        spawner = _make_spawner(tmp_path)
        config = _make_config()
        client = _mock_client_idle()
        orch = Orchestrator(config, spawner, tmp_path, client=client)

        before = time.time()
        orch.tick()
        after = time.time()

        updated = json.loads(evolve_path.read_text())
        assert updated["_last_cycle_ts"] >= before
        assert updated["_last_cycle_ts"] <= after

    def test_resets_consecutive_empty_on_success(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        evolve_path = _write_evolve_json(tmp_path, _consecutive_empty=5)
        spawner = _make_spawner(tmp_path)
        config = _make_config()

        # Override autouse: _evolve_auto_commit returns True (committed)
        monkeypatch.setattr(Orchestrator, "_evolve_auto_commit", lambda self: True)

        # Bulk GET /tasks returns done tasks so tasks_completed > 0
        def _get(url: str, **kwargs: object) -> MagicMock:
            resp = MagicMock()
            resp.status_code = 200
            if url.endswith("/tasks"):
                resp.json.return_value = [
                    {"id": "t1", "title": "Done", "description": "d", "role": "backend", "status": "done"}
                ]
            else:
                resp.json.return_value = []
            resp.raise_for_status.return_value = None
            return resp

        client = MagicMock(spec=httpx.Client)
        client.get.side_effect = _get
        client.post.side_effect = lambda *a, **kw: MagicMock(
            status_code=201, json=MagicMock(return_value={"id": "x"}), raise_for_status=MagicMock()
        )

        orch = Orchestrator(config, spawner, tmp_path, client=client)

        orch.tick()

        updated = json.loads(evolve_path.read_text())
        assert updated["_consecutive_empty"] == 0

    def test_increments_consecutive_empty_on_no_changes(self, tmp_path: Path) -> None:
        evolve_path = _write_evolve_json(tmp_path, _consecutive_empty=2)
        spawner = _make_spawner(tmp_path)
        config = _make_config()
        client = _mock_client_idle()
        orch = Orchestrator(config, spawner, tmp_path, client=client)

        orch.tick()

        updated = json.loads(evolve_path.read_text())
        assert updated["_consecutive_empty"] == 3
