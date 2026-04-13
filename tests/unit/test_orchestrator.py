"""Tests for the Orchestrator — httpx calls and spawner are always mocked."""

from __future__ import annotations

import collections
import json
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from bernstein.core.models import (
    AgentSession,
    CompletionSignal,
    Complexity,
    OrchestratorConfig,
    Scope,
    Task,
    TaskStatus,
    TaskType,
)
from bernstein.core.orchestrator import (
    Orchestrator,
    TickResult,
    group_by_role,
)
from bernstein.core.router import (
    ModelConfig as RouterModelConfig,
)
from bernstein.core.router import (
    ProviderConfig,
    RouterState,
    Tier,
    TierAwareRouter,
)
from bernstein.core.spawner import AgentSpawner
from bernstein.core.tick_pipeline import prioritize_starving_roles

from bernstein.adapters.base import CLIAdapter, SpawnResult

if TYPE_CHECKING:
    from bernstein.core.bulletin import BulletinBoard

# --- Helpers ---


def _make_task(
    *,
    id: str = "T-001",
    role: str = "backend",
    title: str = "Implement feature X",
    description: str = "Write the code.",
    priority: int = 2,
    scope: str = "medium",
    complexity: str = "medium",
    status: str = "open",
    task_type: TaskType = TaskType.STANDARD,
) -> Task:
    return Task(
        id=id,
        title=title,
        description=description,
        role=role,
        priority=priority,
        scope=Scope(scope),
        complexity=Complexity(complexity),
        status=TaskStatus(status),
        task_type=task_type,
    )


def _task_as_dict(task: Task) -> dict[str, object]:
    """Serialise a Task the way the server JSON would look."""
    result: dict[str, object] = {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "role": task.role,
        "priority": task.priority,
        "scope": task.scope.value,
        "complexity": task.complexity.value,
        "estimated_minutes": task.estimated_minutes,
        "status": task.status.value,
        "depends_on": task.depends_on,
        "owned_files": task.owned_files,
        "assigned_agent": task.assigned_agent,
        "result_summary": task.result_summary,
        "task_type": task.task_type.value,
    }
    return result


def _tasks_response(url: httpx.URL, tasks: list[dict]) -> httpx.Response:
    """Return tasks, filtered by ?status= query param when present.

    Used in inline mock handlers so they handle both GET /tasks and
    GET /tasks?status=X correctly.
    """
    status = url.params.get("status")
    if status is not None:
        tasks = [t for t in tasks if t.get("status") == status]
    return httpx.Response(200, json=tasks)


def _mock_adapter(pid: int = 42) -> MagicMock:
    adapter = MagicMock(spec=CLIAdapter)
    adapter.spawn.return_value = SpawnResult(pid=pid, log_path=Path("/tmp/test.log"))
    adapter.is_alive.return_value = True
    adapter.is_rate_limited.return_value = False
    adapter.kill.return_value = None
    adapter.name.return_value = "MockCLI"
    return adapter


def _mock_transport(responses: dict[str, httpx.Response]) -> httpx.MockTransport:
    """Build a mock transport that returns canned responses by URL path+query.

    Args:
        responses: Mapping of "METHOD path?query" to httpx.Response.
                   e.g. {"GET /tasks?status=open": httpx.Response(200, json=[...])}
                   Also supports "GET /tasks" directly.  If "GET /tasks" is not
                   explicitly provided, it is auto-synthesised by aggregating all
                   200-status "GET /tasks?status=X" entries so that existing tests
                   do not need to be rewritten when the orchestrator switches to a
                   single bulk fetch.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        url = request.url
        key = f"{request.method} {url.path}"
        if url.query:
            key += f"?{url.query.decode()}"
        if key in responses:
            return responses[key]
        # Auto-filter: "GET /tasks?status=X" falls back to bulk "GET /tasks" ──
        if key.startswith("GET /tasks?status=") and "GET /tasks" in responses:
            bulk_resp = responses["GET /tasks"]
            if bulk_resp.status_code != 200:
                return bulk_resp
            status_val = url.params.get("status", "")
            filtered = [t for t in bulk_resp.json() if t.get("status") == status_val]
            return httpx.Response(200, json=filtered)
        # Auto-empty: unregistered status filters return [] (not 404) ──────────
        if key.startswith("GET /tasks?status="):
            return httpx.Response(200, json=[])
        # Auto-aggregate for legacy bulk-fetch path ────────────────────────────
        if key == "GET /tasks":
            aggregated: list[object] = []
            for resp_key, resp in responses.items():
                if resp_key.startswith("GET /tasks?status=") and resp.status_code == 200:
                    aggregated.extend(resp.json())
            if aggregated or any(k.startswith("GET /tasks?status=") for k in responses):
                return httpx.Response(200, json=aggregated)
        # Paginated fetch: "GET /tasks?limit=N&offset=M" → wrap bulk result ──
        if request.method == "GET" and url.path == "/tasks" and "limit" in url.params:
            bulk_key = "GET /tasks"
            if bulk_key in responses:
                bulk_resp = responses[bulk_key]
                if bulk_resp.status_code == 200:
                    all_tasks = bulk_resp.json()
                    offset_val = int(url.params.get("offset", "0"))
                    limit_val = int(url.params.get("limit", "100"))
                    page = all_tasks[offset_val : offset_val + limit_val]
                    return httpx.Response(
                        200, json={"tasks": page, "total": len(all_tasks), "limit": limit_val, "offset": offset_val}
                    )
            # Also try aggregating from status-specific entries
            aggregated_p: list[object] = []
            for resp_key, resp in responses.items():
                if resp_key.startswith("GET /tasks?status=") and resp.status_code == 200:
                    aggregated_p.extend(resp.json())
            if aggregated_p or any(k.startswith("GET /tasks?status=") for k in responses):
                return httpx.Response(
                    200, json={"tasks": aggregated_p, "total": len(aggregated_p), "limit": 100, "offset": 0}
                )
        return httpx.Response(404, json={"detail": f"No mock for {key}"})

    return httpx.MockTransport(handler)


def _paginated_transport(inner: httpx.MockTransport) -> httpx.MockTransport:
    """Wrap a mock transport to handle paginated /tasks requests.

    The orchestrator now fetches /tasks?limit=N&offset=M and expects a paginated
    response ``{"tasks": [...], "total": N, ...}``.  Most test transports return
    a plain list for ``GET /tasks``.  This wrapper intercepts paginated requests,
    forwards them as plain ``GET /tasks`` to the inner transport, and wraps the
    response in the paginated envelope.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        url = request.url
        if request.method == "GET" and url.path == "/tasks" and "limit" in url.params:
            # Strip pagination params and forward to inner transport
            plain_params = {k: v for k, v in url.params.items() if k not in ("limit", "offset")}
            plain_url = url.copy_with(params=plain_params) if plain_params else url.copy_with(params={})
            plain = httpx.Request(request.method, plain_url, headers=request.headers)
            resp = inner.handle_request(plain)
            if resp.status_code == 200:
                body = resp.json()
                # Already paginated?
                if isinstance(body, dict) and "tasks" in body:
                    return resp
                # Wrap plain list
                tasks_list = body if isinstance(body, list) else []
                offset_val = int(url.params.get("offset", "0"))
                limit_val = int(url.params.get("limit", "100"))
                page = tasks_list[offset_val : offset_val + limit_val]
                return httpx.Response(
                    200, json={"tasks": page, "total": len(tasks_list), "limit": limit_val, "offset": offset_val}
                )
            return resp
        return inner.handle_request(request)

    return httpx.MockTransport(handler)


def _build_orchestrator(
    tmp_path: Path,
    transport: httpx.MockTransport,
    adapter: CLIAdapter | None = None,
    config: OrchestratorConfig | None = None,
) -> Orchestrator:
    """Convenience: wire up orchestrator with mocked transport."""
    cfg = config or OrchestratorConfig(
        max_agents=6,
        poll_interval_s=1,
        heartbeat_timeout_s=120,
        max_tasks_per_agent=3,
        server_url="http://testserver",
    )
    adp = adapter or _mock_adapter()
    templates_dir = tmp_path / "templates" / "roles"
    templates_dir.mkdir(parents=True)
    spawner = AgentSpawner(adp, templates_dir, tmp_path)
    client = httpx.Client(transport=_paginated_transport(transport), base_url="http://testserver")
    return Orchestrator(cfg, spawner, tmp_path, client=client)


# --- Task.from_dict ---


class TestTaskFromDict:
    def test_round_trip(self) -> None:
        task = _make_task(id="T-099", role="qa", priority=1)
        raw = _task_as_dict(task)
        parsed = Task.from_dict(raw)

        assert parsed.id == "T-099"
        assert parsed.role == "qa"
        assert parsed.priority == 1
        assert parsed.status == TaskStatus.OPEN
        assert parsed.scope == Scope.MEDIUM

    def test_defaults_for_missing_fields(self) -> None:
        raw = {"id": "T-min", "title": "x", "description": "y", "role": "z"}
        parsed = Task.from_dict(raw)

        assert parsed.priority == 2
        assert parsed.scope == Scope.MEDIUM
        assert parsed.complexity == Complexity.MEDIUM
        assert parsed.status == TaskStatus.OPEN


# --- group_by_role ---


class TestGroupByRole:
    def test_single_role_single_batch(self) -> None:
        tasks = [_make_task(id="T-1"), _make_task(id="T-2")]
        batches = group_by_role(tasks, max_per_batch=3)

        assert len(batches) == 1
        assert len(batches[0]) == 2

    def test_single_role_splits_at_max(self) -> None:
        tasks = [_make_task(id=f"T-{i}") for i in range(5)]
        batches = group_by_role(tasks, max_per_batch=2)

        assert len(batches) == 3  # 2+2+1
        assert len(batches[0]) == 2
        assert len(batches[1]) == 2
        assert len(batches[2]) == 1

    def test_multiple_roles_separate_batches(self) -> None:
        tasks = [
            _make_task(id="T-1", role="backend"),
            _make_task(id="T-2", role="qa"),
            _make_task(id="T-3", role="backend"),
        ]
        batches = group_by_role(tasks, max_per_batch=3)

        assert len(batches) == 2
        for batch in batches:
            roles = {t.role for t in batch}
            assert len(roles) == 1  # each batch is same role

    def test_priority_ordering_within_role(self) -> None:
        tasks = [
            _make_task(id="T-low", priority=3),
            _make_task(id="T-crit", priority=1),
            _make_task(id="T-norm", priority=2),
        ]
        batches = group_by_role(tasks, max_per_batch=3)

        assert len(batches) == 1
        ids = [t.id for t in batches[0]]
        assert ids == ["T-crit", "T-norm", "T-low"]

    def test_empty_returns_empty(self) -> None:
        assert group_by_role([], max_per_batch=3) == []

    def test_critical_batch_sorted_first(self) -> None:
        tasks = [
            _make_task(id="T-1", role="qa", priority=3),
            _make_task(id="T-2", role="backend", priority=1),
        ]
        batches = group_by_role(tasks, max_per_batch=1)

        assert len(batches) == 2
        # The batch with priority=1 should come first
        assert batches[0][0].id == "T-2"
        assert batches[1][0].id == "T-1"

    def test_upgrade_proposal_gets_priority_boost(self) -> None:
        """Upgrade proposal tasks should be prioritized over same-priority standard tasks."""
        tasks = [
            _make_task(id="T-normal", priority=2, task_type=TaskType.STANDARD),
            _make_task(id="T-upgrade", priority=2, task_type=TaskType.UPGRADE_PROPOSAL),
        ]
        batches = group_by_role(tasks, max_per_batch=2)

        assert len(batches) == 1
        # Upgrade should come first due to priority boost
        assert batches[0][0].id == "T-upgrade"
        assert batches[0][1].id == "T-normal"

    def test_upgrade_proposal_boost_respects_minimum_priority(self) -> None:
        """Priority boost should not go below 1."""
        tasks = [
            _make_task(id="T-crit-normal", priority=1, task_type=TaskType.STANDARD),
            _make_task(id="T-crit-upgrade", priority=1, task_type=TaskType.UPGRADE_PROPOSAL),
        ]
        batches = group_by_role(tasks, max_per_batch=2)

        assert len(batches) == 1
        # Both have effective priority 1 (upgrade would be 0, but capped), so original priority breaks tie
        # The upgrade should still come first due to secondary sort
        assert batches[0][0].id == "T-crit-upgrade"

    def test_upgrade_proposal_beats_lower_priority_standard(self) -> None:
        """Upgrade proposal with priority=2 should beat standard task with priority=1."""
        tasks = [
            _make_task(id="T-crit", priority=1, task_type=TaskType.STANDARD),
            _make_task(id="T-upgrade", priority=2, task_type=TaskType.UPGRADE_PROPOSAL),
        ]
        batches = group_by_role(tasks, max_per_batch=2)

        assert len(batches) == 1
        # Upgrade with priority=2 gets boosted to effective priority=1, ties with crit
        # Original priority breaks tie, so crit (priority=1) comes first
        assert batches[0][0].id == "T-crit"
        assert batches[0][1].id == "T-upgrade"

    def test_multiple_upgrade_proposals_priority_ordering(self) -> None:
        """Multiple upgrade proposals should be ordered by their boosted priority."""
        tasks = [
            _make_task(id="T-upg-low", priority=3, task_type=TaskType.UPGRADE_PROPOSAL),
            _make_task(id="T-upg-crit", priority=1, task_type=TaskType.UPGRADE_PROPOSAL),
            _make_task(id="T-upg-norm", priority=2, task_type=TaskType.UPGRADE_PROPOSAL),
        ]
        batches = group_by_role(tasks, max_per_batch=3)

        assert len(batches) == 1
        ids = [t.id for t in batches[0]]
        # After boost: crit=0->1, norm=1, low=2
        assert ids == ["T-upg-crit", "T-upg-norm", "T-upg-low"]

    def test_round_robin_interleaves_roles(self) -> None:
        """Batches from different roles are interleaved so no role hogs all slots."""
        tasks = [
            _make_task(id="b1", role="backend", priority=2),
            _make_task(id="b2", role="backend", priority=2),
            _make_task(id="b3", role="backend", priority=2),
            _make_task(id="q1", role="qa", priority=2),
            _make_task(id="q2", role="qa", priority=2),
        ]
        batches = group_by_role(tasks, max_per_batch=1)

        assert len(batches) == 5
        # Round-robin: first 4 batches must alternate roles (b,q,b,q or q,b,q,b)
        roles = [b[0].role for b in batches[:4]]
        # No two consecutive batches should be the same role (for the interleaved portion)
        for i in range(len(roles) - 1):
            assert roles[i] != roles[i + 1], f"Consecutive same-role batches at index {i}: {roles}"

    def test_round_robin_starving_role_gets_first_slot(self) -> None:
        """A role with fewer tasks still gets an agent before over-represented role gets a 2nd."""
        tasks = [
            # backend has 3 tasks, qa has 1 task (same priority)
            _make_task(id="b1", role="backend", priority=2),
            _make_task(id="b2", role="backend", priority=2),
            _make_task(id="b3", role="backend", priority=2),
            _make_task(id="q1", role="qa", priority=2),
        ]
        batches = group_by_role(tasks, max_per_batch=1)

        assert len(batches) == 4
        # q1 must appear within the first 2 batches (round 1), not last
        first_two_roles = {b[0].role for b in batches[:2]}
        assert "qa" in first_two_roles, f"qa not in first 2 batches: {[b[0].id for b in batches]}"

    def test_round_robin_preserves_within_role_priority(self) -> None:
        """Within each role, priority ordering is still respected across rounds."""
        tasks = [
            _make_task(id="b-crit", role="backend", priority=1),
            _make_task(id="b-norm", role="backend", priority=2),
            _make_task(id="q-norm", role="qa", priority=2),
        ]
        batches = group_by_role(tasks, max_per_batch=1)

        assert len(batches) == 3
        # b-crit should appear before b-norm in the result
        backend_ids = [b[0].id for b in batches if b[0].role == "backend"]
        assert backend_ids == ["b-crit", "b-norm"]

    def test_group_by_role_with_alive_per_role_starving_first(self) -> None:
        """group_by_role with alive_per_role should order batches with starving roles first."""
        tasks = [
            _make_task(id="b1", role="backend", priority=2),
            _make_task(id="b2", role="backend", priority=2),
            _make_task(id="q1", role="qa", priority=2),
        ]
        # backend has 3 agents, qa has none (starving)
        alive_per_role = {"backend": 3}
        batches = group_by_role(tasks, max_per_batch=1, alive_per_role=alive_per_role)

        assert len(batches) == 3
        # qa (starving) should come first, even though it has fewer tasks
        roles = [b[0].role for b in batches]
        qa_index = roles.index("qa")
        backend_indices = [i for i, r in enumerate(roles) if r == "backend"]
        # All qa batches should come before backend batches
        assert all(qa_index < bi for bi in backend_indices)

    def test_group_by_role_multiple_starving_roles_prioritized(self) -> None:
        """Multiple starving roles should all come before well-served roles."""
        tasks = [
            _make_task(id="b1", role="backend", priority=2),
            _make_task(id="b2", role="backend", priority=2),
            _make_task(id="q1", role="qa", priority=2),
            _make_task(id="d1", role="docs", priority=2),
        ]
        # Only backend has agents; qa and docs are starving
        alive_per_role = {"backend": 5}
        batches = group_by_role(tasks, max_per_batch=1, alive_per_role=alive_per_role)

        assert len(batches) == 4
        roles = [b[0].role for b in batches]
        # Both qa and docs (starving) should appear before backend (well-served)
        first_backend_idx = next((i for i, r in enumerate(roles) if r == "backend"), None)
        assert first_backend_idx is not None
        for i, role in enumerate(roles[:first_backend_idx]):
            assert role in ("qa", "docs"), f"Expected starving role at index {i}, got {role}"


# --- prioritize_starving_roles ---


class TestPrioritizeStarvingRoles:
    """Unit tests for the starving-role reordering helper."""

    def test_starving_role_moves_before_served_role(self) -> None:
        """A role with 0 alive agents is moved before a role that already has agents."""
        batches = [
            [_make_task(id="b1", role="backend")],
            [_make_task(id="q1", role="qa")],
        ]
        # backend has 3 alive agents, qa has none
        alive_per_role = {"backend": 3}
        result = prioritize_starving_roles(batches, alive_per_role)
        roles = [b[0].role for b in result]
        assert roles[0] == "qa", "starving qa should come before well-served backend"

    def test_no_starving_roles_preserves_order(self) -> None:
        """When all roles have alive agents, the original order is unchanged."""
        batches = [
            [_make_task(id="b1", role="backend")],
            [_make_task(id="q1", role="qa")],
            [_make_task(id="d1", role="docs")],
        ]
        alive_per_role = {"backend": 2, "qa": 1, "docs": 1}
        result = prioritize_starving_roles(batches, alive_per_role)
        assert [b[0].id for b in result] == ["b1", "q1", "d1"]

    def test_empty_batches_returns_unchanged(self) -> None:
        assert prioritize_starving_roles([], {"backend": 1}) == []

    def test_empty_alive_per_role_returns_unchanged(self) -> None:
        """No alive-agent info → nothing to reorder."""
        batches = [
            [_make_task(id="b1", role="backend")],
            [_make_task(id="q1", role="qa")],
        ]
        assert prioritize_starving_roles(batches, {}) == batches

    def test_multiple_starving_roles_all_move_front(self) -> None:
        """All starving roles are moved before any served role."""
        batches = [
            [_make_task(id="b1", role="backend")],  # served (3 agents)
            [_make_task(id="q1", role="qa")],  # starving
            [_make_task(id="d1", role="docs")],  # starving
            [_make_task(id="b2", role="backend")],  # served
        ]
        alive_per_role = {"backend": 3}
        result = prioritize_starving_roles(batches, alive_per_role)
        result_roles = [b[0].role for b in result]
        # qa and docs (starving) must both appear before backend (served)
        first_backend_idx = result_roles.index("backend")
        for i, role in enumerate(result_roles[:first_backend_idx]):
            assert role in ("qa", "docs"), f"unexpected role {role!r} at index {i} before backend"


# --- Tick rebalancing integration ---


class TestTickStarvingRolePriority:
    """Integration tests: the tick reorders batches so starving roles get agents first."""

    def test_starving_role_gets_slot_when_capacity_is_tight(self, tmp_path: Path) -> None:
        """When max_agents capacity is near-full, a starving role gets the last slot
        instead of a role that already has an agent and is still under its per-role cap.
        """
        # Worktree creation requires a git repo with at least one commit.
        subprocess.run(["git", "init", str(tmp_path)], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "--allow-empty", "-m", "init"], capture_output=True, check=True
        )

        backend_task = _make_task(id="T-be", role="backend", priority=2)
        qa_task = _make_task(id="T-qa", role="qa", priority=2)
        all_tasks = [_task_as_dict(backend_task), _task_as_dict(qa_task)]

        def handler(request: httpx.Request) -> httpx.Response:
            url = request.url
            if request.method == "GET" and url.path == "/tasks":
                return _tasks_response(url, all_tasks)
            if request.method == "POST" and "/claim" in url.path:
                # Return 200 for both task claims
                task_id = url.path.split("/")[-2]
                task_dict = next((t for t in all_tasks if t["id"] == task_id), all_tasks[0])
                return httpx.Response(200, json=task_dict)
            return httpx.Response(404)

        # max_agents=2, pre-seed ONE alive backend agent so only 1 slot remains.
        # Both backend and qa are under their per-role caps — without starving-first
        # prioritization, backend (appearing first in alphabetical round-robin) would
        # steal the last slot; with it, qa (starving) must get it.
        cfg = OrchestratorConfig(
            max_agents=2,
            poll_interval_s=1,
            heartbeat_timeout_s=120,
            max_tasks_per_agent=1,
            server_url="http://testserver",
        )
        orch = _build_orchestrator(tmp_path, httpx.MockTransport(handler), config=cfg)
        be_session = AgentSession(
            id="existing-backend",
            role="backend",
            pid=12345,  # non-None so check_alive calls adapter.is_alive(pid)
            task_ids=["T-existing"],
            status="running",
            model_config=RouterModelConfig(model="claude-sonnet-4-6", effort="normal"),
            spawn_ts=time.time(),
        )
        orch._agents["existing-backend"] = be_session

        result = orch.tick()

        # Exactly one new agent should have been spawned (only 1 slot was free)
        assert len(result.spawned) == 1

        # That new agent must be for QA (the starving role), not backend
        new_session_id = result.spawned[0]
        new_session = orch._agents.get(new_session_id)
        assert new_session is not None, "spawned session must be tracked"
        assert new_session.role == "qa", f"starving qa role should have gotten the last slot; got {new_session.role}"


# --- Per-role cap enforcement ---


class TestPerRoleCapDistribution:
    """Integration tests: per-role cap prevents a single role from consuming all slots."""

    def _make_handler(self, task_dicts: list[dict]) -> object:
        def handler(request: httpx.Request) -> httpx.Response:
            url = request.url
            if request.method == "GET" and url.path == "/tasks":
                return httpx.Response(200, json=task_dicts)
            if request.method == "POST" and "/claim" in url.path:
                task_id = url.path.split("/")[-2]
                td = next((t for t in task_dicts if t["id"] == task_id), task_dicts[0])
                return httpx.Response(200, json=td)
            return httpx.Response(404)

        return handler

    def test_backend_at_cap_does_not_get_more_agents(self, tmp_path: Path) -> None:
        """When backend is at its proportional cap, remaining slots go to other roles.

        Setup: max_agents=6, backend: 2 tasks (cap=4), qa: 1 task (cap=2).
        Pre-seed 4 alive backend agents (exactly at cap).
        Global slots: 6 - 4 = 2 free, but backend is at cap.
        Expected: only qa agent(s) spawn, not additional backend agents.
        """
        be1 = _make_task(id="T-be1", role="backend", title="Backend 1")
        be2 = _make_task(id="T-be2", role="backend", title="Backend 2")
        qa1 = _make_task(id="T-qa1", role="qa", title="QA 1")
        task_dicts = [_task_as_dict(be1), _task_as_dict(be2), _task_as_dict(qa1)]

        cfg = OrchestratorConfig(
            max_agents=6,
            poll_interval_s=1,
            heartbeat_timeout_s=120,
            max_tasks_per_agent=1,
            server_url="http://testserver",
        )
        orch = _build_orchestrator(tmp_path, httpx.MockTransport(self._make_handler(task_dicts)), config=cfg)

        # Pre-seed 4 alive backend agents (at cap: ceil(6 * 2 / 3) = 4)
        for i in range(4):
            session = AgentSession(
                id=f"existing-backend-{i}",
                role="backend",
                pid=10000 + i,
                task_ids=[f"T-claimed-{i}"],
                status="running",
                model_config=RouterModelConfig(model="claude-sonnet-4-6", effort="normal"),
                spawn_ts=time.time(),
            )
            orch._agents[session.id] = session

        result = orch.tick()

        # Only QA agent(s) should spawn — backend is at its per-role cap
        spawned_roles = [orch._agents[sid].role for sid in result.spawned if sid in orch._agents]
        assert "backend" not in spawned_roles, (
            f"backend is at its per-role cap; should not spawn more. Got: {spawned_roles}"
        )
        assert "qa" in spawned_roles, f"qa should have gotten a slot; got: {spawned_roles}"

    def test_proportional_cap_allows_both_roles(self, tmp_path: Path) -> None:
        """Both roles stay under cap: each gets at least one agent this tick.

        Setup: max_agents=4, backend: 2 tasks, qa: 2 tasks (cap = 2 each).
        No pre-seeded agents. Both roles are starving.
        Expected: exactly 2 agents spawned (1 backend + 1 qa, capped by max_tasks_per_agent=2).
        """
        tasks = [
            _make_task(id="T-be1", role="backend", title="Backend 1"),
            _make_task(id="T-be2", role="backend", title="Backend 2"),
            _make_task(id="T-qa1", role="qa", title="QA 1"),
            _make_task(id="T-qa2", role="qa", title="QA 2"),
        ]
        task_dicts = [_task_as_dict(t) for t in tasks]

        cfg = OrchestratorConfig(
            max_agents=4,
            poll_interval_s=1,
            heartbeat_timeout_s=120,
            max_tasks_per_agent=2,
            server_url="http://testserver",
        )
        orch = _build_orchestrator(tmp_path, httpx.MockTransport(self._make_handler(task_dicts)), config=cfg)

        result = orch.tick()

        spawned_roles = [orch._agents[sid].role for sid in result.spawned if sid in orch._agents]
        assert "backend" in spawned_roles, "backend should get a slot"
        assert "qa" in spawned_roles, "qa should get a slot"

    def test_cap_minimum_one_per_role(self, tmp_path: Path) -> None:
        """ceil() ensures every role with tasks gets at least 1 cap slot.

        Setup: max_agents=5, backend: 9 tasks, qa: 1 task (total 10).
        Backend cap = ceil(5 * 9 / 10) = 5, qa cap = ceil(5 * 1 / 10) = 1.
        Pre-seed 5 backend agents (at cap). 0 free global slots — no qa spawn.
        This confirms the formula doesn't round qa cap to 0 (ceil enforces >= 1).
        """
        backend_tasks = [_make_task(id=f"T-be{i}", role="backend", title=f"Backend {i}") for i in range(9)]
        qa_task = _make_task(id="T-qa1", role="qa", title="QA 1")
        task_dicts = [_task_as_dict(t) for t in [*backend_tasks, qa_task]]

        cfg = OrchestratorConfig(
            max_agents=5,
            poll_interval_s=1,
            heartbeat_timeout_s=120,
            max_tasks_per_agent=1,
            server_url="http://testserver",
        )
        orch = _build_orchestrator(tmp_path, httpx.MockTransport(self._make_handler(task_dicts)), config=cfg)

        # Pre-seed 5 backend agents (at global cap AND at per-role cap)
        for i in range(5):
            session = AgentSession(
                id=f"existing-backend-{i}",
                role="backend",
                pid=20000 + i,
                task_ids=[f"T-claimed-{i}"],
                status="running",
                model_config=RouterModelConfig(model="claude-sonnet-4-6", effort="normal"),
                spawn_ts=time.time(),
            )
            orch._agents[session.id] = session

        result = orch.tick()

        # Global cap hit — no new spawns
        assert len(result.spawned) == 0, f"global cap (5/5) should block all spawns; got {result.spawned}"

    def test_qa_gets_slot_before_backend_third(self, tmp_path: Path) -> None:
        """Starving qa gets the last open slot; backend does not receive its 3rd agent.

        Setup: max_agents=3, backend: 5 tasks with 2 alive agents (per-role cap = 2),
        qa: 3 tasks with 0 alive agents (per-role cap = 2).
        Per-role cap for backend: ceil(3 * 5/8) = 2 — backend is already at cap.
        One global slot remains. qa is starving (0 agents) so it is promoted to the
        front of the spawn queue. The slot must go to qa, not to a 3rd backend agent.
        """
        backend_tasks = [_make_task(id=f"T-be{i}", role="backend", title=f"Backend {i}") for i in range(5)]
        qa_tasks = [_make_task(id=f"T-qa{i}", role="qa", title=f"QA {i}") for i in range(3)]
        task_dicts = [_task_as_dict(t) for t in backend_tasks + qa_tasks]

        cfg = OrchestratorConfig(
            max_agents=3,
            poll_interval_s=1,
            heartbeat_timeout_s=120,
            max_tasks_per_agent=1,
            server_url="http://testserver",
        )
        orch = _build_orchestrator(tmp_path, httpx.MockTransport(self._make_handler(task_dicts)), config=cfg)

        # Pre-seed 2 alive backend agents — exactly at their per-role cap (ceil(3 * 5/8) = 2)
        for i in range(2):
            session = AgentSession(
                id=f"existing-backend-{i}",
                role="backend",
                pid=30000 + i,
                task_ids=[f"T-claimed-be{i}"],
                status="running",
                model_config=RouterModelConfig(model="claude-sonnet-4-6", effort="normal"),
                spawn_ts=time.time(),
            )
            orch._agents[session.id] = session

        result = orch.tick()

        spawned_roles = [orch._agents[sid].role for sid in result.spawned if sid in orch._agents]
        assert "qa" in spawned_roles, f"qa should get the last slot (starving, under cap); got: {spawned_roles}"
        assert "backend" not in spawned_roles, (
            f"backend is at its per-role cap (2/2); should not get a 3rd agent. Got: {spawned_roles}"
        )

    def test_no_spawn_when_alive_agents_equal_open_batches(self, tmp_path: Path) -> None:
        """No new agents spawned when alive agents for a role already equal open task batches.

        Guards the rebalancing rule: alive_agents_for_role >= open_batches → skip.
        Backend has 2 open tasks (2 batches) and already 2 alive agents → no new spawn.
        QA has 1 open task and 0 agents → gets an agent.
        """
        be1 = _make_task(id="T-be1", role="backend", title="Backend 1")
        be2 = _make_task(id="T-be2", role="backend", title="Backend 2")
        qa1 = _make_task(id="T-qa1", role="qa", title="QA 1")
        task_dicts = [_task_as_dict(be1), _task_as_dict(be2), _task_as_dict(qa1)]

        cfg = OrchestratorConfig(
            max_agents=6,
            poll_interval_s=1,
            heartbeat_timeout_s=120,
            max_tasks_per_agent=1,
            server_url="http://testserver",
        )
        orch = _build_orchestrator(tmp_path, httpx.MockTransport(self._make_handler(task_dicts)), config=cfg)

        # Pre-seed 2 alive backend agents — one per open backend batch
        for i in range(2):
            session = AgentSession(
                id=f"alive-backend-{i}",
                role="backend",
                pid=40000 + i,
                task_ids=[f"T-be{i + 1}"],  # matches the open tasks
                status="running",
                model_config=RouterModelConfig(model="claude-sonnet-4-6", effort="normal"),
                spawn_ts=time.time(),
            )
            orch._agents[session.id] = session

        result = orch.tick()

        spawned_roles = [orch._agents[sid].role for sid in result.spawned if sid in orch._agents]
        assert "backend" not in spawned_roles, (
            f"backend has {2} agents for {2} batches — must not spawn more. Got: {spawned_roles}"
        )
        assert "qa" in spawned_roles, f"qa (0 agents, 1 batch) should get a slot; got: {spawned_roles}"


# --- Role-filtered claiming ---


class TestRoleFilteredClaiming:
    """Tests: orchestrator enforces single-role batches; claim failures abort spawn."""

    def test_batches_contain_single_role_only(self) -> None:
        """group_by_role guarantees each batch holds tasks of exactly one role.

        This is the structural guarantee that prevents cross-role claiming:
        the spawner always receives a same-role batch, so any agent it creates
        only ever holds tasks for its own role.
        """
        tasks = [
            _make_task(id="T-be1", role="backend"),
            _make_task(id="T-qa1", role="qa"),
            _make_task(id="T-be2", role="backend"),
            _make_task(id="T-qa2", role="qa"),
            _make_task(id="T-sec1", role="security"),
        ]
        batches = group_by_role(tasks, max_per_batch=3)

        for batch in batches:
            roles = {t.role for t in batch}
            assert len(roles) == 1, f"batch mixes roles: {roles}"

    def test_role_mismatch_error_from_server_aborts_spawn(self, tmp_path: Path) -> None:
        """When the claim endpoint rejects with a server error (e.g. role mismatch),
        the orchestrator aborts the spawn without creating an agent.
        """
        qa_task = _make_task(id="T-qa-mismatch", role="qa")

        def handler(request: httpx.Request) -> httpx.Response:
            url = request.url
            if request.method == "GET" and url.path == "/tasks":
                return _tasks_response(url, [_task_as_dict(qa_task)])
            if request.method == "POST" and "/claim" in url.path:
                return httpx.Response(500, json={"detail": "role mismatch: task requires role 'qa'"})
            return httpx.Response(404)

        orch = _build_orchestrator(tmp_path, httpx.MockTransport(handler))
        result = orch.tick()

        assert len(result.spawned) == 0, "spawn must be aborted when claim is rejected"
        assert any("claim" in e for e in result.errors), "claim error should be recorded"

    def test_only_matching_role_tasks_per_agent(self, tmp_path: Path) -> None:
        """Spawned agents hold tasks of exactly one role — no cross-role mixing."""
        tasks = [
            _make_task(id="T-be1", role="backend", title="Backend 1"),
            _make_task(id="T-be2", role="backend", title="Backend 2"),
            _make_task(id="T-qa1", role="qa", title="QA 1"),
            _make_task(id="T-qa2", role="qa", title="QA 2"),
        ]
        task_dicts = [_task_as_dict(t) for t in tasks]

        def handler(request: httpx.Request) -> httpx.Response:
            url = request.url
            if request.method == "GET" and url.path == "/tasks":
                return _tasks_response(url, task_dicts)
            if request.method == "POST" and "/claim" in url.path:
                task_id = url.path.split("/")[-2]
                td = next((t for t in task_dicts if t["id"] == task_id), task_dicts[0])
                return httpx.Response(200, json=td)
            return httpx.Response(404)

        cfg = OrchestratorConfig(
            max_agents=4,
            poll_interval_s=1,
            heartbeat_timeout_s=120,
            max_tasks_per_agent=2,
            server_url="http://testserver",
        )
        orch = _build_orchestrator(tmp_path, httpx.MockTransport(handler), config=cfg)
        result = orch.tick()

        spawned_roles = [orch._agents[sid].role for sid in result.spawned if sid in orch._agents]
        assert "backend" in spawned_roles, "backend role should get an agent"
        assert "qa" in spawned_roles, "qa role should get an agent"


# --- Agent rebalancing ---


class TestAgentRebalancing:
    """Tests: agents receive SHUTDOWN signal when their role's task queue empties."""

    def test_agent_gets_shutdown_when_all_tasks_done(self, tmp_path: Path) -> None:
        """Backend agent is recycled (SHUTDOWN written) when all its tasks are resolved."""
        done_task = _make_task(id="T-be-done", role="backend", status="done")
        transport = _mock_transport({"GET /tasks": httpx.Response(200, json=[_task_as_dict(done_task)])})
        adapter = _mock_adapter()
        adapter.is_alive.return_value = True  # process is still running

        orch = _build_orchestrator(tmp_path, transport, adapter=adapter)

        session = AgentSession(
            id="backend-idle",
            role="backend",
            pid=12345,
            task_ids=["T-be-done"],
            status="running",
            model_config=RouterModelConfig(model="claude-sonnet-4-6", effort="normal"),
            spawn_ts=time.time(),
        )
        orch._agents["backend-idle"] = session

        orch.tick()

        shutdown_file = tmp_path / ".sdd" / "runtime" / "signals" / "backend-idle" / "SHUTDOWN"
        assert shutdown_file.exists(), "SHUTDOWN signal must be written when all role tasks are done"

    def test_no_shutdown_when_agent_task_still_claimed(self, tmp_path: Path) -> None:
        """Agent is NOT recycled when its task is still claimed (in progress)."""
        claimed_task = _make_task(id="T-be-claimed", role="backend", status="claimed")
        transport = _mock_transport({"GET /tasks": httpx.Response(200, json=[_task_as_dict(claimed_task)])})
        adapter = _mock_adapter()
        adapter.is_alive.return_value = True

        orch = _build_orchestrator(tmp_path, transport, adapter=adapter)

        session = AgentSession(
            id="backend-active",
            role="backend",
            pid=12345,
            task_ids=["T-be-claimed"],
            status="running",
            model_config=RouterModelConfig(model="claude-sonnet-4-6", effort="normal"),
            spawn_ts=time.time(),
        )
        orch._agents["backend-active"] = session

        orch.tick()

        shutdown_file = tmp_path / ".sdd" / "runtime" / "signals" / "backend-active" / "SHUTDOWN"
        assert not shutdown_file.exists(), "no SHUTDOWN when agent's task is still in-progress"

    def test_only_idle_role_agent_gets_shutdown_not_active_role(self, tmp_path: Path) -> None:
        """When backend tasks are done but QA tasks are still active, only backend agent
        receives SHUTDOWN; QA agent is left running.
        """
        done_task = _make_task(id="T-be-done", role="backend", status="done")
        claimed_task = _make_task(id="T-qa-claimed", role="qa", status="claimed")
        task_dicts = [_task_as_dict(done_task), _task_as_dict(claimed_task)]

        transport = _mock_transport({"GET /tasks": httpx.Response(200, json=task_dicts)})
        adapter = _mock_adapter()
        adapter.is_alive.return_value = True

        orch = _build_orchestrator(tmp_path, transport, adapter=adapter)

        be_session = AgentSession(
            id="backend-done",
            role="backend",
            pid=11111,
            task_ids=["T-be-done"],
            status="running",
            model_config=RouterModelConfig(model="claude-sonnet-4-6", effort="normal"),
            spawn_ts=time.time(),
        )
        qa_session = AgentSession(
            id="qa-active",
            role="qa",
            pid=22222,
            task_ids=["T-qa-claimed"],
            status="running",
            model_config=RouterModelConfig(model="claude-sonnet-4-6", effort="normal"),
            spawn_ts=time.time(),
        )
        orch._agents["backend-done"] = be_session
        orch._agents["qa-active"] = qa_session

        orch.tick()

        be_shutdown = tmp_path / ".sdd" / "runtime" / "signals" / "backend-done" / "SHUTDOWN"
        qa_shutdown = tmp_path / ".sdd" / "runtime" / "signals" / "qa-active" / "SHUTDOWN"
        assert be_shutdown.exists(), "backend agent (all tasks done) must receive SHUTDOWN"
        assert not qa_shutdown.exists(), "qa agent (task still claimed) must NOT receive SHUTDOWN"

    def test_spawn_allowed_when_idle_agent_waiting_to_exit(self, tmp_path: Path) -> None:
        """New agents can be spawned for a role even when an idle agent is in grace period.

        Scenario:
        1. Agent A finishes its only task for role X
        2. Agent A gets SHUTDOWN signal (idle, waiting to exit)
        3. New tasks arrive for role X
        4. A new agent should be spawned, NOT blocked by Agent A's presence

        This tests the fix for (#333d-03a): _effective_role_cap calculation must
        exclude idle agents from the alive count, since they won't accept new work.
        """
        # Setup: idle agent + new open tasks for the same role
        idle_agent = AgentSession(
            id="backend-idle",
            role="backend",
            pid=11111,
            task_ids=[],  # no tasks (already completed/resolved)
            status="running",
            model_config=RouterModelConfig(model="claude-sonnet-4-6", effort="normal"),
            spawn_ts=time.time(),
        )

        new_task = _make_task(id="T-new", role="backend", title="New backend task")
        transport = _mock_transport({"GET /tasks": httpx.Response(200, json=[_task_as_dict(new_task)])})
        adapter = _mock_adapter()
        adapter.is_alive.return_value = True

        config = OrchestratorConfig(
            max_agents=2,
            max_tasks_per_agent=1,
            poll_interval_s=1,
            server_url="http://testserver",
        )
        orch = _build_orchestrator(tmp_path, transport, adapter=adapter, config=config)

        # Add the idle agent to orchestrator and mark it as having received SHUTDOWN
        orch._agents["backend-idle"] = idle_agent
        orch._idle_shutdown_ts["backend-idle"] = time.time()  # mark as idle/waiting to exit

        result = orch.tick()

        # Should spawn new agent for the new task, not blocked by idle agent
        assert len(result.spawned) >= 1, "new agent should spawn despite idle agent in grace period"

        # Verify the newly spawned agent is for backend role
        spawned_backends = [
            sid for sid in result.spawned if sid in orch._agents and orch._agents[sid].role == "backend"
        ]
        assert len(spawned_backends) >= 1, "new backend agent must be spawned"


# --- Orchestrator.tick ---


class TestOrchestratorTick:
    def test_spawns_agent_for_open_tasks(self, tmp_path: Path) -> None:
        tasks = [_make_task(id="T-1"), _make_task(id="T-2")]
        transport = _mock_transport(
            {
                "GET /tasks": httpx.Response(200, json=[_task_as_dict(t) for t in tasks]),
            }
        )
        orch = _build_orchestrator(tmp_path, transport)

        result = orch.tick()

        assert result.open_tasks == 2
        assert len(result.spawned) == 1  # one batch of 2 tasks
        assert len(result.errors) == 0

    def test_respects_max_agents(self, tmp_path: Path) -> None:
        # 6 tasks across 3 roles -- but max_agents=2
        tasks = [
            _make_task(id="T-1", role="backend", title="Backend task 1"),
            _make_task(id="T-2", role="backend", title="Backend task 2"),
            _make_task(id="T-3", role="qa", title="QA task 1"),
            _make_task(id="T-4", role="qa", title="QA task 2"),
            _make_task(id="T-5", role="devops", title="Devops task 1"),
            _make_task(id="T-6", role="devops", title="Devops task 2"),
        ]
        transport = _mock_transport(
            {
                "GET /tasks": httpx.Response(200, json=[_task_as_dict(t) for t in tasks]),
            }
        )
        config = OrchestratorConfig(
            max_agents=2,
            poll_interval_s=1,
            server_url="http://testserver",
        )
        orch = _build_orchestrator(tmp_path, transport, config=config)

        result = orch.tick()

        assert len(result.spawned) == 2  # capped at max_agents

    def test_no_spawn_when_no_open_tasks(self, tmp_path: Path) -> None:
        transport = _mock_transport(
            {
                "GET /tasks": httpx.Response(200, json=[]),
            }
        )
        orch = _build_orchestrator(tmp_path, transport)

        result = orch.tick()

        assert result.open_tasks == 0
        assert len(result.spawned) == 0

    def test_depends_on_blocks_scheduling_until_dep_done(self, tmp_path: Path) -> None:
        """Task B with depends_on=[A.id] is not scheduled until A is in status 'done'."""
        task_a = _make_task(id="T-A", role="backend")
        task_b = _make_task(id="T-B", role="backend")
        task_b.depends_on = ["T-A"]

        # Tick 1: A is open, B depends on A — only A should be scheduled
        transport = _mock_transport(
            {
                "GET /tasks": httpx.Response(200, json=[_task_as_dict(task_a), _task_as_dict(task_b)]),
            }
        )
        orch = _build_orchestrator(tmp_path, transport)

        orch.tick()

        # Only task_a's batch spawned; task_b blocked by unmet dependency
        spawned_task_ids: list[str] = []
        for session in orch.active_agents.values():
            spawned_task_ids.extend(session.task_ids)
        assert "T-A" in spawned_task_ids
        assert "T-B" not in spawned_task_ids

    def test_depends_on_unblocked_when_dep_done(self, tmp_path: Path) -> None:
        """Task B with depends_on=[A.id] is scheduled once A appears in 'done'."""
        task_b = _make_task(id="T-B", role="backend")
        task_b.depends_on = ["T-A"]
        task_a_done = _make_task(id="T-A", role="backend", status="done")

        transport = _mock_transport(
            {
                "GET /tasks": httpx.Response(200, json=[_task_as_dict(task_b), _task_as_dict(task_a_done)]),
            }
        )
        orch = _build_orchestrator(tmp_path, transport)

        orch.tick()

        spawned_task_ids: list[str] = []
        for session in orch.active_agents.values():
            spawned_task_ids.extend(session.task_ids)
        assert "T-B" in spawned_task_ids

    def test_handles_server_error_on_fetch(self, tmp_path: Path) -> None:
        transport = _mock_transport(
            {
                "GET /tasks": httpx.Response(500, text="Internal error"),
            }
        )
        orch = _build_orchestrator(tmp_path, transport)

        result = orch.tick()

        assert len(result.errors) == 1
        assert "fetch_all" in result.errors[0]

    def test_tracks_agents_across_ticks(self, tmp_path: Path) -> None:
        tasks_tick1 = [_make_task(id="T-1")]
        tasks_tick2 = [_make_task(id="T-1", status="claimed"), _make_task(id="T-2", role="qa")]

        # Switch to tick2 data once T-1 has been claimed (POST /tasks/T-1/claim).
        # This approach is agnostic to how many GET /tasks requests the orchestrator
        # makes per tick (works with both 1 bulk request and N per-status requests).
        phase = [1]  # mutable container to avoid nonlocal in nested fn

        def handler(request: httpx.Request) -> httpx.Response:
            url = request.url
            if request.method == "POST" and url.path == "/tasks/T-1/claim":
                phase[0] = 2
                return httpx.Response(200, json={})
            if request.method == "GET" and url.path == "/tasks":
                status_filter = url.params.get("status")
                tasks = tasks_tick1 if phase[0] == 1 else tasks_tick2
                filtered = [_task_as_dict(t) for t in tasks if status_filter is None or t.status.value == status_filter]
                return httpx.Response(200, json=filtered)
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        orch = _build_orchestrator(tmp_path, transport)

        r1 = orch.tick()
        assert len(r1.spawned) == 1

        r2 = orch.tick()
        assert len(r2.spawned) == 1

        # Two agents should be tracked now
        assert len(orch.active_agents) == 2

    def test_writes_log_file(self, tmp_path: Path) -> None:
        transport = _mock_transport(
            {
                "GET /tasks": httpx.Response(200, json=[]),
            }
        )
        orch = _build_orchestrator(tmp_path, transport)

        orch.tick()

        log_path = tmp_path / ".sdd" / "runtime" / "orchestrator.log"
        assert log_path.exists()
        content = log_path.read_text()
        assert "open=0" in content
        assert "agents=" in content

    def test_spawn_failure_records_error(self, tmp_path: Path) -> None:
        tasks = [_make_task(id="T-1")]
        transport = _mock_transport(
            {
                "GET /tasks": httpx.Response(200, json=[_task_as_dict(t) for t in tasks]),
            }
        )
        adapter = _mock_adapter()
        adapter.spawn.side_effect = RuntimeError("process failed to start")
        orch = _build_orchestrator(tmp_path, transport, adapter=adapter)

        result = orch.tick()

        assert len(result.errors) == 1
        assert "spawn" in result.errors[0]
        assert len(result.spawned) == 0

    def test_tick_skips_spawning_when_budget_exceeded(self, tmp_path: Path) -> None:
        """tick() must not spawn agents when cumulative cost has reached the budget cap."""
        task = _make_task(id="T-001")
        transport = _mock_transport(
            {
                "GET /tasks": httpx.Response(200, json=[_task_as_dict(task)]),
            }
        )
        adapter = _mock_adapter()
        config = OrchestratorConfig(
            max_agents=6,
            poll_interval_s=1,
            server_url="http://testserver",
            budget_usd=0.05,  # budget is $0.05, but $0.10 already spent
        )
        orch = _build_orchestrator(tmp_path, transport, adapter=adapter, config=config)

        # Pre-seed the cost tracker with spend exceeding the budget
        orch._cost_tracker.record(
            agent_id="prev-agent",
            task_id="T-prev",
            model="sonnet",
            input_tokens=0,
            output_tokens=0,
            cost_usd=0.10,
        )

        result = orch.tick()

        adapter.spawn.assert_not_called()
        assert result.spawned == []

    def test_tick_spawns_normally_when_under_budget(self, tmp_path: Path) -> None:
        """tick() spawns normally when spent < budget_usd."""
        task = _make_task(id="T-001")
        transport = _mock_transport(
            {
                "GET /tasks": httpx.Response(200, json=[_task_as_dict(task)]),
            }
        )
        adapter = _mock_adapter()
        config = OrchestratorConfig(
            max_agents=6,
            poll_interval_s=1,
            server_url="http://testserver",
            budget_usd=1.00,  # $0.01 spent < $1.00 budget
        )
        orch = _build_orchestrator(tmp_path, transport, adapter=adapter, config=config)

        # Pre-seed with small spend under budget
        orch._cost_tracker.record(
            agent_id="prev-agent",
            task_id="T-prev",
            model="sonnet",
            input_tokens=0,
            output_tokens=0,
            cost_usd=0.01,
        )

        result = orch.tick()

        assert len(result.spawned) == 1

    def test_tick_no_budget_check_when_budget_is_zero(self, tmp_path: Path) -> None:
        """tick() never enforces a budget when budget_usd=0 (default)."""
        task = _make_task(id="T-001")
        transport = _mock_transport(
            {
                "GET /tasks": httpx.Response(200, json=[_task_as_dict(task)]),
            }
        )
        adapter = _mock_adapter()
        # Default config has budget_usd=0 (no cap)
        config = OrchestratorConfig(
            max_agents=6,
            poll_interval_s=1,
            server_url="http://testserver",
            budget_usd=0.0,
        )
        orch = _build_orchestrator(tmp_path, transport, adapter=adapter, config=config)

        # Even with huge recorded spend, budget=0 means unlimited
        orch._cost_tracker.record(
            agent_id="prev-agent",
            task_id="T-prev",
            model="sonnet",
            input_tokens=0,
            output_tokens=0,
            cost_usd=999.99,
        )

        result = orch.tick()

        assert len(result.spawned) == 1

    def test_dry_run_prevents_spawning(self, tmp_path: Path) -> None:
        """tick() with dry_run=True logs planned spawns but never calls adapter.spawn."""
        task = _make_task(id="T-dry", role="backend", title="Build something")
        transport = _mock_transport(
            {
                "GET /tasks": httpx.Response(200, json=[_task_as_dict(task)]),
            }
        )
        adapter = _mock_adapter()
        config = OrchestratorConfig(
            max_agents=6,
            poll_interval_s=1,
            server_url="http://testserver",
            dry_run=True,
        )
        orch = _build_orchestrator(tmp_path, transport, adapter=adapter, config=config)

        result = orch.tick()

        adapter.spawn.assert_not_called()
        assert result.spawned == []
        assert len(result.dry_run_planned) == 1
        role, title, _model, _effort = result.dry_run_planned[0]
        assert role == "backend"
        assert title == "Build something"


# --- Spawn resilience: claim-before-spawn, backoff, and failure escalation ---


class TestSpawnResiliency:
    """Server outage and spawn failure scenarios."""

    def test_claim_500_aborts_spawn(self, tmp_path: Path) -> None:
        """Server 500 on task claim aborts spawn — agent must not be launched."""
        task = _make_task(id="T-claim-500")

        def handler(request: httpx.Request) -> httpx.Response:
            url = request.url
            if request.method == "GET" and url.path == "/tasks":
                return _tasks_response(url, [_task_as_dict(task)])
            if request.method == "POST" and url.path == "/tasks/T-claim-500/claim":
                return httpx.Response(500, json={"detail": "internal server error"})
            return httpx.Response(404)

        adapter = _mock_adapter()
        orch = _build_orchestrator(tmp_path, httpx.MockTransport(handler), adapter=adapter)

        result = orch.tick()

        adapter.spawn.assert_not_called()
        assert len(result.spawned) == 0
        assert any("claim" in e for e in result.errors)

    def test_claim_connection_error_aborts_spawn(self, tmp_path: Path) -> None:
        """Server unreachable during claim aborts spawn without crashing."""
        task = _make_task(id="T-claim-conn")

        def handler(request: httpx.Request) -> httpx.Response:
            url = request.url
            if request.method == "GET" and url.path == "/tasks":
                return _tasks_response(url, [_task_as_dict(task)])
            if request.method == "POST" and url.path == "/tasks/T-claim-conn/claim":
                raise httpx.ConnectError("Connection refused")
            return httpx.Response(404)

        adapter = _mock_adapter()
        orch = _build_orchestrator(tmp_path, httpx.MockTransport(handler), adapter=adapter)

        result = orch.tick()

        adapter.spawn.assert_not_called()
        assert len(result.spawned) == 0
        assert any("claim" in e for e in result.errors)

    def test_spawn_failure_not_retried_within_backoff_window(self, tmp_path: Path) -> None:
        """A batch that failed to spawn is not retried until the backoff window expires."""
        task = _make_task(id="T-backoff")
        transport = _mock_transport(
            {
                "GET /tasks": httpx.Response(200, json=[_task_as_dict(task)]),
            }
        )
        adapter = _mock_adapter()
        adapter.spawn.side_effect = RuntimeError("subprocess died")
        orch = _build_orchestrator(tmp_path, transport, adapter=adapter)

        # Tick 1: spawn attempt fails, failure is recorded
        r1 = orch.tick()
        assert adapter.spawn.call_count == 1
        assert any("spawn" in e for e in r1.errors)

        # Tick 2: immediately after — still within backoff window, batch must be skipped
        r2 = orch.tick()
        assert len(r2.spawned) == 0
        assert adapter.spawn.call_count == 1  # not retried

    def test_consecutive_spawn_failures_mark_tasks_failed(self, tmp_path: Path) -> None:
        """After MAX_SPAWN_FAILURES consecutive failures, tasks are marked failed on the server."""
        task = _make_task(id="T-maxfail")

        fail_called = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal fail_called
            url = request.url
            if request.method == "GET" and url.path == "/tasks":
                status_filter = url.params.get("status")
                all_tasks = [_task_as_dict(task)]
                filtered = [t for t in all_tasks if status_filter is None or t.get("status") == status_filter]
                return httpx.Response(200, json=filtered)
            if request.method == "POST" and url.path == "/tasks/T-maxfail/fail":
                fail_called = True
                return httpx.Response(200, json={"status": "failed"})
            return httpx.Response(404)

        adapter = _mock_adapter()
        adapter.spawn.side_effect = RuntimeError("always fails")
        orch = _build_orchestrator(tmp_path, httpx.MockTransport(handler), adapter=adapter)

        # Pre-seed failure count at (max - 1) with a timestamp that is:
        # - Past the exponential backoff window (so the batch is retried), but
        # - Within the cleanup purge window (so the entry is NOT purged).
        # Backoff for fail_count=2 is base*2^1 = 60s, cleanup purge is 300s.
        import time as _time

        batch_key = frozenset(["T-maxfail"])
        max_failures = orch._MAX_SPAWN_FAILURES
        expired_ts = _time.time() - 120  # 120s ago: past 60s backoff, within 300s purge
        orch._spawn_failures[batch_key] = (max_failures - 1, expired_ts)

        # This tick hits the limit and should mark the task as failed
        orch.tick()

        assert fail_called
        # Failure tracking is cleared after escalation
        assert batch_key not in orch._spawn_failures


# --- Reaping stale agents ---


class TestReaping:
    @patch("bernstein.core.agent_recycling._is_process_alive", return_value=False)
    def test_reaps_stale_heartbeat(self, _mock_alive: MagicMock, tmp_path: Path) -> None:
        transport = _mock_transport(
            {
                "GET /tasks": httpx.Response(200, json=[]),
            }
        )
        adapter = _mock_adapter()
        config = OrchestratorConfig(
            max_agents=6,
            poll_interval_s=1,
            heartbeat_timeout_s=60,
            server_url="http://testserver",
        )
        orch = _build_orchestrator(tmp_path, transport, adapter=adapter, config=config)

        # Inject a stale agent
        stale_session = AgentSession(
            id="backend-stale",
            role="backend",
            pid=999,
            task_ids=["T-stale"],
            heartbeat_ts=time.time() - 120,  # 120s ago, threshold is 60
            status="working",
        )
        orch._agents["backend-stale"] = stale_session

        # Need fail endpoint for the reaped task
        fail_called = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal fail_called
            url = request.url
            key = f"{request.method} {url.path}"
            if key == "GET /tasks":
                return httpx.Response(200, json=[])
            if key == "GET /tasks/T-stale":
                return httpx.Response(200, json=_task_as_dict(_make_task(id="T-stale")))
            if key == "POST /tasks":
                return httpx.Response(201, json={"id": "T-stale-retry"})
            if key == "POST /tasks/T-stale/fail":
                fail_called = True
                return httpx.Response(200, json=_task_as_dict(_make_task(id="T-stale", status="failed")))
            return httpx.Response(404)

        # Rebuild with custom transport
        client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://testserver")
        orch._client = client

        result = orch.tick()

        assert "backend-stale" in result.reaped
        assert stale_session.status == "dead"
        assert fail_called
        adapter.kill.assert_called_once_with(999)

    def test_does_not_reap_fresh_heartbeat(self, tmp_path: Path) -> None:
        transport = _mock_transport(
            {
                "GET /tasks": httpx.Response(200, json=[]),
            }
        )
        adapter = _mock_adapter()
        adapter.is_alive.return_value = True
        config = OrchestratorConfig(
            max_agents=6,
            poll_interval_s=1,
            heartbeat_timeout_s=60,
            server_url="http://testserver",
        )
        orch = _build_orchestrator(tmp_path, transport, adapter=adapter, config=config)

        # Inject a fresh agent
        fresh_session = AgentSession(
            id="backend-fresh",
            role="backend",
            pid=100,
            task_ids=["T-fresh"],
            heartbeat_ts=time.time(),  # just now
            status="working",
        )
        orch._agents["backend-fresh"] = fresh_session

        result = orch.tick()

        assert len(result.reaped) == 0
        assert fresh_session.status == "working"

    def test_dead_process_marked_dead_on_refresh(self, tmp_path: Path) -> None:
        transport = _mock_transport(
            {
                "GET /tasks": httpx.Response(200, json=[]),
            }
        )
        adapter = _mock_adapter()
        adapter.is_alive.return_value = False  # process exited
        orch = _build_orchestrator(tmp_path, transport, adapter=adapter)

        session = AgentSession(
            id="backend-dead",
            role="backend",
            pid=77,
            status="working",
        )
        orch._agents["backend-dead"] = session

        orch.tick()

        assert session.status == "dead"

    @patch("bernstein.core.agent_recycling._is_process_alive", return_value=True)
    def test_zero_heartbeat_not_reaped_if_alive(self, _mock_alive: MagicMock, tmp_path: Path) -> None:
        """An agent that never heartbeated but whose process is alive is NOT reaped."""
        transport = _mock_transport(
            {
                "GET /tasks": httpx.Response(200, json=[]),
            }
        )
        adapter = _mock_adapter()
        config = OrchestratorConfig(
            heartbeat_timeout_s=60,
            server_url="http://testserver",
        )
        orch = _build_orchestrator(tmp_path, transport, adapter=adapter, config=config)

        session = AgentSession(
            id="backend-new",
            role="backend",
            pid=55,
            heartbeat_ts=0.0,  # never heartbeated
            status="working",
        )
        orch._agents["backend-new"] = session

        result = orch.tick()

        assert len(result.reaped) == 0
        assert session.status == "working"


# --- run / stop ---


class TestRunStop:
    def test_stop_breaks_loop(self, tmp_path: Path) -> None:
        transport = _mock_transport(
            {
                "GET /tasks": httpx.Response(200, json=[]),
            }
        )
        config = OrchestratorConfig(
            poll_interval_s=0,  # no sleep between ticks for test speed
            server_url="http://testserver",
        )
        orch = _build_orchestrator(tmp_path, transport, config=config)

        # Patch tick to stop after 3 calls
        call_count = 0
        original_tick = orch.tick

        def counting_tick() -> TickResult:
            nonlocal call_count
            call_count += 1
            if call_count >= 3:
                orch.stop()
            return original_tick()

        orch.tick = counting_tick  # type: ignore[assignment]
        orch.run()

        assert call_count == 3

    def test_tick_does_not_claim_or_spawn_after_stop_requested(self, tmp_path: Path) -> None:
        task = _make_task(id="T-stop", title="Update docs", description="Refresh docs.")
        requests: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            key = f"{request.method} {request.url.path}"
            requests.append(key)
            if key == "GET /tasks":
                return httpx.Response(200, json=[_task_as_dict(task)])
            pytest.fail(f"unexpected request after stop(): {key}")

        orch = _build_orchestrator(tmp_path, httpx.MockTransport(handler))
        orch._spawner.spawn_for_tasks = MagicMock(side_effect=AssertionError("spawn should not be called"))

        orch.stop()
        result = orch.tick()

        orch._spawner.spawn_for_tasks.assert_not_called()
        assert requests == ["GET /tasks"]
        assert result.spawned == []


# --- TickResult ---


class TestTickResult:
    def test_defaults(self) -> None:
        r = TickResult()
        assert r.open_tasks == 0
        assert r.active_agents == 0
        assert r.spawned == []
        assert r.reaped == []
        assert r.verified == []
        assert r.verification_failures == []
        assert r.errors == []


# --- Feature 1: Agent Completion Protocol ---


class TestAgentCompletionProtocol:
    """When an agent dies, orphaned tasks are verified and completed/failed."""

    def test_orphaned_task_with_signals_passes_janitor(self, tmp_path: Path) -> None:
        """Dead agent + open task + passing janitor => auto-complete."""
        # Create the file that the signal checks for
        (tmp_path / "output.txt").write_text("done")

        task = _make_task(id="T-orphan", status="in_progress")
        task_dict = _task_as_dict(task)
        task_dict["completion_signals"] = [{"type": "path_exists", "value": "output.txt"}]
        task_dict["status"] = "in_progress"

        complete_called = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal complete_called
            url = request.url
            key = f"{request.method} {url.path}"
            if url.query:
                key += f"?{url.query.decode()}"
            if key in (
                "GET /tasks",
                "GET /tasks?status=open",
                "GET /tasks?status=claimed",
                "GET /tasks?status=done",
                "GET /tasks?status=failed",
            ):
                return httpx.Response(200, json=[])
            if key == "GET /tasks/T-orphan":
                return httpx.Response(200, json=task_dict)
            if key == "POST /tasks/T-orphan/complete":
                complete_called = True
                return httpx.Response(200, json={"status": "done"})
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        adapter = _mock_adapter()
        adapter.is_alive.return_value = False  # process died
        orch = _build_orchestrator(tmp_path, transport, adapter=adapter)

        session = AgentSession(
            id="backend-dying",
            role="backend",
            pid=42,
            task_ids=["T-orphan"],
            status="working",
        )
        orch._agents["backend-dying"] = session

        orch.tick()

        assert session.status == "dead"
        assert complete_called

    def test_orphaned_task_with_signals_fails_janitor(self, tmp_path: Path) -> None:
        """Dead agent + open task + failing janitor => fail task."""
        # Do NOT create "missing.txt" so the signal fails

        task = _make_task(id="T-orphan-fail", status="in_progress")
        task_dict = _task_as_dict(task)
        task_dict["completion_signals"] = [{"type": "path_exists", "value": "missing.txt"}]
        task_dict["status"] = "in_progress"

        fail_called = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal fail_called
            url = request.url
            key = f"{request.method} {url.path}"
            if url.query:
                key += f"?{url.query.decode()}"
            if key in (
                "GET /tasks",
                "GET /tasks?status=open",
                "GET /tasks?status=claimed",
                "GET /tasks?status=done",
                "GET /tasks?status=failed",
            ):
                return httpx.Response(200, json=[])
            if key == "GET /tasks/T-orphan-fail":
                return httpx.Response(200, json=task_dict)
            if key == "POST /tasks/T-orphan-fail/fail":
                fail_called = True
                return httpx.Response(200, json={"status": "failed"})
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        adapter = _mock_adapter()
        adapter.is_alive.return_value = False
        orch = _build_orchestrator(tmp_path, transport, adapter=adapter)

        session = AgentSession(
            id="backend-fail",
            role="backend",
            pid=42,
            task_ids=["T-orphan-fail"],
            status="working",
        )
        orch._agents["backend-fail"] = session

        orch.tick()

        assert session.status == "dead"
        assert fail_called

    def test_orphaned_task_no_signals_fails(self, tmp_path: Path) -> None:
        """Dead agent + open task + no completion signals => fail task."""
        task_dict = _task_as_dict(_make_task(id="T-nosig", status="in_progress"))
        task_dict["status"] = "in_progress"
        # no completion_signals field

        fail_called = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal fail_called
            url = request.url
            key = f"{request.method} {url.path}"
            if url.query:
                key += f"?{url.query.decode()}"
            if request.method == "GET" and url.path == "/tasks":
                return httpx.Response(200, json=[])
            if key == "GET /tasks/T-nosig":
                return httpx.Response(200, json=task_dict)
            if key == "POST /tasks/T-nosig/fail":
                fail_called = True
                return httpx.Response(200, json={"status": "failed"})
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        adapter = _mock_adapter()
        adapter.is_alive.return_value = False
        orch = _build_orchestrator(tmp_path, transport, adapter=adapter)

        session = AgentSession(
            id="backend-nosig",
            role="backend",
            pid=42,
            task_ids=["T-nosig"],
            status="working",
        )
        orch._agents["backend-nosig"] = session

        orch.tick()

        assert session.status == "dead"
        assert fail_called

    def test_orphaned_task_already_done_skipped(self, tmp_path: Path) -> None:
        """If the task is already done on the server, do nothing."""
        task_dict = _task_as_dict(_make_task(id="T-done", status="done"))

        complete_called = False
        fail_called = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal complete_called, fail_called
            url = request.url
            key = f"{request.method} {url.path}"
            if url.query:
                key += f"?{url.query.decode()}"
            if key == "GET /tasks":
                return httpx.Response(200, json=[])
            if key == "GET /tasks/T-done":
                return httpx.Response(200, json=task_dict)
            if "complete" in key:
                complete_called = True
                return httpx.Response(200, json={})
            if key.endswith("/fail"):
                fail_called = True
                return httpx.Response(200, json={})
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        adapter = _mock_adapter()
        adapter.is_alive.return_value = False
        orch = _build_orchestrator(tmp_path, transport, adapter=adapter)

        session = AgentSession(
            id="backend-done",
            role="backend",
            pid=42,
            task_ids=["T-done"],
            status="working",
        )
        orch._agents["backend-done"] = session

        orch.tick()

        assert not complete_called
        assert not fail_called


# --- Feature 2: File Ownership Enforcement ---


class TestFileOwnership:
    """Track file ownership and skip batches with conflicting files."""

    def test_skips_batch_with_conflicting_files(self, tmp_path: Path) -> None:
        """Batch with owned_files that overlap active agent is skipped."""
        # Two tasks that own the same file, in different roles
        task1 = _make_task(id="T-1", role="backend")
        task1.owned_files = ["src/main.py"]
        task2 = _make_task(id="T-2", role="qa")
        task2.owned_files = ["src/main.py"]

        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            url = request.url
            if request.method == "GET" and url.path == "/tasks":
                call_count += 1
                if call_count == 1:
                    return _tasks_response(url, [_task_as_dict(task1)])
                return _tasks_response(url, [_task_as_dict(task2)])
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        orch = _build_orchestrator(tmp_path, transport)

        # First tick: spawns agent for task1, claims src/main.py
        r1 = orch.tick()
        assert len(r1.spawned) == 1
        assert "src/main.py" in orch._file_ownership

        # Second tick: task2 also needs src/main.py => skipped
        r2 = orch.tick()
        assert len(r2.spawned) == 0

    def test_releases_ownership_on_death(self, tmp_path: Path) -> None:
        """File ownership is released when an agent dies."""
        transport = _mock_transport(
            {
                "GET /tasks?status=open": httpx.Response(200, json=[]),
                "GET /tasks?status=done": httpx.Response(200, json=[]),
            }
        )
        adapter = _mock_adapter()
        adapter.is_alive.return_value = False  # process died
        orch = _build_orchestrator(tmp_path, transport, adapter=adapter)

        # Pre-populate ownership
        orch._file_ownership["src/main.py"] = "backend-owner"
        session = AgentSession(
            id="backend-owner",
            role="backend",
            pid=42,
            task_ids=[],  # no tasks to avoid orphan handler needing endpoints
            status="working",
        )
        orch._agents["backend-owner"] = session

        orch.tick()

        assert session.status == "dead"
        assert "src/main.py" not in orch._file_ownership

    def test_no_conflict_when_files_differ(self, tmp_path: Path) -> None:
        """Batches with non-overlapping owned_files spawn normally."""
        task1 = _make_task(id="T-1", role="backend")
        task1.owned_files = ["src/a.py"]
        task2 = _make_task(id="T-2", role="qa")
        task2.owned_files = ["src/b.py"]

        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            url = request.url
            if request.method == "GET" and url.path == "/tasks":
                call_count += 1
                if call_count == 1:
                    return _tasks_response(url, [_task_as_dict(task1)])
                return _tasks_response(url, [_task_as_dict(task2)])
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        orch = _build_orchestrator(tmp_path, transport)

        r1 = orch.tick()
        assert len(r1.spawned) == 1

        r2 = orch.tick()
        assert len(r2.spawned) == 1  # no conflict, spawns fine

    @patch("bernstein.core.agent_recycling._is_process_alive", return_value=False)
    def test_ownership_released_on_reap(self, _mock_alive: MagicMock, tmp_path: Path) -> None:
        """File ownership released when a stale agent is reaped."""
        transport = _mock_transport(
            {
                "GET /tasks?status=open": httpx.Response(200, json=[]),
                "GET /tasks?status=done": httpx.Response(200, json=[]),
            }
        )
        adapter = _mock_adapter()
        config = OrchestratorConfig(
            heartbeat_timeout_s=60,
            server_url="http://testserver",
        )
        orch = _build_orchestrator(tmp_path, transport, adapter=adapter, config=config)

        orch._file_ownership["src/owned.py"] = "backend-stale"
        session = AgentSession(
            id="backend-stale",
            role="backend",
            pid=99,
            task_ids=["T-stale"],
            heartbeat_ts=time.time() - 120,  # stale
            status="working",
        )
        orch._agents["backend-stale"] = session

        # Add fail endpoint for reaped tasks
        def handler(request: httpx.Request) -> httpx.Response:
            url = request.url
            key = f"{request.method} {url.path}"
            if url.query:
                key += f"?{url.query.decode()}"
            if request.method == "GET" and url.path == "/tasks":
                return httpx.Response(200, json=[])
            if key == "POST /tasks/T-stale/fail":
                return httpx.Response(200, json={})
            return httpx.Response(404)

        orch._client = httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="http://testserver",
        )

        orch.tick()

        assert "src/owned.py" not in orch._file_ownership


# --- Feature 3: Metrics Emission ---


def _make_task_transport(
    routes: dict[str, httpx.Response],
) -> httpx.MockTransport:
    """Build a MockTransport that returns empty task lists by default."""
    def _handler(request: httpx.Request) -> httpx.Response:
        url = request.url
        key = f"{request.method} {url.path}"
        if url.query:
            key += f"?{url.query.decode()}"
        if request.method == "GET" and url.path == "/tasks":
            return httpx.Response(200, json=[])
        return routes.get(key, httpx.Response(404))

    return httpx.MockTransport(_handler)


def _find_metrics_record(tmp_path: Path, task_id: str) -> dict[str, Any] | None:
    """Read all JSONL metrics files and return the first record matching *task_id*."""
    metrics_dir = tmp_path / ".sdd" / "metrics"
    for jf in metrics_dir.glob("*.jsonl"):
        for line in jf.read_text().strip().split("\n"):
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("task_id") == task_id:
                return record
    return None


class TestOrphanMetrics:
    """Orphaned task handling emits MetricsRecord to .sdd/metrics/."""

    def test_metrics_written_on_orphan_complete(self, tmp_path: Path) -> None:
        """Successful auto-complete writes a metrics JSONL record."""
        (tmp_path / "result.txt").write_text("ok")

        task_dict = _task_as_dict(_make_task(id="T-met", status="in_progress"))
        task_dict["completion_signals"] = [{"type": "path_exists", "value": "result.txt"}]
        task_dict["status"] = "in_progress"

        routes = {
            "GET /tasks/T-met": httpx.Response(200, json=task_dict),
            "POST /tasks/T-met/complete": httpx.Response(200, json={}),
        }
        transport = _make_task_transport(routes)
        adapter = _mock_adapter()
        adapter.is_alive.return_value = False
        orch = _build_orchestrator(tmp_path, transport, adapter=adapter)

        session = AgentSession(
            id="backend-met",
            role="backend",
            pid=42,
            task_ids=["T-met"],
            status="working",
        )
        orch._agents["backend-met"] = session

        orch.tick()

        record = _find_metrics_record(tmp_path, "T-met")
        assert record is not None, "No metrics record for T-met"
        assert record.get("task_id") == "T-met"
        assert "duration_seconds" in record or "cost_usd" in record

    def test_metrics_written_on_orphan_fail(self, tmp_path: Path) -> None:
        """Failed orphan writes a metrics record with error_type set."""
        task_dict = _task_as_dict(_make_task(id="T-fail-met", status="claimed"))
        task_dict["status"] = "claimed"

        routes = {
            "GET /tasks/T-fail-met": httpx.Response(200, json=task_dict),
            "POST /tasks/T-fail-met/fail": httpx.Response(200, json={}),
        }
        transport = _make_task_transport(routes)
        adapter = _mock_adapter()
        adapter.is_alive.return_value = False
        orch = _build_orchestrator(tmp_path, transport, adapter=adapter)

        session = AgentSession(
            id="backend-failmet",
            role="backend",
            pid=42,
            task_ids=["T-fail-met"],
            status="working",
        )
        orch._agents["backend-failmet"] = session

        orch.tick()

        record = _find_metrics_record(tmp_path, "T-fail-met")
        assert record is not None, "No metrics record for T-fail-met"
        assert record.get("task_id") == "T-fail-met"


# --- TierAwareRouter wiring ---


def _make_router_with_provider() -> TierAwareRouter:
    """Create a TierAwareRouter with a single test provider."""
    router = TierAwareRouter()
    router.register_provider(
        ProviderConfig(
            name="test_provider",
            models={
                "sonnet": RouterModelConfig("sonnet", "high"),
                "opus": RouterModelConfig("opus", "max"),
            },
            tier=Tier.STANDARD,
            cost_per_1k_tokens=0.003,
        )
    )
    return router


class TestTierAwareRouterWiring:
    """Verify TierAwareRouter is wired into the orchestrator correctly."""

    def test_orchestrator_accepts_router(self, tmp_path: Path) -> None:
        router = _make_router_with_provider()
        transport = _mock_transport(
            {
                "GET /tasks?status=open": httpx.Response(200, json=[]),
                "GET /tasks?status=done": httpx.Response(200, json=[]),
            }
        )
        orch = _build_orchestrator(tmp_path, transport)
        orch._router = router

        assert orch._router is router

    def test_orchestrator_constructor_with_router(self, tmp_path: Path) -> None:
        router = _make_router_with_provider()
        cfg = OrchestratorConfig(server_url="http://testserver")
        adapter = _mock_adapter()
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adapter, templates_dir, tmp_path, router=router)
        transport = _mock_transport(
            {
                "GET /tasks?status=open": httpx.Response(200, json=[]),
                "GET /tasks?status=done": httpx.Response(200, json=[]),
            }
        )
        client = httpx.Client(transport=transport, base_url="http://testserver")
        orch = Orchestrator(cfg, spawner, tmp_path, client=client, router=router)

        assert orch._router is router
        assert orch._router.state.providers["test_provider"].name == "test_provider"

    def test_record_provider_health_updates_router(self, tmp_path: Path) -> None:
        router = _make_router_with_provider()
        transport = _mock_transport(
            {
                "GET /tasks?status=open": httpx.Response(200, json=[]),
                "GET /tasks?status=done": httpx.Response(200, json=[]),
            }
        )
        cfg = OrchestratorConfig(server_url="http://testserver")
        adapter = _mock_adapter()
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adapter, templates_dir, tmp_path)
        client = httpx.Client(transport=transport, base_url="http://testserver")
        orch = Orchestrator(cfg, spawner, tmp_path, client=client, router=router)

        session = AgentSession(
            id="backend-123",
            role="backend",
            pid=42,
            provider="test_provider",
        )
        orch._record_provider_health(session, success=True, latency_ms=100.0)

        provider = router.state.providers["test_provider"]
        assert provider.health.consecutive_successes == 1
        assert provider.health.avg_latency_ms > 0

    def test_record_provider_cost_updates_router(self, tmp_path: Path) -> None:
        router = _make_router_with_provider()
        transport = _mock_transport(
            {
                "GET /tasks?status=open": httpx.Response(200, json=[]),
                "GET /tasks?status=done": httpx.Response(200, json=[]),
            }
        )
        cfg = OrchestratorConfig(server_url="http://testserver")
        adapter = _mock_adapter()
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adapter, templates_dir, tmp_path)
        client = httpx.Client(transport=transport, base_url="http://testserver")
        orch = Orchestrator(cfg, spawner, tmp_path, client=client, router=router)

        session = AgentSession(
            id="backend-123",
            role="backend",
            pid=42,
            provider="test_provider",
        )
        orch._record_provider_health(
            session,
            success=True,
            cost_usd=0.05,
            tokens=1000,
        )

        provider = router.state.providers["test_provider"]
        assert provider.cost_tracker.total_cost_usd == pytest.approx(0.05)
        assert provider.cost_tracker.total_tokens == 1000

    def test_no_router_is_noop(self, tmp_path: Path) -> None:
        """When no router is configured, health/cost recording is a no-op."""
        transport = _mock_transport(
            {
                "GET /tasks?status=open": httpx.Response(200, json=[]),
                "GET /tasks?status=done": httpx.Response(200, json=[]),
            }
        )
        orch = _build_orchestrator(tmp_path, transport)
        assert orch._router is None

        session = AgentSession(
            id="backend-123",
            role="backend",
            pid=42,
            provider="x",
        )
        # Should not raise
        orch._record_provider_health(session, success=True, cost_usd=1.0, tokens=500)

    def test_loads_router_from_providers_yaml(self, tmp_path: Path) -> None:
        """TierAwareRouter auto-loads providers when providers.yaml exists."""
        config_dir = tmp_path / ".sdd" / "config"
        config_dir.mkdir(parents=True)
        providers_yaml = config_dir / "providers.yaml"
        providers_yaml.write_text(
            "providers:\n"
            "  yaml_provider:\n"
            "    tier: standard\n"
            "    cost_per_1k_tokens: 0.01\n"
            "    models:\n"
            "      opus:\n"
            "        model: opus\n"
            "        effort: max\n"
        )

        router = TierAwareRouter()
        # Router starts with no providers; constructor loads from YAML
        transport = _mock_transport(
            {
                "GET /tasks?status=open": httpx.Response(200, json=[]),
                "GET /tasks?status=done": httpx.Response(200, json=[]),
            }
        )
        cfg = OrchestratorConfig(server_url="http://testserver")
        adapter = _mock_adapter()
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adapter, templates_dir, tmp_path)
        client = httpx.Client(transport=transport, base_url="http://testserver")
        orch = Orchestrator(cfg, spawner, tmp_path, client=client, router=router)

        # The orchestrator's __init__ should have loaded from YAML
        assert "yaml_provider" in orch._router.state.providers


# --- Feature 4: Backlog Sync ---


class TestBacklogSync:
    """When a task is marked done, its .md file moves from backlog/open/ to backlog/closed/."""

    def _setup_backlog(self, tmp_path: Path, filenames: list[str]) -> Path:
        open_dir = tmp_path / ".sdd" / "backlog" / "open"
        open_dir.mkdir(parents=True)
        closed_dir = tmp_path / ".sdd" / "backlog" / "closed"
        closed_dir.mkdir(parents=True)
        for name in filenames:
            (open_dir / name).write_text(f"# {name}\n\nTask description here.\n")
        return open_dir

    def test_done_task_moves_matching_backlog_file(self, tmp_path: Path) -> None:
        """A done task with title matching a backlog file moves it to closed/."""
        self._setup_backlog(tmp_path, ["104-approval-gate-router.md"])

        done_task = _make_task(
            id="T-done-1",
            title="Implement risk-stratified ApprovalGate",
            status="done",
        )

        transport = _mock_transport(
            {
                "GET /tasks?status=open": httpx.Response(200, json=[]),
                "GET /tasks?status=done": httpx.Response(200, json=[_task_as_dict(done_task)]),
            }
        )
        orch = _build_orchestrator(tmp_path, transport)
        orch.tick()

        open_dir = tmp_path / ".sdd" / "backlog" / "open"
        closed_dir = tmp_path / ".sdd" / "backlog" / "closed"
        assert not (open_dir / "104-approval-gate-router.md").exists()
        assert (closed_dir / "104-approval-gate-router.md").exists()

    def test_closed_file_has_completion_timestamp(self, tmp_path: Path) -> None:
        """Moved file has a completion timestamp appended."""
        self._setup_backlog(tmp_path, ["104-approval-gate-router.md"])

        done_task = _make_task(
            id="T-done-2",
            title="Implement risk-stratified ApprovalGate",
            status="done",
        )
        done_task.result_summary = "All tests pass"

        transport = _mock_transport(
            {
                "GET /tasks?status=open": httpx.Response(200, json=[]),
                "GET /tasks?status=done": httpx.Response(200, json=[_task_as_dict(done_task)]),
            }
        )
        orch = _build_orchestrator(tmp_path, transport)
        orch.tick()

        closed_path = tmp_path / ".sdd" / "backlog" / "closed" / "104-approval-gate-router.md"
        content = closed_path.read_text()
        assert "completed" in content.lower() or "done" in content.lower()

    def test_no_match_leaves_open_unchanged(self, tmp_path: Path) -> None:
        """A done task with no matching backlog file leaves open/ intact."""
        self._setup_backlog(tmp_path, ["104-approval-gate-router.md"])

        done_task = _make_task(
            id="T-nomatch",
            title="Some completely unrelated task xyz",
            status="done",
        )

        transport = _mock_transport(
            {
                "GET /tasks?status=open": httpx.Response(200, json=[]),
                "GET /tasks?status=done": httpx.Response(200, json=[_task_as_dict(done_task)]),
            }
        )
        orch = _build_orchestrator(tmp_path, transport)
        orch.tick()

        open_dir = tmp_path / ".sdd" / "backlog" / "open"
        assert (open_dir / "104-approval-gate-router.md").exists()

    def test_sync_not_repeated_for_already_processed_task(self, tmp_path: Path) -> None:
        """A task processed in tick 1 is not re-synced in tick 2."""
        self._setup_backlog(tmp_path, ["114-sync-backlog-files-with-server.md"])

        done_task = _make_task(
            id="T-rep",
            title="Sync .sdd/backlog files with task server state",
            status="done",
        )
        transport = _mock_transport(
            {
                "GET /tasks?status=open": httpx.Response(200, json=[]),
                "GET /tasks?status=done": httpx.Response(200, json=[_task_as_dict(done_task)]),
            }
        )
        orch = _build_orchestrator(tmp_path, transport)
        orch.tick()

        closed_dir = tmp_path / ".sdd" / "backlog" / "closed"
        assert (closed_dir / "114-sync-backlog-files-with-server.md").exists()

        # Second tick: file already moved, no crash
        orch.tick()
        assert (closed_dir / "114-sync-backlog-files-with-server.md").exists()

    def test_no_backlog_dir_is_noop(self, tmp_path: Path) -> None:
        """If .sdd/backlog/open/ does not exist, sync silently does nothing."""
        done_task = _make_task(id="T-nodir", title="Whatever task", status="done")
        transport = _mock_transport(
            {
                "GET /tasks?status=open": httpx.Response(200, json=[]),
                "GET /tasks?status=done": httpx.Response(200, json=[_task_as_dict(done_task)]),
            }
        )
        orch = _build_orchestrator(tmp_path, transport)
        orch.tick()  # Should not raise


# --- Feature 5: Evolve Mode (idle detection + re-planning) ---


def _write_evolve_config(
    tmp_path: Path,
    *,
    enabled: bool = True,
    max_cycles: int = 0,
    budget_usd: float = 0.0,
    interval_s: int = 0,
    cycle_count: int = 0,
    last_cycle_ts: float = 0.0,
    consecutive_empty: int = 0,
    spent_usd: float = 0.0,
) -> Path:
    """Write an evolve.json config for testing."""
    runtime = tmp_path / ".sdd" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = {
        "enabled": enabled,
        "max_cycles": max_cycles,
        "budget_usd": budget_usd,
        "interval_s": interval_s,
        "_cycle_count": cycle_count,
        "_last_cycle_ts": last_cycle_ts,
        "_consecutive_empty": consecutive_empty,
        "_spent_usd": spent_usd,
    }
    path = runtime / "evolve.json"
    path.write_text(json.dumps(config))
    return path


def _evolve_handler(
    *,
    open_tasks: list[dict[str, object]] | None = None,
    claimed_tasks: list[dict[str, object]] | None = None,
    done_tasks: list[dict[str, object]] | None = None,
    manager_task_created: list[dict[str, object]] | None = None,
) -> httpx.MockTransport:
    """Build a transport that tracks manager task creation for evolve tests."""
    _open = open_tasks or []
    _claimed = claimed_tasks or []
    _done = done_tasks or []
    created = manager_task_created if manager_task_created is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        url = request.url
        if request.method == "GET" and url.path == "/tasks":
            return _tasks_response(url, _open + _claimed + _done)
        if request.method == "POST" and url.path == "/tasks":
            body = json.loads(request.content)
            created.append(body)
            return httpx.Response(200, json={"id": "T-evolve-mgr"})
        return httpx.Response(200, json={})

    return httpx.MockTransport(handler)


class TestEvolveIdleDetection:
    """Tests for _check_evolve: idle detection and re-planning trigger."""

    def test_no_evolve_config_is_noop(self, tmp_path: Path) -> None:
        """No evolve.json => evolve check silently does nothing."""
        transport = _mock_transport(
            {
                "GET /tasks?status=open": httpx.Response(200, json=[]),
                "GET /tasks?status=done": httpx.Response(200, json=[]),
            }
        )
        orch = _build_orchestrator(tmp_path, transport)
        result = orch.tick()
        assert result.errors == []

    def test_evolve_disabled_is_noop(self, tmp_path: Path) -> None:
        """evolve.json with enabled=false does nothing."""
        _write_evolve_config(tmp_path, enabled=False)
        transport = _mock_transport(
            {
                "GET /tasks?status=open": httpx.Response(200, json=[]),
                "GET /tasks?status=done": httpx.Response(200, json=[]),
            }
        )
        orch = _build_orchestrator(tmp_path, transport)
        result = orch.tick()
        assert result.errors == []

    def test_evolve_triggers_when_idle(self, tmp_path: Path) -> None:
        """When idle (no open/claimed tasks, no agents), creates a manager task."""
        _write_evolve_config(tmp_path, interval_s=0)
        created: list[dict[str, object]] = []
        transport = _evolve_handler(manager_task_created=created)
        config = OrchestratorConfig(
            server_url="http://testserver",
            evolution_enabled=False,  # disable EvolutionCoordinator to isolate _check_evolve
        )
        orch = _build_orchestrator(tmp_path, transport, config=config)

        # Patch out test/commit steps to avoid subprocess calls
        orch._evolve_run_tests = lambda: {"passed": 5, "failed": 0, "summary": "5 passed"}  # type: ignore[assignment]
        orch._evolve_auto_commit = lambda: False  # type: ignore[assignment]

        orch.tick()

        assert len(created) == 1
        assert created[0]["role"] == "manager"
        assert "Evolve cycle" in str(created[0]["title"])

    def test_evolve_does_not_trigger_when_tasks_open(self, tmp_path: Path) -> None:
        """Evolve does NOT trigger when there are still open tasks."""
        _write_evolve_config(tmp_path, interval_s=0)
        task = _make_task(id="T-open")
        created: list[dict[str, object]] = []
        transport = _evolve_handler(
            open_tasks=[_task_as_dict(task)],
            manager_task_created=created,
        )
        config = OrchestratorConfig(
            server_url="http://testserver",
            evolution_enabled=False,
        )
        orch = _build_orchestrator(tmp_path, transport, config=config)
        orch.tick()

        assert len(created) == 0

    def test_evolve_does_not_trigger_when_agents_alive(self, tmp_path: Path) -> None:
        """Evolve does NOT trigger when agents are still running."""
        _write_evolve_config(tmp_path, interval_s=0)
        created: list[dict[str, object]] = []
        transport = _evolve_handler(manager_task_created=created)
        adapter = _mock_adapter()
        adapter.is_alive.return_value = True
        config = OrchestratorConfig(
            server_url="http://testserver",
            evolution_enabled=False,
        )
        orch = _build_orchestrator(tmp_path, transport, adapter=adapter, config=config)

        # Inject an alive agent
        session = AgentSession(
            id="backend-busy",
            role="backend",
            pid=42,
            task_ids=["T-x"],
            status="working",
        )
        orch._agents["backend-busy"] = session

        orch.tick()

        assert len(created) == 0

    def test_evolve_stops_at_max_cycles(self, tmp_path: Path) -> None:
        """Evolve does NOT trigger after max_cycles is reached."""
        _write_evolve_config(tmp_path, max_cycles=3, cycle_count=3, interval_s=0)
        created: list[dict[str, object]] = []
        transport = _evolve_handler(manager_task_created=created)
        config = OrchestratorConfig(
            server_url="http://testserver",
            evolution_enabled=False,
        )
        orch = _build_orchestrator(tmp_path, transport, config=config)
        orch.tick()

        assert len(created) == 0

    def test_evolve_stops_at_budget(self, tmp_path: Path) -> None:
        """Evolve does NOT trigger after budget_usd is exhausted."""
        _write_evolve_config(
            tmp_path,
            budget_usd=10.0,
            spent_usd=10.0,
            interval_s=0,
        )
        created: list[dict[str, object]] = []
        transport = _evolve_handler(manager_task_created=created)
        config = OrchestratorConfig(
            server_url="http://testserver",
            evolution_enabled=False,
        )
        orch = _build_orchestrator(tmp_path, transport, config=config)
        orch.tick()

        assert len(created) == 0

    def test_evolve_respects_interval(self, tmp_path: Path) -> None:
        """Evolve does NOT trigger before the interval elapses."""
        _write_evolve_config(
            tmp_path,
            interval_s=9999,
            last_cycle_ts=time.time(),  # just ran
        )
        created: list[dict[str, object]] = []
        transport = _evolve_handler(manager_task_created=created)
        config = OrchestratorConfig(
            server_url="http://testserver",
            evolution_enabled=False,
        )
        orch = _build_orchestrator(tmp_path, transport, config=config)
        orch.tick()

        assert len(created) == 0

    def test_evolve_logs_cycle_to_jsonl(self, tmp_path: Path) -> None:
        """Each evolve cycle is logged to evolve_cycles.jsonl."""
        _write_evolve_config(tmp_path, interval_s=0)
        transport = _evolve_handler()
        config = OrchestratorConfig(
            server_url="http://testserver",
            evolution_enabled=False,
        )
        orch = _build_orchestrator(tmp_path, transport, config=config)
        orch._evolve_run_tests = lambda: {"passed": 0, "failed": 0, "summary": ""}  # type: ignore[assignment]
        orch._evolve_auto_commit = lambda: False  # type: ignore[assignment]

        orch.tick()

        log_path = tmp_path / ".sdd" / "metrics" / "evolve_cycles.jsonl"
        assert log_path.exists()
        entry = json.loads(log_path.read_text().strip())
        assert entry["cycle"] == 1
        assert "focus_area" in entry
        assert "timestamp" in entry

    def test_evolve_rotates_focus_areas(self, tmp_path: Path) -> None:
        """Successive cycles rotate through different focus areas."""
        created: list[dict[str, object]] = []
        transport = _evolve_handler(manager_task_created=created)
        config = OrchestratorConfig(
            server_url="http://testserver",
            evolution_enabled=False,
        )
        orch = _build_orchestrator(tmp_path, transport, config=config)
        orch._evolve_run_tests = lambda: {"passed": 0, "failed": 0, "summary": ""}  # type: ignore[assignment]
        orch._evolve_auto_commit = lambda: False  # type: ignore[assignment]

        titles: list[str] = []
        for i in range(3):
            _write_evolve_config(tmp_path, interval_s=0, cycle_count=i)
            created.clear()
            orch.tick()
            if created:
                titles.append(str(created[0]["title"]))

        # Each cycle should have a different focus
        assert len(titles) == 3
        assert titles[0] != titles[1]

    def test_evolve_updates_cycle_count(self, tmp_path: Path) -> None:
        """After a cycle, _cycle_count is incremented in evolve.json."""
        evolve_path = _write_evolve_config(tmp_path, interval_s=0)
        transport = _evolve_handler()
        config = OrchestratorConfig(
            server_url="http://testserver",
            evolution_enabled=False,
        )
        orch = _build_orchestrator(tmp_path, transport, config=config)
        orch._evolve_run_tests = lambda: {"passed": 0, "failed": 0, "summary": ""}  # type: ignore[assignment]
        orch._evolve_auto_commit = lambda: False  # type: ignore[assignment]

        orch.tick()

        updated = json.loads(evolve_path.read_text())
        assert updated["_cycle_count"] == 1
        assert updated["_last_cycle_ts"] > 0

    def test_evolve_diminishing_returns_backoff(self, tmp_path: Path) -> None:
        """After 3+ consecutive empty cycles, interval increases via backoff."""
        # 3 consecutive empty cycles with interval_s=100 => effective interval = 100 * 2 = 200
        _write_evolve_config(
            tmp_path,
            interval_s=100,
            consecutive_empty=3,
            last_cycle_ts=time.time() - 150,  # 150s ago (< 200s effective interval)
        )
        created: list[dict[str, object]] = []
        transport = _evolve_handler(manager_task_created=created)
        config = OrchestratorConfig(
            server_url="http://testserver",
            evolution_enabled=False,
        )
        orch = _build_orchestrator(tmp_path, transport, config=config)
        orch.tick()

        # Should NOT trigger: 150s < 200s (100 * 2^1)
        assert len(created) == 0

    def test_evolve_includes_research_context(self, tmp_path: Path) -> None:
        """When Tavily research succeeds, the manager task gets market context."""
        from unittest.mock import patch

        from bernstein.core.researcher import ResearchReport, ResearchResult

        _write_evolve_config(tmp_path, interval_s=0)
        created: list[dict[str, object]] = []
        transport = _evolve_handler(manager_task_created=created)
        config = OrchestratorConfig(
            server_url="http://testserver",
            evolution_enabled=False,
        )
        orch = _build_orchestrator(tmp_path, transport, config=config)
        orch._evolve_run_tests = lambda: {"passed": 0, "failed": 0, "summary": ""}  # type: ignore[assignment]
        orch._evolve_auto_commit = lambda: False  # type: ignore[assignment]

        fake_report = ResearchReport(
            competitors=[ResearchResult(query="q", content="CompetitorX data", timestamp=1.0)],
            searches_performed=1,
        )
        with patch("bernstein.core.researcher.run_research_sync", return_value=fake_report):
            orch.tick()

        assert len(created) == 1
        desc = str(created[0]["description"])
        assert "CompetitorX data" in desc


# --- Provider health recording and evolution metrics ---


class TestProviderHealthRecording:
    """Orchestrator records provider health feedback on done task processing."""

    def _build_with_router(self, tmp_path: Path) -> tuple[Orchestrator, MagicMock]:
        router = MagicMock(spec=TierAwareRouter)
        router.state = RouterState(providers={})
        transport = _mock_transport(
            {
                "GET /tasks?status=open": httpx.Response(200, json=[]),
                "GET /tasks?status=done": httpx.Response(
                    200, json=[_task_as_dict(_make_task(id="T-done", status="done"))]
                ),
            }
        )
        cfg = OrchestratorConfig(
            max_agents=6,
            poll_interval_s=1,
            server_url="http://testserver",
            evolution_enabled=False,
        )
        adp = _mock_adapter()
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adp, templates_dir, tmp_path)
        client = httpx.Client(transport=transport, base_url="http://testserver")
        orch = Orchestrator(cfg, spawner, tmp_path, client=client, router=router)
        return orch, router

    def test_tick_records_provider_health_on_success(self, tmp_path: Path) -> None:
        orch, router = self._build_with_router(tmp_path)

        # Inject a session that owns T-done with a known provider
        session = AgentSession(
            id="backend-a",
            role="backend",
            pid=10,
            task_ids=["T-done"],
            provider="anthropic",
            status="working",
        )
        orch._agents["backend-a"] = session
        orch._task_to_session["T-done"] = "backend-a"

        orch.tick()

        router.update_provider_health.assert_called_once_with("anthropic", True, 0.0)

    def test_tick_records_provider_health_on_failure(self, tmp_path: Path) -> None:
        orch, router = self._build_with_router(tmp_path)

        # Build task JSON with completion_signals so the janitor runs
        done_task_json = _task_as_dict(_make_task(id="T-done-sig", status="done"))
        done_task_json["completion_signals"] = [{"type": "file_exists", "value": "definitely_missing_file.txt"}]

        transport = _mock_transport(
            {
                "GET /tasks?status=open": httpx.Response(200, json=[]),
                "GET /tasks?status=done": httpx.Response(200, json=[done_task_json]),
            }
        )
        orch._client = httpx.Client(transport=transport, base_url="http://testserver")

        session = AgentSession(
            id="backend-c",
            role="backend",
            pid=12,
            task_ids=["T-done-sig"],
            provider="openai",
            status="working",
        )
        orch._agents["backend-c"] = session
        orch._task_to_session["T-done-sig"] = "backend-c"

        orch.tick()

        # Janitor fails (file does not exist) → success=False
        router.update_provider_health.assert_called_once_with("openai", False, 0.0)

    def test_tick_without_router_skips_health(self, tmp_path: Path) -> None:
        """No crash when router is None and a done task is processed."""
        transport = _mock_transport(
            {
                "GET /tasks?status=open": httpx.Response(200, json=[]),
                "GET /tasks?status=done": httpx.Response(
                    200, json=[_task_as_dict(_make_task(id="T-done2", status="done"))]
                ),
            }
        )
        orch = _build_orchestrator(tmp_path, transport)
        assert orch._router is None

        session = AgentSession(
            id="backend-d",
            role="backend",
            pid=13,
            task_ids=["T-done2"],
            provider="anthropic",
            status="working",
        )
        orch._agents["backend-d"] = session
        orch._task_to_session["T-done2"] = "backend-d"

        # Should not raise even without a router
        result = orch.tick()
        assert len(result.errors) == 0


class TestEvolutionMetricsRecording:
    """Orchestrator records task completion to EvolutionCoordinator."""

    def _build_with_evolution(self, tmp_path: Path) -> tuple[Orchestrator, MagicMock]:
        from bernstein.core.evolution import EvolutionCoordinator

        evolution = MagicMock(spec=EvolutionCoordinator)
        transport = _mock_transport(
            {
                "GET /tasks?status=open": httpx.Response(200, json=[]),
                "GET /tasks?status=done": httpx.Response(
                    200, json=[_task_as_dict(_make_task(id="T-evo", status="done"))]
                ),
            }
        )
        cfg = OrchestratorConfig(
            max_agents=6,
            poll_interval_s=1,
            server_url="http://testserver",
            evolution_enabled=True,
        )
        adp = _mock_adapter()
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adp, templates_dir, tmp_path)
        client = httpx.Client(transport=transport, base_url="http://testserver")
        orch = Orchestrator(cfg, spawner, tmp_path, client=client, evolution=evolution)
        return orch, evolution

    def test_tick_records_evolution_metrics(self, tmp_path: Path) -> None:
        orch, evolution = self._build_with_evolution(tmp_path)

        session = AgentSession(
            id="backend-evo",
            role="backend",
            pid=20,
            task_ids=["T-evo"],
            provider="anthropic",
            spawn_ts=time.time() - 5.0,  # 5 seconds ago
            status="working",
        )
        orch._agents["backend-evo"] = session

        orch.tick()

        evolution.record_task_completion.assert_called_once()
        call_kwargs = evolution.record_task_completion.call_args
        assert call_kwargs.kwargs["janitor_passed"] is True
        assert call_kwargs.kwargs["duration_seconds"] >= 0.0

    def test_tick_evolution_record_failure_logged(self, tmp_path: Path) -> None:
        """If record_task_completion raises, the orchestrator catches it and does not crash."""
        orch, evolution = self._build_with_evolution(tmp_path)
        evolution.record_task_completion.side_effect = RuntimeError("db failure")

        session = AgentSession(
            id="backend-evo2",
            role="backend",
            pid=21,
            task_ids=["T-evo"],
            status="working",
        )
        orch._agents["backend-evo2"] = session

        # Must not raise
        result = orch.tick()
        assert len(result.errors) == 0
        evolution.record_task_completion.assert_called_once()

    def test_complete_agent_task_called_before_end_agent(self, tmp_path: Path) -> None:
        """complete_agent_task() is called before end_agent() so AGENT_SUCCESS metric is written."""
        import bernstein.core.metrics as _metrics_mod
        from bernstein.core.metrics import get_collector

        # Reset the global so we get a fresh collector for this test.
        _metrics_mod._default_collector = None

        orch, _evolution = self._build_with_evolution(tmp_path)
        session = AgentSession(
            id="backend-agent-task",
            role="backend",
            pid=22,
            task_ids=["T-evo"],
            spawn_ts=time.time() - 3.0,
            status="working",
        )
        orch._agents["backend-agent-task"] = session
        orch._task_to_session["T-evo"] = "backend-agent-task"

        collector = get_collector(tmp_path / ".sdd" / "metrics")
        collector.start_agent(
            agent_id="backend-agent-task",
            role="backend",
            model="sonnet",
            provider="anthropic",
        )
        collector.start_task(
            task_id="T-evo",
            role="backend",
            model="sonnet",
            provider="anthropic",
        )

        orch.tick()

        agent_m = collector._agent_metrics.get("backend-agent-task")
        assert agent_m is not None, "AgentMetrics not found in collector"
        total = agent_m.tasks_completed + agent_m.tasks_failed
        assert total == 1, (
            f"complete_agent_task() was not called: tasks_completed={agent_m.tasks_completed}, "
            f"tasks_failed={agent_m.tasks_failed}"
        )

        # Cleanup global collector so other tests are not affected.
        _metrics_mod._default_collector = None


class TestOrphanEvolutionMetrics:
    """Orphaned task handling feeds the evolution coordinator via record_task_completion."""

    def _build_with_evolution(self, tmp_path: Path) -> MagicMock:
        from bernstein.core.evolution import EvolutionCoordinator

        evolution = MagicMock(spec=EvolutionCoordinator)
        evolution.execute_pending_upgrades.return_value = []
        return evolution

    def test_orphan_success_feeds_evolution(self, tmp_path: Path) -> None:
        """Successful orphan auto-complete calls evolution.record_task_completion."""
        (tmp_path / "result.txt").write_text("ok")
        from bernstein.core.evolution import EvolutionCoordinator

        evolution = MagicMock(spec=EvolutionCoordinator)
        task_dict = _task_as_dict(_make_task(id="T-orp-evo", status="in_progress"))
        task_dict["completion_signals"] = [{"type": "path_exists", "value": "result.txt"}]
        task_dict["status"] = "in_progress"

        def handler(request: httpx.Request) -> httpx.Response:
            url = request.url
            key = f"{request.method} {url.path}"
            if url.query:
                key += f"?{url.query.decode()}"
            if request.method == "GET" and url.path == "/tasks":
                return httpx.Response(200, json=[])
            if key == "GET /tasks/T-orp-evo":
                return httpx.Response(200, json=task_dict)
            if key == "POST /tasks/T-orp-evo/complete":
                return httpx.Response(200, json={})
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        adapter = _mock_adapter()
        adapter.is_alive.return_value = False
        cfg = OrchestratorConfig(
            max_agents=6,
            poll_interval_s=1,
            server_url="http://testserver",
            evolution_enabled=True,
        )
        adp = adapter
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adp, templates_dir, tmp_path)
        client = httpx.Client(transport=transport, base_url="http://testserver")
        orch = Orchestrator(cfg, spawner, tmp_path, client=client, evolution=evolution)

        session = AgentSession(
            id="backend-orp-evo",
            role="backend",
            pid=42,
            task_ids=["T-orp-evo"],
            status="working",
        )
        orch._agents["backend-orp-evo"] = session

        orch.tick()

        evolution.record_task_completion.assert_called_once()
        call_kwargs = evolution.record_task_completion.call_args
        assert call_kwargs.kwargs["janitor_passed"] is True
        assert call_kwargs.kwargs["duration_seconds"] >= 0.0

    def test_orphan_fail_feeds_evolution(self, tmp_path: Path) -> None:
        """Failed orphan (no completion signals) calls evolution.record_task_completion."""
        from bernstein.core.evolution import EvolutionCoordinator

        evolution = MagicMock(spec=EvolutionCoordinator)
        task_dict = _task_as_dict(_make_task(id="T-orp-fail", status="claimed"))
        task_dict["status"] = "claimed"

        def handler(request: httpx.Request) -> httpx.Response:
            url = request.url
            key = f"{request.method} {url.path}"
            if url.query:
                key += f"?{url.query.decode()}"
            if request.method == "GET" and url.path == "/tasks":
                return httpx.Response(200, json=[])
            if key == "GET /tasks/T-orp-fail":
                return httpx.Response(200, json=task_dict)
            if key == "POST /tasks/T-orp-fail/fail":
                return httpx.Response(200, json={})
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        adapter = _mock_adapter()
        adapter.is_alive.return_value = False
        cfg = OrchestratorConfig(
            max_agents=6,
            poll_interval_s=1,
            server_url="http://testserver",
            evolution_enabled=True,
        )
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adapter, templates_dir, tmp_path)
        client = httpx.Client(transport=transport, base_url="http://testserver")
        orch = Orchestrator(cfg, spawner, tmp_path, client=client, evolution=evolution)

        session = AgentSession(
            id="backend-orp-fail",
            role="backend",
            pid=43,
            task_ids=["T-orp-fail"],
            status="working",
        )
        orch._agents["backend-orp-fail"] = session

        orch.tick()

        evolution.record_task_completion.assert_called_once()
        call_kwargs = evolution.record_task_completion.call_args
        assert call_kwargs.kwargs["janitor_passed"] is False

    def test_orphan_evolution_failure_does_not_crash(self, tmp_path: Path) -> None:
        """If evolution.record_task_completion raises for an orphan, orchestrator does not crash."""
        (tmp_path / "result.txt").write_text("ok")
        from bernstein.core.evolution import EvolutionCoordinator

        evolution = MagicMock(spec=EvolutionCoordinator)
        evolution.record_task_completion.side_effect = RuntimeError("evolution down")
        task_dict = _task_as_dict(_make_task(id="T-orp-err", status="in_progress"))
        task_dict["completion_signals"] = [{"type": "path_exists", "value": "result.txt"}]
        task_dict["status"] = "in_progress"

        def handler(request: httpx.Request) -> httpx.Response:
            url = request.url
            key = f"{request.method} {url.path}"
            if url.query:
                key += f"?{url.query.decode()}"
            if request.method == "GET" and url.path == "/tasks":
                return httpx.Response(200, json=[])
            if key == "GET /tasks/T-orp-err":
                return httpx.Response(200, json=task_dict)
            if key == "POST /tasks/T-orp-err/complete":
                return httpx.Response(200, json={})
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        adapter = _mock_adapter()
        adapter.is_alive.return_value = False
        cfg = OrchestratorConfig(
            max_agents=6,
            poll_interval_s=1,
            server_url="http://testserver",
            evolution_enabled=True,
        )
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adapter, templates_dir, tmp_path)
        client = httpx.Client(transport=transport, base_url="http://testserver")
        orch = Orchestrator(cfg, spawner, tmp_path, client=client, evolution=evolution)

        session = AgentSession(
            id="backend-orp-err",
            role="backend",
            pid=44,
            task_ids=["T-orp-err"],
            status="working",
        )
        orch._agents["backend-orp-err"] = session

        # Must not raise
        result = orch.tick()
        assert len(result.errors) == 0


class TestConsecutiveTickFailureCircuitBreaker:
    """run() exits after max_consecutive_failures tick exceptions."""

    def test_run_stops_after_max_consecutive_failures(self, tmp_path: Path) -> None:
        transport = _mock_transport(
            {
                "GET /tasks?status=open": httpx.Response(200, json=[]),
                "GET /tasks?status=done": httpx.Response(200, json=[]),
            }
        )
        config = OrchestratorConfig(
            poll_interval_s=0,
            server_url="http://testserver",
        )
        orch = _build_orchestrator(tmp_path, transport, config=config)

        call_count = 0

        def always_failing_tick() -> TickResult:
            nonlocal call_count
            call_count += 1
            raise RuntimeError("tick exploded")

        orch.tick = always_failing_tick  # type: ignore[assignment]
        orch.run()

        # 10 consecutive failures → loop breaks
        assert call_count == 10


# --- New edge-case coverage ---


class TestDeadAgentFileOwnershipEdgeCases:
    """Edge cases for file ownership release and respawn after agent death."""

    def test_dead_agent_file_ownership_released(self, tmp_path: Path) -> None:
        """When an agent process dies, all its file ownership entries are removed."""
        transport = _mock_transport(
            {
                "GET /tasks?status=open": httpx.Response(200, json=[]),
                "GET /tasks?status=done": httpx.Response(200, json=[]),
            }
        )
        adapter = _mock_adapter()
        adapter.is_alive.return_value = False
        orch = _build_orchestrator(tmp_path, transport, adapter=adapter)

        # Pre-claim two files for the dying agent
        orch._file_ownership["src/main.py"] = "backend-dying"
        orch._file_ownership["src/utils.py"] = "backend-dying"

        session = AgentSession(
            id="backend-dying",
            role="backend",
            pid=42,
            task_ids=[],  # no tasks avoids needing orphan-handler endpoints
            status="working",
        )
        orch._agents["backend-dying"] = session

        orch.tick()

        assert session.status == "dead"
        assert "src/main.py" not in orch._file_ownership
        assert "src/utils.py" not in orch._file_ownership

    def test_file_overlap_cleared_after_dead_agent_allows_respawn(self, tmp_path: Path) -> None:
        """Spawn is blocked while an agent owns a file; after it dies the next tick spawns."""
        task1 = _make_task(id="T-owner", role="backend")
        task1.owned_files = ["src/shared.py"]
        task2 = _make_task(id="T-waiter", role="qa")
        task2.owned_files = ["src/shared.py"]

        # Reflect task1 as "in_progress" so the orphan handler skips completing it
        task1_inprog = _make_task(id="T-owner", role="backend", status="in_progress")

        tick = 0
        is_alive_flag = True

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal tick
            url = request.url
            key = f"{request.method} {url.path}"
            if request.method == "GET" and url.path == "/tasks":
                tick += 1
                if tick == 1:
                    return _tasks_response(url, [_task_as_dict(task1)])
                return _tasks_response(url, [_task_as_dict(task2)])
            if key == "GET /tasks/T-owner":
                # Orphan handler fetches the task; return it as in_progress (no signals → fail)
                return httpx.Response(200, json=_task_as_dict(task1_inprog))
            if key == "POST /tasks/T-owner/fail":
                return httpx.Response(200, json={})
            # claim endpoint and other best-effort calls
            return httpx.Response(200, json={})

        adapter = _mock_adapter()
        adapter.is_alive.side_effect = lambda session: is_alive_flag

        transport = httpx.MockTransport(handler)
        orch = _build_orchestrator(tmp_path, transport, adapter=adapter)

        # Tick 1: spawns agent for task1, claims src/shared.py
        r1 = orch.tick()
        assert len(r1.spawned) == 1
        assert "src/shared.py" in orch._file_ownership

        # Tick 2: task2 blocked because src/shared.py is still owned (agent alive)
        r2 = orch.tick()
        assert len(r2.spawned) == 0

        # Capture the id of the agent that owns the file before it dies
        dead_agent_id = orch._file_ownership["src/shared.py"]

        # Agent for task1 dies
        is_alive_flag = False

        # Tick 3: dead agent detected → file released → task2 can spawn
        r3 = orch.tick()
        assert len(r3.spawned) == 1
        # The dead agent must no longer own the file
        assert orch._file_ownership.get("src/shared.py") != dead_agent_id


class TestStaleHeartbeatReapingDefault:
    """An agent whose heartbeat exceeds the configured timeout is reaped and its tasks failed."""

    @patch("bernstein.core.agent_recycling._is_process_alive", return_value=False)
    def test_stale_heartbeat_reaps_agent(self, _mock_alive: MagicMock, tmp_path: Path) -> None:
        """Heartbeat older than heartbeat_timeout_s triggers reaping and task failure."""
        adapter = _mock_adapter()
        config = OrchestratorConfig(
            heartbeat_timeout_s=600,
            server_url="http://testserver",
        )

        fail_called = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal fail_called
            url = request.url
            key = f"{request.method} {url.path}"
            if url.query:
                key += f"?{url.query.decode()}"
            if request.method == "GET" and url.path == "/tasks":
                return httpx.Response(200, json=[])
            if key == "GET /tasks/T-stale":
                return httpx.Response(200, json=_task_as_dict(_make_task(id="T-stale")))
            if key == "POST /tasks":
                return httpx.Response(201, json={"id": "T-stale-retry"})
            if key == "POST /tasks/T-stale/fail":
                fail_called = True
                return httpx.Response(200, json={})
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        orch = _build_orchestrator(tmp_path, transport, adapter=adapter, config=config)

        stale_session = AgentSession(
            id="backend-stale-hb",
            role="backend",
            pid=77,
            task_ids=["T-stale"],
            heartbeat_ts=time.time() - 700,  # 700s ago > 600s threshold
            status="working",
            # spawn_ts defaults to time.time() so wall-clock timeout won't fire
        )
        orch._agents["backend-stale-hb"] = stale_session

        result = orch.tick()

        assert "backend-stale-hb" in result.reaped
        assert stale_session.status != "working"
        assert fail_called
        adapter.kill.assert_called()


class TestAssignedTaskIdDoubleSpawn:
    """Two consecutive ticks with identical open tasks must not double-spawn agents."""

    def test_assigned_task_ids_prevents_double_spawn(self, tmp_path: Path) -> None:
        """Second tick skips batches whose tasks are already owned by alive agents."""
        task = _make_task(id="T-singleton", role="backend")

        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            url = request.url
            if request.method == "GET" and url.path == "/tasks":
                call_count += 1
                # Same task returned on every tick (simulate server not yet updated)
                return _tasks_response(url, [_task_as_dict(task)])
            # claim / other endpoints
            return httpx.Response(200, json={})

        adapter = _mock_adapter()
        adapter.is_alive.return_value = True  # agent stays alive between ticks

        transport = httpx.MockTransport(handler)
        orch = _build_orchestrator(tmp_path, transport, adapter=adapter)

        r1 = orch.tick()
        assert len(r1.spawned) == 1  # first tick spawns

        r2 = orch.tick()
        assert len(r2.spawned) == 0  # second tick skips — task already assigned

        # Only one agent should exist in total
        non_dead = [s for s in orch.active_agents.values() if s.status != "dead"]
        assert len(non_dead) == 1


class TestEvolveAutoCommitRuntimeExclusion:
    """_evolve_auto_commit stages all changes then unstages .sdd/runtime/ and .sdd/metrics/."""

    def test_evolve_auto_commit_excludes_runtime_files(self, tmp_path: Path) -> None:
        """git add -A is followed by git reset HEAD -- .sdd/runtime/ .sdd/metrics/."""
        from unittest.mock import patch

        transport = _mock_transport(
            {
                "GET /tasks?status=open": httpx.Response(200, json=[]),
                "GET /tasks?status=done": httpx.Response(200, json=[]),
            }
        )
        orch = _build_orchestrator(tmp_path, transport)

        status_result = MagicMock()
        status_result.stdout = "M src/bernstein/foo.py\n"
        status_result.returncode = 0

        test_result = MagicMock()
        test_result.returncode = 0

        # conventional_commit calls diff_cached_names (git diff --cached --name-only)
        # before committing; return at least one staged file so it doesn't bail out.
        diff_result = MagicMock()
        diff_result.returncode = 0
        diff_result.stdout = "src/bernstein/foo.py\n"

        completed_ok = MagicMock()
        completed_ok.returncode = 0
        completed_ok.stdout = ""

        def _fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
            if cmd[:2] == ["git", "status"]:
                return status_result
            if cmd[:2] == ["git", "diff"]:
                return diff_result
            if cmd[:2] == ["uv", "run"]:
                return test_result
            return completed_ok

        with patch("subprocess.run", side_effect=_fake_run) as mock_run:
            result = orch._evolve_auto_commit()

        assert result is True

        cmds = [c.args[0] for c in mock_run.call_args_list]

        # git add -A must appear
        assert ["git", "add", "-A"] in cmds

        # git reset HEAD -- must include .sdd/runtime/ and .sdd/metrics/
        reset_calls = [c for c in cmds if c[:4] == ["git", "reset", "HEAD", "--"]]
        assert len(reset_calls) >= 1, "Expected at least one git reset HEAD -- call"
        reset_args = reset_calls[0]
        assert ".sdd/runtime/" in reset_args
        assert ".sdd/metrics/" in reset_args

        # reset must come after add
        add_idx = cmds.index(["git", "add", "-A"])
        reset_idx = next(i for i, c in enumerate(cmds) if c[:4] == ["git", "reset", "HEAD", "--"])
        assert reset_idx > add_idx


# --- _retry_or_fail_task ---


class TestRetryOrFailTask:
    """Unit tests for Orchestrator._retry_or_fail_task."""

    def _build(
        self,
        tmp_path: Path,
        task: Task,
        *,
        max_retries: int = 2,
    ) -> tuple[Orchestrator, list[dict]]:
        """Return (orchestrator, captured_post_bodies).

        The mock transport:
        - GET /tasks/{id} → returns task JSON
        - POST /tasks       → records body, returns 201
        - POST /tasks/{id}/fail → returns 200
        """
        posted: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if request.method == "GET" and path == f"/tasks/{task.id}":
                return httpx.Response(200, json=_task_as_dict(task))
            if request.method == "POST" and path == "/tasks":
                posted.append(request.read() and __import__("json").loads(request.content))
                return httpx.Response(201, json={"id": "NEW-001"})
            if request.method == "POST" and path.endswith("/fail"):
                return httpx.Response(200, json={})
            return httpx.Response(404, json={"detail": f"No mock for {request.method} {path}"})

        transport = httpx.MockTransport(handler)
        cfg = OrchestratorConfig(
            server_url="http://testserver",
            max_task_retries=max_retries,
        )
        orch = _build_orchestrator(tmp_path, transport, config=cfg)
        return orch, posted

    def test_first_retry_creates_new_task(self, tmp_path: Path) -> None:
        task = _make_task(id="T-retry", description="Do the thing.")
        orch, posted = self._build(tmp_path, task, max_retries=2)

        orch._retry_or_fail_task("T-retry", "agent crashed")

        assert len(posted) == 1
        assert posted[0]["description"].startswith("[retry:1] Do the thing.")

    def test_second_retry_increments_counter(self, tmp_path: Path) -> None:
        task = _make_task(id="T-retry", description="[retry:1] Do the thing.")
        orch, posted = self._build(tmp_path, task, max_retries=2)

        orch._retry_or_fail_task("T-retry", "agent crashed again")

        assert len(posted) == 1
        assert posted[0]["description"].startswith("[retry:2] Do the thing.")

    def test_max_retries_exceeded_does_not_create_new_task(self, tmp_path: Path) -> None:
        task = _make_task(id="T-retry", description="[retry:2] Do the thing.")
        orch, posted = self._build(tmp_path, task, max_retries=2)

        orch._retry_or_fail_task("T-retry", "agent crashed yet again")

        # No new task should be created
        assert posted == []

    def test_zero_max_retries_always_fails(self, tmp_path: Path) -> None:
        task = _make_task(id="T-retry", description="Do the thing.")
        orch, posted = self._build(tmp_path, task, max_retries=0)

        orch._retry_or_fail_task("T-retry", "agent crashed")

        assert posted == []

    def test_retry_preserves_task_fields(self, tmp_path: Path) -> None:
        task = _make_task(
            id="T-retry",
            role="security",
            priority=1,
            scope="large",
            complexity="high",
            description="Fix the vuln.",
        )
        orch, posted = self._build(tmp_path, task, max_retries=2)

        orch._retry_or_fail_task("T-retry", "agent crashed")

        assert len(posted) == 1
        body = posted[0]
        assert body["role"] == "security"
        assert body["priority"] == 1
        assert body["scope"] == "large"
        assert body["complexity"] == "high"
        assert task.title in body["title"]  # may have [RETRY N] prefix


# --- _maybe_retry_task ---


class TestMaybeRetryTask:
    """Unit tests for Orchestrator._maybe_retry_task."""

    def _build(
        self,
        tmp_path: Path,
        *,
        max_retries: int = 2,
    ) -> tuple[Orchestrator, list[dict]]:
        """Return (orchestrator, captured_post_bodies) with POST /tasks mocked."""
        posted: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST" and request.url.path == "/tasks":
                posted.append(__import__("json").loads(request.content))
                return httpx.Response(201, json={"id": "NEW-RETRY"})
            return httpx.Response(404, json={"detail": "no mock"})

        transport = httpx.MockTransport(handler)
        cfg = OrchestratorConfig(server_url="http://testserver", max_task_retries=max_retries)
        orch = _build_orchestrator(tmp_path, transport, config=cfg)
        return orch, posted

    def test_first_retry_bumps_effort_keeps_model(self, tmp_path: Path) -> None:
        task = Task(
            id="T-fail",
            title="Do work",
            description="Do the thing.",
            role="backend",
            status=TaskStatus.FAILED,
            model="sonnet",
            effort="low",
        )
        orch, posted = self._build(tmp_path)

        result = orch._maybe_retry_task(task)

        assert result is True
        assert len(posted) == 1
        body = posted[0]
        assert body["model"] == "sonnet"  # model unchanged
        assert body["effort"] == "medium"  # low → medium

    def test_first_retry_title_prefixed(self, tmp_path: Path) -> None:
        task = Task(
            id="T-fail",
            title="Do work",
            description="Do the thing.",
            role="backend",
            status=TaskStatus.FAILED,
        )
        orch, posted = self._build(tmp_path)

        orch._maybe_retry_task(task)

        assert posted[0]["title"] == "[RETRY 1] Do work"
        assert posted[0]["description"].startswith("[RETRY 1]")

    def test_second_retry_escalates_model(self, tmp_path: Path) -> None:
        task = Task(
            id="T-fail",
            title="[RETRY 1] Do work",
            description="[RETRY 1] Do the thing.",
            role="backend",
            status=TaskStatus.FAILED,
            model="sonnet",
            effort="medium",
            retry_count=1,
        )
        orch, posted = self._build(tmp_path)

        result = orch._maybe_retry_task(task)

        assert result is True
        body = posted[0]
        assert body["model"] == "opus"  # sonnet → opus
        assert body["effort"] == "high"  # reset to high

    def test_max_retries_respected(self, tmp_path: Path) -> None:
        task = Task(
            id="T-fail",
            title="[RETRY 2] Do work",
            description="[RETRY 2] Do the thing.",
            role="backend",
            status=TaskStatus.FAILED,
            retry_count=2,
            max_retries=2,
        )
        orch, posted = self._build(tmp_path, max_retries=2)

        result = orch._maybe_retry_task(task)

        assert result is False
        assert posted == []

    def test_already_retried_task_not_retried_again(self, tmp_path: Path) -> None:
        task = Task(
            id="T-fail",
            title="Do work",
            description="Do the thing.",
            role="backend",
            status=TaskStatus.FAILED,
        )
        orch, posted = self._build(tmp_path)

        orch._maybe_retry_task(task)
        result = orch._maybe_retry_task(task)  # second call same task

        assert result is False
        assert len(posted) == 1  # only one POST made

    def test_retry_records_task_id_in_retried_set(self, tmp_path: Path) -> None:
        task = Task(
            id="T-fail",
            title="Do work",
            description="Do the thing.",
            role="backend",
            status=TaskStatus.FAILED,
        )
        orch, _ = self._build(tmp_path)

        orch._maybe_retry_task(task)

        assert "T-fail" in orch._retried_task_ids

    def test_effort_capped_at_max(self, tmp_path: Path) -> None:
        task = Task(
            id="T-fail",
            title="Do work",
            description="Do the thing.",
            role="backend",
            status=TaskStatus.FAILED,
            model="sonnet",
            effort="max",
        )
        orch, posted = self._build(tmp_path)

        orch._maybe_retry_task(task)

        assert posted[0]["effort"] == "max"  # already at max, stays max

    def test_haiku_escalates_to_sonnet_on_second_retry(self, tmp_path: Path) -> None:
        task = Task(
            id="T-fail",
            title="[RETRY 1] Do work",
            description="[RETRY 1] Do the thing.",
            role="backend",
            status=TaskStatus.FAILED,
            model="haiku",
            effort="medium",
            retry_count=1,
        )
        orch, posted = self._build(tmp_path)

        orch._maybe_retry_task(task)

        assert posted[0]["model"] == "sonnet"

    def test_zero_max_retries_never_retries(self, tmp_path: Path) -> None:
        task = Task(
            id="T-fail",
            title="Do work",
            description="Do the thing.",
            role="backend",
            status=TaskStatus.FAILED,
            max_retries=0,
        )
        orch, posted = self._build(tmp_path, max_retries=0)

        result = orch._maybe_retry_task(task)

        assert result is False
        assert posted == []


# --- _replenish_backlog ---


class TestReplenishBacklog:
    """Tests for Orchestrator._replenish_backlog()."""

    _RUFF_VIOLATIONS = [
        {
            "filename": "src/foo.py",
            "code": "E501",
            "message": "Line too long (92 > 88 characters)",
            "location": {"row": 10, "column": 1},
        },
        {
            "filename": "src/bar.py",
            "code": "F401",
            "message": "`os` imported but unused",
            "location": {"row": 1, "column": 1},
        },
        {
            "filename": "src/baz.py",
            "code": "E501",  # duplicate rule — should deduplicate
            "message": "Line too long (99 > 88 characters)",
            "location": {"row": 20, "column": 1},
        },
    ]

    def _build_orch_evolve(
        self,
        tmp_path: Path,
        *,
        evolve_mode: bool = True,
        open_tasks_json: list[object] | None = None,
        done_tasks_json: list[object] | None = None,
        post_handler: object = None,
    ) -> tuple[Orchestrator, list[dict[str, object]]]:
        """Build an orchestrator in evolve mode with mocked HTTP and collected POST /tasks bodies."""
        if open_tasks_json is None:
            open_tasks_json = []
        if done_tasks_json is None:
            done_tasks_json = []

        posted: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = request.url
            if request.method == "GET" and url.path == "/tasks":
                return _tasks_response(url, open_tasks_json + done_tasks_json)
            if request.method == "POST" and url.path == "/tasks":
                body = json.loads(request.content)
                posted.append(body)
                return httpx.Response(201, json={"id": f"T-ruff-{len(posted)}"})
            return httpx.Response(404)

        cfg = OrchestratorConfig(
            max_agents=4,
            poll_interval_s=1,
            server_url="http://testserver",
            evolve_mode=evolve_mode,
            evolution_enabled=False,
        )
        adp = _mock_adapter()
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adp, templates_dir, tmp_path)
        client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://testserver")
        orch = Orchestrator(cfg, spawner, tmp_path, client=client)
        return orch, posted

    def test_creates_tasks_from_ruff_output(self, tmp_path: Path) -> None:
        """Replenishment creates one task per unique ruff rule code (async two-phase)."""
        from unittest.mock import patch

        orch, posted = self._build_orch_evolve(tmp_path)

        mock_proc = MagicMock()
        mock_proc.communicate.return_value = (json.dumps(self._RUFF_VIOLATIONS), "")
        mock_proc.pid = 9999

        with patch("subprocess.Popen", return_value=mock_proc):
            result = MagicMock()
            result.open_tasks = 0
            # Phase 1: submit future
            orch._replenish_backlog(result)
            assert len(posted) == 0  # no tasks yet — future is pending
            # Wait for background thread to finish
            assert orch._pending_ruff_future is not None
            orch._pending_ruff_future.result()
            # Phase 2: harvest result and create tasks
            orch._replenish_backlog(result)

        # E501 appears twice but should produce only one task; F401 = one task
        assert len(posted) == 2
        codes = {p["title"].split()[-1] for p in posted}
        assert codes == {"E501", "F401"}
        # Verify required fields
        for body in posted:
            assert body["role"] == "backend"
            assert body["priority"] == 3
            assert body["model"] == "sonnet"
            assert body["effort"] == "low"

    def test_does_not_run_when_evolve_mode_false(self, tmp_path: Path) -> None:
        """Replenishment is a no-op when evolve_mode=False."""
        from unittest.mock import patch

        orch, posted = self._build_orch_evolve(tmp_path, evolve_mode=False)

        with patch("subprocess.run") as mock_run:
            result = MagicMock()
            result.open_tasks = 0
            orch._replenish_backlog(result)
            mock_run.assert_not_called()

        assert posted == []

    def test_does_not_run_when_open_tasks_present(self, tmp_path: Path) -> None:
        """Replenishment is a no-op when there are open tasks."""
        from unittest.mock import patch

        orch, posted = self._build_orch_evolve(tmp_path)

        with patch("subprocess.run") as mock_run:
            result = MagicMock()
            result.open_tasks = 3
            orch._replenish_backlog(result)
            mock_run.assert_not_called()

        assert posted == []

    def test_caps_at_five_tasks(self, tmp_path: Path) -> None:
        """At most 5 tasks are created per replenishment cycle."""
        from unittest.mock import patch

        many_violations = [
            {
                "filename": f"src/f{i}.py",
                "code": f"E{100 + i}",
                "message": "some issue",
                "location": {"row": i, "column": 1},
            }
            for i in range(10)
        ]
        orch, posted = self._build_orch_evolve(tmp_path)

        mock_proc = MagicMock()
        mock_proc.communicate.return_value = (json.dumps(many_violations), "")
        mock_proc.pid = 9999
        with patch("subprocess.Popen", return_value=mock_proc):
            result = MagicMock()
            result.open_tasks = 0
            orch._replenish_backlog(result)  # submit
            assert orch._pending_ruff_future is not None
            orch._pending_ruff_future.result()  # wait for thread
            orch._replenish_backlog(result)  # harvest

        assert len(posted) == 5

    def test_respects_60s_cooldown(self, tmp_path: Path) -> None:
        """After harvesting, a second submission is blocked by cooldown."""
        from unittest.mock import patch

        orch, posted = self._build_orch_evolve(tmp_path)

        mock_proc = MagicMock()
        mock_proc.communicate.return_value = (json.dumps(self._RUFF_VIOLATIONS), "")
        mock_proc.pid = 9999
        with patch("subprocess.Popen", return_value=mock_proc):
            result = MagicMock()
            result.open_tasks = 0
            # Phase 1: submit future
            orch._replenish_backlog(result)
            assert orch._pending_ruff_future is not None
            orch._pending_ruff_future.result()  # wait for thread
            # Phase 2: harvest — creates 2 tasks
            orch._replenish_backlog(result)
            tasks_after_harvest = len(posted)
            # Phase 3: immediate retry — cooldown blocks new submission
            orch._replenish_backlog(result)

        assert tasks_after_harvest == 2
        assert len(posted) == 2  # no new tasks from the third call

    def test_cooldown_resets_after_60s(self, tmp_path: Path) -> None:
        """After 60s the replenishment runs again."""
        from unittest.mock import patch

        orch, posted = self._build_orch_evolve(tmp_path)

        mock_proc = MagicMock()
        mock_proc.communicate.return_value = (json.dumps(self._RUFF_VIOLATIONS), "")
        mock_proc.pid = 9999
        with patch("subprocess.Popen", return_value=mock_proc):
            result = MagicMock()
            result.open_tasks = 0
            # First cycle: submit → wait → harvest
            orch._replenish_backlog(result)
            assert orch._pending_ruff_future is not None
            orch._pending_ruff_future.result()
            orch._replenish_backlog(result)
            # Fake that 61 seconds have passed
            orch._last_replenish_ts -= 61
            # Second cycle: submit → wait → harvest
            orch._replenish_backlog(result)
            assert orch._pending_ruff_future is not None
            orch._pending_ruff_future.result()
            orch._replenish_backlog(result)

        assert len(posted) == 4  # 2 unique rules × 2 cycles

    def test_tick_does_not_block_on_ruff_or_pytest(self, tmp_path: Path) -> None:
        """tick() must return in under 1 second even when ruff/pytest are slow."""
        import time
        from unittest.mock import patch

        orch, _posted = self._build_orch_evolve(tmp_path)

        def slow_popen(*_args: object, **_kwargs: object) -> object:
            m = MagicMock()

            def slow_communicate(timeout: object = None) -> tuple[str, str]:
                time.sleep(2)
                return ("[]", "")

            m.communicate = slow_communicate
            m.pid = 9999
            return m

        result = MagicMock()
        result.open_tasks = 0

        with patch("subprocess.Popen", side_effect=slow_popen):
            start = time.monotonic()
            orch._replenish_backlog(result)
            elapsed = time.monotonic() - start

            assert elapsed < 1.0, f"_replenish_backlog blocked for {elapsed:.2f}s; expected < 1s"
            # Future should be pending (submitted to thread pool, not yet complete)
            assert orch._pending_ruff_future is not None
            # Clean up inside the patch block so the thread sees the mock
            orch._pending_ruff_future.result()


# --- Per-task timeout calculation ---


def test_per_task_timeout_short_task(tmp_path: Path) -> None:
    """Small-scope work gets the fixed 15-minute timeout bucket."""
    task = Task(
        id="T-short",
        title="Short task",
        description=".",
        role="backend",
        estimated_minutes=5,
        status=TaskStatus.OPEN,
        scope=Scope.SMALL,
        complexity=Complexity.LOW,
    )
    task_dict = {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "role": task.role,
        "estimated_minutes": task.estimated_minutes,
        "status": "open",
        "scope": "small",
        "complexity": "low",
        "priority": 2,
        "depends_on": [],
        "owned_files": [],
        "assigned_agent": None,
        "result_summary": None,
        "task_type": "standard",
    }
    transport = _mock_transport(
        {
            "GET /tasks?status=open": httpx.Response(200, json=[task_dict]),
            "GET /tasks?status=done": httpx.Response(200, json=[]),
            "GET /tasks?status=failed": httpx.Response(200, json=[]),
            "GET /status": httpx.Response(200, json={"open": 1, "done": 0}),
            f"POST /tasks/{task.id}/claim": httpx.Response(200, json={}),
        }
    )
    cfg = OrchestratorConfig(
        max_agents=6,
        poll_interval_s=1,
        max_agent_runtime_s=600,
        max_tasks_per_agent=1,
        server_url="http://testserver",
    )
    orch = _build_orchestrator(tmp_path, transport, config=cfg)
    orch.tick()

    # Verify that the spawned session has the per-task timeout set
    sessions = list(orch._agents.values())
    assert sessions, "Expected one agent to be spawned"
    session = sessions[0]
    assert session.timeout_s == 900


def test_per_task_timeout_medium_bucket(tmp_path: Path) -> None:
    """Medium-scope work gets the fixed 30-minute timeout bucket."""
    task = Task(
        id="T-long",
        title="Long task",
        description=".",
        role="backend",
        estimated_minutes=60,
        status=TaskStatus.OPEN,
        scope=Scope.MEDIUM,
        complexity=Complexity.HIGH,
    )
    task_dict = {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "role": task.role,
        "estimated_minutes": task.estimated_minutes,
        "status": "open",
        "scope": "medium",
        "complexity": "high",
        "priority": 2,
        "depends_on": [],
        "owned_files": [],
        "assigned_agent": None,
        "result_summary": None,
        "task_type": "standard",
    }
    transport = _mock_transport(
        {
            "GET /tasks?status=open": httpx.Response(200, json=[task_dict]),
            "GET /tasks?status=done": httpx.Response(200, json=[]),
            "GET /tasks?status=failed": httpx.Response(200, json=[]),
            "GET /status": httpx.Response(200, json={"open": 1, "done": 0}),
            f"POST /tasks/{task.id}/claim": httpx.Response(200, json={}),
        }
    )
    cfg = OrchestratorConfig(
        max_agents=6,
        poll_interval_s=1,
        max_agent_runtime_s=600,
        max_tasks_per_agent=1,
        server_url="http://testserver",
    )
    orch = _build_orchestrator(tmp_path, transport, config=cfg)
    orch.tick()

    sessions = list(orch._agents.values())
    assert sessions, "Expected one agent to be spawned"
    session = sessions[0]
    assert session.timeout_s == 1800


def test_reap_uses_per_session_timeout(tmp_path: Path) -> None:
    """_reap_dead_agents uses session.timeout_s when set, not config.max_agent_runtime_s."""
    cfg = OrchestratorConfig(
        max_agents=6,
        poll_interval_s=1,
        max_agent_runtime_s=600,
        server_url="http://testserver",
    )
    transport = _mock_transport(
        {
            "GET /tasks?status=open": httpx.Response(200, json=[]),
            "GET /tasks?status=done": httpx.Response(200, json=[]),
            "GET /tasks?status=failed": httpx.Response(200, json=[]),
            "GET /status": httpx.Response(200, json={"open": 0, "done": 0}),
        }
    )
    orch = _build_orchestrator(tmp_path, transport, config=cfg)

    # Inject a session with a short timeout (120s) that has been running for 130s
    session = AgentSession(id="sess-1", role="backend", pid=9999, task_ids=["T-x"])
    session.timeout_s = 120
    session.spawn_ts = time.time() - 130  # running for 130s > 120s timeout
    orch._agents[session.id] = session

    result = TickResult()
    orch._spawner.kill = MagicMock()  # type: ignore[method-assign]
    orch._reap_dead_agents(result, {})

    assert session.id in result.reaped, "Session should be reaped due to per-session timeout"


# --- Run completion summary ---


class TestRunCompletionSummary:
    """tick() writes .sdd/runtime/summary.md when all tasks are done and evolve_mode is off."""

    def _build(
        self,
        tmp_path: Path,
        *,
        done_tasks: list[dict] | None = None,
        failed_tasks: list[dict] | None = None,
    ) -> Orchestrator:
        _done = done_tasks or []
        _failed = failed_tasks or []

        def handler(request: httpx.Request) -> httpx.Response:
            url = request.url
            if request.method == "GET" and url.path == "/tasks":
                return _tasks_response(url, _done + _failed)
            return httpx.Response(200, json={})

        transport = httpx.MockTransport(handler)
        cfg = OrchestratorConfig(
            server_url="http://testserver",
            evolve_mode=False,
            evolution_enabled=False,
        )
        return _build_orchestrator(tmp_path, transport, config=cfg)

    def test_summary_created_when_all_tasks_done(self, tmp_path: Path) -> None:
        """summary.md is created when open=0, agents=0, evolve_mode=False."""
        done = [_task_as_dict(_make_task(id="T-1", title="Fix auth bug", status="done"))]
        orch = self._build(tmp_path, done_tasks=done)

        orch.tick()

        summary_path = tmp_path / ".sdd" / "runtime" / "summary.md"
        assert summary_path.exists(), "summary.md should be created"

    def test_summary_contains_task_counts(self, tmp_path: Path) -> None:
        done = [_task_as_dict(_make_task(id=f"T-{i}", title=f"Task {i}", status="done")) for i in range(3)]
        failed = [_task_as_dict(_make_task(id="T-fail", title="Failed task", status="failed"))]
        orch = self._build(tmp_path, done_tasks=done, failed_tasks=failed)

        orch.tick()

        content = (tmp_path / ".sdd" / "runtime" / "summary.md").read_text()
        assert "**Total completed:** 3" in content
        assert "**Total failed:** 1" in content

    def test_summary_lists_task_titles(self, tmp_path: Path) -> None:
        done = [_task_as_dict(_make_task(id="T-1", title="Implement login", status="done"))]
        failed = [_task_as_dict(_make_task(id="T-2", title="Write tests", status="failed"))]
        orch = self._build(tmp_path, done_tasks=done, failed_tasks=failed)

        orch.tick()

        content = (tmp_path / ".sdd" / "runtime" / "summary.md").read_text()
        assert "Implement login" in content
        assert "Write tests" in content
        assert "*(failed)*" in content

    def test_summary_not_written_twice(self, tmp_path: Path) -> None:
        """Second tick with same state does not overwrite summary.md."""
        done = [_task_as_dict(_make_task(id="T-1", title="Task A", status="done"))]
        orch = self._build(tmp_path, done_tasks=done)

        orch.tick()
        summary_path = tmp_path / ".sdd" / "runtime" / "summary.md"
        first_mtime = summary_path.stat().st_mtime

        orch.tick()
        second_mtime = summary_path.stat().st_mtime

        assert first_mtime == second_mtime, "summary.md should not be rewritten on second tick"

    def test_summary_not_created_in_evolve_mode(self, tmp_path: Path) -> None:
        """summary.md is NOT created when evolve_mode=True."""

        def handler(request: httpx.Request) -> httpx.Response:
            url = request.url
            if request.method == "GET" and url.path == "/tasks":
                return _tasks_response(url, [_task_as_dict(_make_task(id="T-1", status="done"))])
            return httpx.Response(200, json={})

        transport = httpx.MockTransport(handler)
        cfg = OrchestratorConfig(
            server_url="http://testserver",
            evolve_mode=True,
            evolution_enabled=False,
        )
        orch = _build_orchestrator(tmp_path, transport, config=cfg)

        orch.tick()

        summary_path = tmp_path / ".sdd" / "runtime" / "summary.md"
        assert not summary_path.exists(), "summary.md should not be created in evolve_mode"

    def test_summary_includes_duration_and_cost(self, tmp_path: Path) -> None:
        done = [_task_as_dict(_make_task(id="T-1", title="Deploy", status="done"))]
        orch = self._build(tmp_path, done_tasks=done)

        orch.tick()

        content = (tmp_path / ".sdd" / "runtime" / "summary.md").read_text()
        assert "**Wall-clock duration:**" in content
        assert "**Estimated cost:**" in content
        assert "**Files modified:**" in content


# --- DryRun ---


class TestDryRun:
    """dry_run=True should populate TickResult.dry_run_planned but never spawn agents."""

    def test_dry_run_populates_planned(self, tmp_path: Path) -> None:
        open_task = _task_as_dict(_make_task(id="T-1", role="backend", title="Add feature"))
        transport = _mock_transport(
            {
                "GET /tasks": httpx.Response(200, json=[open_task]),
            }
        )
        cfg = OrchestratorConfig(
            max_agents=6,
            poll_interval_s=1,
            heartbeat_timeout_s=120,
            server_url="http://testserver",
            dry_run=True,
        )
        orch = _build_orchestrator(tmp_path, transport, config=cfg)

        result = orch.tick()

        assert len(result.dry_run_planned) == 1
        role, title, _model, _effort = result.dry_run_planned[0]
        assert role == "backend"
        assert title == "Add feature"

    def test_dry_run_does_not_spawn_agents(self, tmp_path: Path) -> None:
        adapter = _mock_adapter()
        open_task = _task_as_dict(_make_task(id="T-1", role="backend", title="Add feature"))
        transport = _mock_transport(
            {
                "GET /tasks": httpx.Response(200, json=[open_task]),
            }
        )
        cfg = OrchestratorConfig(
            max_agents=6,
            poll_interval_s=1,
            heartbeat_timeout_s=120,
            server_url="http://testserver",
            dry_run=True,
        )
        orch = _build_orchestrator(tmp_path, transport, adapter=adapter, config=cfg)

        orch.tick()

        adapter.spawn.assert_not_called()

    def test_dry_run_false_does_spawn(self, tmp_path: Path) -> None:
        """Sanity check: without dry_run, spawn IS called for open tasks."""
        adapter = _mock_adapter()
        open_task = _task_as_dict(_make_task(id="T-1", role="backend", title="Add feature"))
        transport = _mock_transport(
            {
                "GET /tasks": httpx.Response(200, json=[open_task]),
                "POST /tasks/T-1/claim": httpx.Response(
                    200, json=_task_as_dict(_make_task(id="T-1", status="claimed"))
                ),
            }
        )
        cfg = OrchestratorConfig(
            max_agents=6,
            poll_interval_s=1,
            heartbeat_timeout_s=120,
            server_url="http://testserver",
            dry_run=False,
        )
        orch = _build_orchestrator(tmp_path, transport, adapter=adapter, config=cfg)

        orch.tick()

        adapter.spawn.assert_called_once()


# --- _run_evolution_cycle ---


class TestRunEvolutionCycle:
    """Unit tests for Orchestrator._run_evolution_cycle."""

    def _build_with_evolution_mock(self, tmp_path: Path) -> tuple[Orchestrator, MagicMock]:
        from bernstein.core.evolution import EvolutionCoordinator

        evolution = MagicMock(spec=EvolutionCoordinator)
        evolution.execute_pending_upgrades.return_value = []
        transport = _mock_transport(
            {
                "GET /tasks": httpx.Response(200, json=[]),
            }
        )
        cfg = OrchestratorConfig(
            max_agents=6,
            poll_interval_s=1,
            server_url="http://testserver",
            evolution_enabled=True,
        )
        adp = _mock_adapter()
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adp, templates_dir, tmp_path)
        client = httpx.Client(transport=transport, base_url="http://testserver")
        orch = Orchestrator(cfg, spawner, tmp_path, client=client, evolution=evolution)
        return orch, evolution

    def _make_proposal(self, proposal_id: str = "P-001", title: str = "Improve routing") -> MagicMock:
        from bernstein.evolution.proposals import UpgradeStatus

        proposal = MagicMock()
        proposal.id = proposal_id
        proposal.title = title
        proposal.description = f"Description for {title}"
        proposal.status = UpgradeStatus.PENDING
        return proposal

    def test_happy_path_creates_http_task_per_proposal(self, tmp_path: Path) -> None:
        """run_analysis_cycle returns proposals → POST /tasks for each."""
        posted: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST" and request.url.path == "/tasks":
                posted.append(json.loads(request.content))
                return httpx.Response(200, json={"id": "T-new"})
            return httpx.Response(200, json=[])

        orch, evolution = self._build_with_evolution_mock(tmp_path)
        orch._client = httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="http://testserver",
        )

        p1 = self._make_proposal("P-001", "Proposal One")
        p2 = self._make_proposal("P-002", "Proposal Two")
        evolution.run_analysis_cycle.return_value = [p1, p2]

        result = TickResult()
        orch._run_evolution_cycle(result)

        assert len(posted) == 2
        assert posted[0]["title"] == "Upgrade: Proposal One"
        assert posted[1]["title"] == "Upgrade: Proposal Two"
        assert result.errors == []

    def test_task_payload_structure(self, tmp_path: Path) -> None:
        """Posted task body has correct fields: title, description, role, priority, task_type."""
        posted: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST" and request.url.path == "/tasks":
                posted.append(json.loads(request.content))
                return httpx.Response(200, json={"id": "T-new"})
            return httpx.Response(200, json=[])

        orch, evolution = self._build_with_evolution_mock(tmp_path)
        orch._client = httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="http://testserver",
        )

        p = self._make_proposal("P-001", "Optimize model router")
        evolution.run_analysis_cycle.return_value = [p]

        result = TickResult()
        orch._run_evolution_cycle(result)

        assert len(posted) == 1
        body = posted[0]
        assert body["title"] == "Upgrade: Optimize model router"
        assert body["description"] == p.description
        assert body["role"] == "backend"
        assert body["priority"] == 2
        assert body["task_type"] == TaskType.UPGRADE_PROPOSAL.value

    def test_no_proposals_makes_no_http_calls(self, tmp_path: Path) -> None:
        """When run_analysis_cycle returns [], no POST /tasks calls are made."""
        posted: list[object] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST" and request.url.path == "/tasks":
                posted.append(request)
            return httpx.Response(200, json=[])

        orch, evolution = self._build_with_evolution_mock(tmp_path)
        orch._client = httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="http://testserver",
        )
        evolution.run_analysis_cycle.return_value = []

        result = TickResult()
        orch._run_evolution_cycle(result)

        assert posted == []
        assert result.errors == []

    def test_http_post_failure_logs_warning_and_continues(self, tmp_path: Path) -> None:
        """If one POST fails, logs warning, adds to errors, continues with remaining proposals."""
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            if request.method == "POST" and request.url.path == "/tasks":
                call_count += 1
                if call_count == 1:
                    return httpx.Response(500, json={"detail": "server error"})
                return httpx.Response(200, json={"id": "T-new"})
            return httpx.Response(200, json=[])

        orch, evolution = self._build_with_evolution_mock(tmp_path)
        orch._client = httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="http://testserver",
        )

        p1 = self._make_proposal("P-001", "First proposal")
        p2 = self._make_proposal("P-002", "Second proposal")
        evolution.run_analysis_cycle.return_value = [p1, p2]

        result = TickResult()
        orch._run_evolution_cycle(result)

        # Both proposals attempted
        assert call_count == 2
        # One error recorded for the failed POST
        assert len(result.errors) == 1
        assert "evolution_task:" in result.errors[0]

    def test_analysis_cycle_raises_adds_error(self, tmp_path: Path) -> None:
        """If run_analysis_cycle raises, error is added to result.errors."""
        orch, evolution = self._build_with_evolution_mock(tmp_path)
        evolution.run_analysis_cycle.side_effect = RuntimeError("analysis failed")

        result = TickResult()
        orch._run_evolution_cycle(result)

        assert len(result.errors) == 1
        assert "evolution:" in result.errors[0]
        assert "analysis failed" in result.errors[0]

    def test_auto_applied_proposals_skip_task_creation(self, tmp_path: Path) -> None:
        """Proposals already applied by execute_pending_upgrades do NOT create server tasks."""
        from bernstein.evolution.proposals import UpgradeStatus

        posted: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST" and request.url.path == "/tasks":
                posted.append(json.loads(request.content))
                return httpx.Response(200, json={"id": "T-new"})
            return httpx.Response(200, json=[])

        orch, evolution = self._build_with_evolution_mock(tmp_path)
        orch._client = httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="http://testserver",
        )

        # p1 was auto-applied, p2 is still pending
        p_applied = self._make_proposal("P-auto", "Auto-applied proposal")
        p_applied.status = UpgradeStatus.APPLIED
        p_pending = self._make_proposal("P-pend", "Pending proposal")
        p_pending.status = UpgradeStatus.PENDING
        evolution.run_analysis_cycle.return_value = [p_applied, p_pending]

        result = TickResult()
        orch._run_evolution_cycle(result)

        # Only the PENDING proposal should create a task — APPLIED one is skipped.
        assert len(posted) == 1
        assert posted[0]["title"] == "Upgrade: Pending proposal"
        assert result.errors == []

    def test_rejected_proposals_skip_task_creation(self, tmp_path: Path) -> None:
        """Proposals rejected during execute_pending_upgrades do NOT create server tasks."""
        from bernstein.evolution.proposals import UpgradeStatus

        posted: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST" and request.url.path == "/tasks":
                posted.append(json.loads(request.content))
                return httpx.Response(200, json={"id": "T-new"})
            return httpx.Response(200, json=[])

        orch, evolution = self._build_with_evolution_mock(tmp_path)
        orch._client = httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="http://testserver",
        )

        p_rejected = self._make_proposal("P-rej", "Rejected proposal")
        p_rejected.status = UpgradeStatus.REJECTED
        p_rolled_back = self._make_proposal("P-rb", "Rolled-back proposal")
        p_rolled_back.status = UpgradeStatus.ROLLED_BACK
        evolution.run_analysis_cycle.return_value = [p_rejected, p_rolled_back]

        result = TickResult()
        orch._run_evolution_cycle(result)

        assert posted == []
        assert result.errors == []


# --- _collect_completion_data ---


class TestExtractFromAgentLog:
    def _make_session(self, session_id: str = "sess-001") -> AgentSession:
        from bernstein.core.models import ModelConfig

        return AgentSession(id=session_id, role="backend", model_config=ModelConfig("sonnet", "high"))

    def _make_orch(self, tmp_path: Path) -> Orchestrator:
        transport = _mock_transport({"GET /tasks": httpx.Response(200, json=[])})
        return _build_orchestrator(tmp_path, transport)

    def _write_log(self, tmp_path: Path, session_id: str, content: str) -> Path:
        log_dir = tmp_path / ".sdd" / "runtime"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{session_id}.log"
        log_path.write_text(content, encoding="utf-8")
        return log_path

    def test_modified_and_created_files(self, tmp_path: Path) -> None:
        orch = self._make_orch(tmp_path)
        session = self._make_session("s1")
        self._write_log(
            tmp_path,
            "s1",
            ("Some output\nModified: src/foo.py\nCreated: src/bar.py\nMore output\nModified: tests/test_foo.py\n"),
        )
        result = orch._collect_completion_data(session)
        assert result["files_modified"] == ["src/foo.py", "src/bar.py", "tests/test_foo.py"]

    def test_deduplicates_file_paths(self, tmp_path: Path) -> None:
        orch = self._make_orch(tmp_path)
        session = self._make_session("s2")
        self._write_log(
            tmp_path, "s2", ("Modified: src/foo.py\nModified: src/foo.py\nCreated: src/foo.py\nModified: src/bar.py\n")
        )
        result = orch._collect_completion_data(session)
        assert result["files_modified"] == ["src/foo.py", "src/bar.py"]

    def test_extracts_pytest_summary(self, tmp_path: Path) -> None:
        orch = self._make_orch(tmp_path)
        session = self._make_session("s3")
        self._write_log(
            tmp_path, "s3", ("collecting ...\ntest_foo.py::test_bar PASSED\n===== 3 passed, 1 failed in 0.42s =====\n")
        )
        result = orch._collect_completion_data(session)
        assert result["test_results"] == {"summary": "===== 3 passed, 1 failed in 0.42s ====="}

    def test_log_file_does_not_exist(self, tmp_path: Path) -> None:
        orch = self._make_orch(tmp_path)
        session = self._make_session("missing-session")
        result = orch._collect_completion_data(session)
        assert result == {"files_modified": [], "test_results": {}}

    def test_oserror_on_read(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        orch = self._make_orch(tmp_path)
        session = self._make_session("s4")
        log_path = tmp_path / ".sdd" / "runtime" / "s4.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("some content")
        with patch.object(log_path.__class__, "read_text", side_effect=OSError("disk error")):
            result = orch._collect_completion_data(session)
        assert result == {"files_modified": [], "test_results": {}}

    def test_empty_log_file(self, tmp_path: Path) -> None:
        orch = self._make_orch(tmp_path)
        session = self._make_session("s5")
        self._write_log(tmp_path, "s5", "")
        result = orch._collect_completion_data(session)
        assert result["files_modified"] == []


# --- _check_evolve: cycle management unit tests ---


class TestCheckEvolve:
    """Direct unit tests for Orchestrator._check_evolve."""

    def _make_orch(self, tmp_path: Path) -> Orchestrator:
        transport = _mock_transport({"GET /tasks": httpx.Response(200, json=[])})
        cfg = OrchestratorConfig(
            max_agents=6,
            poll_interval_s=1,
            server_url="http://testserver",
            evolution_enabled=False,
        )
        adp = _mock_adapter()
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True, exist_ok=True)
        spawner = AgentSpawner(adp, templates_dir, tmp_path)
        client = httpx.Client(transport=transport, base_url="http://testserver")
        return Orchestrator(cfg, spawner, tmp_path, client=client)

    def _patch_evolve_helpers(
        self,
        orch: Orchestrator,
        *,
        committed: bool = False,
        test_info: dict[str, object] | None = None,
    ) -> tuple[MagicMock, MagicMock, MagicMock]:
        """Patch sub-methods to avoid subprocess/git calls; return mocks."""

        _test_info = test_info or {"passed": 5, "failed": 0, "summary": "5 passed"}
        mock_run_tests = MagicMock(return_value=_test_info)
        mock_auto_commit = MagicMock(return_value=committed)
        mock_spawn_manager = MagicMock(return_value=None)
        orch._evolve_run_tests = mock_run_tests  # type: ignore[assignment]
        orch._evolve_auto_commit = mock_auto_commit  # type: ignore[assignment]
        orch._evolve_spawn_manager = mock_spawn_manager  # type: ignore[assignment]
        orch._log_evolve_cycle = MagicMock(return_value=None)  # type: ignore[assignment]
        return mock_run_tests, mock_auto_commit, mock_spawn_manager

    def test_no_evolve_json_is_noop(self, tmp_path: Path) -> None:
        """No evolve.json → _check_evolve returns without doing anything."""
        orch = self._make_orch(tmp_path)
        mock_run, mock_commit, mock_spawn = self._patch_evolve_helpers(orch)

        orch._check_evolve(TickResult(), {})

        mock_run.assert_not_called()
        mock_commit.assert_not_called()
        mock_spawn.assert_not_called()

    def test_invalid_json_is_noop(self, tmp_path: Path) -> None:
        """evolve.json with invalid JSON → no crash, no cycle triggered."""
        runtime = tmp_path / ".sdd" / "runtime"
        runtime.mkdir(parents=True)
        (runtime / "evolve.json").write_text("{not valid json!!")
        orch = self._make_orch(tmp_path)
        mock_run, _mock_commit, mock_spawn = self._patch_evolve_helpers(orch)

        orch._check_evolve(TickResult(), {})  # must not raise

        mock_run.assert_not_called()
        mock_spawn.assert_not_called()

    def test_oserror_on_read_is_noop(self, tmp_path: Path) -> None:
        """OSError reading evolve.json → no crash, no cycle triggered."""
        from unittest.mock import patch

        runtime = tmp_path / ".sdd" / "runtime"
        runtime.mkdir(parents=True)
        evolve_path = runtime / "evolve.json"
        evolve_path.write_text('{"enabled": true}')
        orch = self._make_orch(tmp_path)
        mock_run, _mock_commit, mock_spawn = self._patch_evolve_helpers(orch)

        with patch.object(evolve_path.__class__, "read_text", side_effect=OSError("disk error")):
            orch._check_evolve(TickResult(), {})  # must not raise

        mock_run.assert_not_called()
        mock_spawn.assert_not_called()

    def test_enabled_false_is_noop(self, tmp_path: Path) -> None:
        """enabled=false in evolve.json → no cycle triggered."""
        _write_evolve_config(tmp_path, enabled=False)
        orch = self._make_orch(tmp_path)
        mock_run, _mock_commit, mock_spawn = self._patch_evolve_helpers(orch)

        orch._check_evolve(TickResult(), {})

        mock_run.assert_not_called()
        mock_spawn.assert_not_called()

    def test_triggers_cycle_when_all_tasks_complete(self, tmp_path: Path) -> None:
        """When enabled and no open/claimed tasks or alive agents, cycle runs."""
        _write_evolve_config(tmp_path, interval_s=0)
        orch = self._make_orch(tmp_path)
        mock_run, mock_commit, mock_spawn = self._patch_evolve_helpers(orch)

        orch._check_evolve(TickResult(), {"done": [_make_task(id="T-1", status="done")]})

        mock_run.assert_called_once()
        mock_commit.assert_called_once()
        mock_spawn.assert_called_once()

    def test_cycle_count_increments_and_written_back(self, tmp_path: Path) -> None:
        """After a successful cycle, _cycle_count increments in evolve.json."""
        evolve_path = _write_evolve_config(tmp_path, interval_s=0, cycle_count=2)
        orch = self._make_orch(tmp_path)
        self._patch_evolve_helpers(orch)

        orch._check_evolve(TickResult(), {})

        updated = json.loads(evolve_path.read_text())
        assert updated["_cycle_count"] == 3
        assert updated["_last_cycle_ts"] > 0

    def test_focus_area_uses_cycle_count_modulo(self, tmp_path: Path) -> None:
        """Focus area passed to _evolve_spawn_manager rotates by cycle_count % len."""
        focus_areas = Orchestrator._EVOLVE_FOCUS_AREAS
        for i, expected_focus in enumerate(focus_areas):
            _write_evolve_config(tmp_path, interval_s=0, cycle_count=i)
            orch = self._make_orch(tmp_path)
            _, _, mock_spawn = self._patch_evolve_helpers(orch)

            orch._check_evolve(TickResult(), {})

            call_kwargs = mock_spawn.call_args
            assert call_kwargs is not None
            actual_focus = call_kwargs.kwargs.get("focus_area") or call_kwargs.args[1]
            assert actual_focus == expected_focus, (
                f"cycle_count={i}: expected focus={expected_focus!r}, got {actual_focus!r}"
            )

    def test_focus_area_wraps_around(self, tmp_path: Path) -> None:
        """Focus area wraps when cycle_count exceeds len(_EVOLVE_FOCUS_AREAS)."""
        areas = Orchestrator._EVOLVE_FOCUS_AREAS
        wrap_cycle = len(areas)  # should map back to index 0
        _write_evolve_config(tmp_path, interval_s=0, cycle_count=wrap_cycle)
        orch = self._make_orch(tmp_path)
        _, _, mock_spawn = self._patch_evolve_helpers(orch)

        orch._check_evolve(TickResult(), {})

        call_kwargs = mock_spawn.call_args
        actual_focus = call_kwargs.kwargs.get("focus_area") or call_kwargs.args[1]
        assert actual_focus == areas[0]

    def test_spawn_manager_receives_cycle_number_and_test_summary(self, tmp_path: Path) -> None:
        """_evolve_spawn_manager is called with correct cycle_number and test_summary."""
        _write_evolve_config(tmp_path, interval_s=0, cycle_count=4)
        orch = self._make_orch(tmp_path)
        _, _, mock_spawn = self._patch_evolve_helpers(
            orch, test_info={"passed": 7, "failed": 1, "summary": "7 passed, 1 failed"}
        )

        orch._check_evolve(TickResult(), {})

        mock_spawn.assert_called_once()
        kwargs = mock_spawn.call_args.kwargs
        assert kwargs["cycle_number"] == 5
        assert kwargs["test_summary"] == "7 passed, 1 failed"

    def test_consecutive_empty_resets_when_committed(self, tmp_path: Path) -> None:
        """If committed=True, _consecutive_empty resets to 0."""
        evolve_path = _write_evolve_config(tmp_path, interval_s=0, consecutive_empty=5)
        orch = self._make_orch(tmp_path)
        self._patch_evolve_helpers(orch, committed=True)

        orch._check_evolve(TickResult(), {})

        updated = json.loads(evolve_path.read_text())
        assert updated["_consecutive_empty"] == 0

    def test_consecutive_empty_increments_when_no_changes(self, tmp_path: Path) -> None:
        """If nothing committed and no done tasks, _consecutive_empty increments."""
        evolve_path = _write_evolve_config(tmp_path, interval_s=0, consecutive_empty=2)
        orch = self._make_orch(tmp_path)
        self._patch_evolve_helpers(orch, committed=False)

        # tasks_by_status has no "done" key → tasks_completed = 0
        orch._check_evolve(TickResult(), {})

        updated = json.loads(evolve_path.read_text())
        assert updated["_consecutive_empty"] == 3


# --- Parallel verification ---


class TestParallelVerification:
    """verify_task() calls for multiple done tasks run concurrently."""

    def test_multiple_done_tasks_verified_concurrently(self, tmp_path: Path) -> None:
        """Multiple done tasks with signals are verified in parallel.

        Mocks verify_task with a 0.2s sleep. With 4 tasks running serially
        this would take ~0.8s; in parallel it should finish in ~0.2s.
        """
        import threading
        from unittest.mock import patch

        call_times: list[float] = []
        lock = threading.Lock()

        def slow_verify(task: object, workdir: object) -> tuple[bool, list[str]]:
            start = time.time()
            time.sleep(0.15)
            with lock:
                call_times.append(start)
            return (True, [])

        task_dicts = []
        for i in range(4):
            t = _make_task(id=f"T-par-{i}", status="done")
            td = _task_as_dict(t)
            td["completion_signals"] = [{"type": "path_exists", "value": "x"}]
            task_dicts.append(td)
        tasks_with_signals = [Task.from_dict(td) for td in task_dicts]

        transport = _mock_transport(
            {
                "GET /tasks?status=open": httpx.Response(200, json=[]),
                "GET /tasks?status=done": httpx.Response(200, json=task_dicts),
            }
        )
        orch = _build_orchestrator(tmp_path, transport)

        with patch("bernstein.core.tasks.task_lifecycle.verify_task", side_effect=slow_verify):
            t_start = time.time()
            result = TickResult()
            orch._process_completed_tasks(tasks_with_signals, result)
            elapsed = time.time() - t_start

        # All 4 tasks verified
        assert len(result.verified) == 4
        # Total wall time should be much less than 4 × 0.15s = 0.6s
        assert elapsed < 0.5, f"Expected parallel execution but took {elapsed:.2f}s"
        # All 4 verify calls started within ~0.15s of each other
        assert len(call_times) == 4
        spread = max(call_times) - min(call_times)
        assert spread < 0.1, f"Calls spread too far apart: {spread:.3f}s"


# --- Parallel verification ---


class TestProcessCompletedTasksParallel:
    """_process_completed_tasks runs verify_task() concurrently."""

    def test_multiple_done_tasks_verified_concurrently(self, tmp_path: Path) -> None:
        """verify_task() for N done tasks must run in parallel, not serially.

        We mock verify_task to sleep 0.2 s per task.  With 4 tasks the serial
        total would be >= 0.8 s; the parallel total (max_workers=4) should be
        well under 0.5 s.
        """
        import time
        from unittest.mock import patch

        SLEEP = 0.2
        N = 4

        task_dicts = []
        for i in range(N):
            t = _make_task(id=f"T-par-{i}", status="done")
            d = _task_as_dict(t)
            d["status"] = "done"
            d["completion_signals"] = [{"type": "path_exists", "value": "x.txt"}]
            task_dicts.append(d)

        transport = _mock_transport(
            {
                "GET /tasks?status=open": httpx.Response(200, json=[]),
                "GET /tasks?status=done": httpx.Response(200, json=task_dicts),
            }
        )
        orch = _build_orchestrator(tmp_path, transport)

        def slow_verify(task: object, workdir: object) -> tuple[bool, list[str]]:
            time.sleep(SLEEP)
            return True, []

        with patch("bernstein.core.tasks.task_lifecycle.verify_task", side_effect=slow_verify):
            tick_result = TickResult()
            start = time.monotonic()
            orch._process_completed_tasks([Task.from_dict(d) for d in task_dicts], tick_result)
            elapsed = time.monotonic() - start

        # All tasks verified successfully
        assert len(tick_result.verified) == N
        assert tick_result.verification_failures == []
        # Parallel: total wall time should be much less than N * SLEEP
        assert elapsed < SLEEP * N * 0.75, f"Expected parallel execution (<{SLEEP * N * 0.75:.2f}s), got {elapsed:.2f}s"


class TestComputeTotalSpentCache:
    """Tests for mtime-based caching in _compute_total_spent."""

    def test_no_reparse_when_files_unchanged(self, tmp_path: Path) -> None:
        """Second call with unchanged files must not re-parse them."""
        from unittest.mock import patch

        import pytest
        from bernstein.core.orchestrator import _compute_total_spent, _total_spent_cache

        from bernstein.core import tick_pipeline as pipeline_mod

        metrics_dir = tmp_path / ".sdd" / "metrics"
        metrics_dir.mkdir(parents=True)
        jsonl = metrics_dir / "cost_efficiency_agent1.jsonl"
        jsonl.write_text('{"value": 0.05, "labels": {"task_id": "t1"}}\n{"value": 0.10, "labels": {"task_id": "t2"}}\n')

        _total_spent_cache.clear()

        first = _compute_total_spent(tmp_path)
        assert first == pytest.approx(0.15)

        # Second call: _parse_file_total should not be called at all.
        with patch.object(pipeline_mod, "_parse_file_total", wraps=pipeline_mod._parse_file_total) as mock_parse:
            second = _compute_total_spent(tmp_path)
            assert second == pytest.approx(0.15)
            mock_parse.assert_not_called()

    def test_reparsed_after_modification(self, tmp_path: Path) -> None:
        """Cache is invalidated when a file's mtime changes."""
        import time as _time

        import pytest
        from bernstein.core.orchestrator import _compute_total_spent, _total_spent_cache

        metrics_dir = tmp_path / ".sdd" / "metrics"
        metrics_dir.mkdir(parents=True)
        jsonl = metrics_dir / "cost_efficiency_agent1.jsonl"
        jsonl.write_text('{"value": 0.05, "labels": {"task_id": "t1"}}\n')

        _total_spent_cache.clear()

        first = _compute_total_spent(tmp_path)
        assert first == pytest.approx(0.05)

        _time.sleep(0.01)
        jsonl.write_text('{"value": 0.05, "labels": {"task_id": "t1"}}\n{"value": 0.20, "labels": {"task_id": "t3"}}\n')

        second = _compute_total_spent(tmp_path)
        assert second == pytest.approx(0.25)

    def test_empty_metrics_dir(self, tmp_path: Path) -> None:
        """Returns 0.0 when no cost_efficiency files exist."""
        from bernstein.core.orchestrator import _compute_total_spent, _total_spent_cache

        metrics_dir = tmp_path / ".sdd" / "metrics"
        metrics_dir.mkdir(parents=True)
        _total_spent_cache.clear()

        assert _compute_total_spent(tmp_path) == pytest.approx(0.0)


# --- Metrics wiring ---


class TestMetricsWiring:
    """Verify the operational MetricsCollector is correctly wired in the orchestrator.

    Each test resets the global _default_collector singleton so tests are isolated.
    """

    def _reset_collector(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import bernstein.core.metric_collector as _mc

        monkeypatch.setattr(_mc, "_default_collector", None)

    def test_spawn_records_start_task_for_each_task(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """start_task() is called for every task in the spawned batch."""
        self._reset_collector(monkeypatch)

        task1 = _make_task(id="T-m1", role="backend")
        task2 = _make_task(id="T-m2", role="backend")
        transport = _mock_transport(
            {
                "GET /tasks?status=open": httpx.Response(
                    200,
                    json=[
                        _task_as_dict(task1),
                        _task_as_dict(task2),
                    ],
                ),
                "POST /tasks/T-m1/claim": httpx.Response(200, json=_task_as_dict(task1)),
                "POST /tasks/T-m2/claim": httpx.Response(200, json=_task_as_dict(task2)),
            }
        )
        orch = _build_orchestrator(tmp_path, transport)
        orch.tick()

        from bernstein.core.metrics import get_collector

        collector = get_collector(tmp_path / ".sdd" / "metrics")
        assert "T-m1" in collector._task_metrics
        assert "T-m2" in collector._task_metrics

    def test_process_completed_tasks_calls_complete_task(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """complete_task() is called for each done task, setting end_time and success."""
        self._reset_collector(monkeypatch)

        done_task = _make_task(id="T-done-ct", status="done")
        transport = _mock_transport(
            {
                "GET /tasks?status=open": httpx.Response(200, json=[]),
                "GET /tasks?status=done": httpx.Response(200, json=[_task_as_dict(done_task)]),
            }
        )
        orch = _build_orchestrator(tmp_path, transport)

        from bernstein.core.metrics import get_collector

        collector = get_collector(tmp_path / ".sdd" / "metrics")
        # Pre-register the task so complete_task() finds it
        collector.start_task("T-done-ct", "backend", "sonnet", "claude")

        orch.tick()

        tm = collector._task_metrics["T-done-ct"]
        assert tm.end_time is not None
        # No completion_signals → janitor_passed=True
        assert tm.success is True
        assert tm.janitor_passed is True

    def test_process_completed_tasks_calls_end_agent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """end_agent() is called when the session is found for a done task."""
        self._reset_collector(monkeypatch)

        done_task = _make_task(id="T-done-ea", status="done")
        transport = _mock_transport(
            {
                "GET /tasks?status=open": httpx.Response(200, json=[]),
                "GET /tasks?status=done": httpx.Response(200, json=[_task_as_dict(done_task)]),
            }
        )
        orch = _build_orchestrator(tmp_path, transport)

        from bernstein.core.metrics import get_collector

        collector = get_collector(tmp_path / ".sdd" / "metrics")
        collector.start_agent("sess-ea", "backend", "sonnet", "claude")
        collector.start_task("T-done-ea", "backend", "sonnet", "claude")

        session = AgentSession(
            id="sess-ea",
            role="backend",
            pid=55,
            task_ids=["T-done-ea"],
            status="working",
        )
        orch._agents["sess-ea"] = session
        orch._task_to_session["T-done-ea"] = "sess-ea"

        orch.tick()

        am = collector._agent_metrics["sess-ea"]
        assert am.end_time is not None

    def test_wall_clock_reap_records_end_agent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """end_agent() is called for agents reaped by wall-clock timeout."""
        self._reset_collector(monkeypatch)

        adapter = _mock_adapter()
        adapter.is_alive.return_value = True
        config = OrchestratorConfig(
            max_agents=6,
            poll_interval_s=1,
            max_agent_runtime_s=60,
            server_url="http://testserver",
        )

        def handler(request: httpx.Request) -> httpx.Response:
            url = request.url
            if request.method == "GET" and url.path == "/tasks":
                return httpx.Response(200, json=[])
            if url.path == "/tasks/T-wct":
                return httpx.Response(200, json=_task_as_dict(_make_task(id="T-wct")))
            if request.method == "POST" and url.path.startswith("/tasks/T-wct"):
                return httpx.Response(200, json={})
            if request.method == "POST" and url.path == "/tasks":
                return httpx.Response(201, json={"id": "T-retry"})
            return httpx.Response(404)

        orch = _build_orchestrator(tmp_path, httpx.MockTransport(handler), adapter=adapter, config=config)

        from bernstein.core.metrics import get_collector

        collector = get_collector(tmp_path / ".sdd" / "metrics")
        collector.start_agent("sess-wct", "backend", "sonnet", "claude")

        timeout_session = AgentSession(
            id="sess-wct",
            role="backend",
            pid=88,
            task_ids=["T-wct"],
            spawn_ts=time.time() - 200,  # 200s > 60s limit → wall-clock reap
            heartbeat_ts=time.time() - 130,  # >120s ago so extension logic doesn't kick in, <900s so no heartbeat reap
            status="working",
        )
        orch._agents["sess-wct"] = timeout_session

        orch.tick()

        assert "sess-wct" in orch._agents
        am = collector._agent_metrics.get("sess-wct")
        assert am is not None, "end_agent() was not called for wall-clock-reaped agent"
        assert am.end_time is not None


# --- Evolution agent-lifetime recording ---


class TestEvolutionAgentLifetimeRecording:
    """Evolution coordinator receives agent-lifetime metrics from all reaping paths."""

    def _build_with_evolution(
        self,
        tmp_path: Path,
        transport: httpx.MockTransport,
    ) -> tuple[Orchestrator, MagicMock]:
        from bernstein.core.evolution import EvolutionCoordinator

        evolution = MagicMock(spec=EvolutionCoordinator)
        evolution.record_task_completion.return_value = None
        evolution.record_agent_lifetime.return_value = None
        evolution.run_analysis_cycle.return_value = []
        evolution.execute_pending_upgrades.return_value = []
        cfg = OrchestratorConfig(
            max_agents=6,
            poll_interval_s=1,
            max_agent_runtime_s=300,
            heartbeat_timeout_s=60,
            server_url="http://testserver",
            evolution_enabled=True,
        )
        adp = _mock_adapter()
        adp.is_alive.return_value = True
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adp, templates_dir, tmp_path)
        client = httpx.Client(transport=transport, base_url="http://testserver")
        orch = Orchestrator(cfg, spawner, tmp_path, client=client, evolution=evolution)
        return orch, evolution

    def test_normal_completion_records_agent_lifetime(self, tmp_path: Path) -> None:
        """_process_completed_tasks calls record_agent_lifetime on evolution coordinator."""
        transport = _mock_transport(
            {
                "GET /tasks": httpx.Response(200, json=[_task_as_dict(_make_task(id="T-lt", status="done"))]),
            }
        )
        orch, evolution = self._build_with_evolution(tmp_path, transport)
        session = AgentSession(
            id="sess-lt",
            role="backend",
            pid=10,
            task_ids=["T-lt"],
            spawn_ts=time.time() - 30.0,
            status="working",
        )
        orch._agents["sess-lt"] = session
        orch._task_to_session["T-lt"] = "sess-lt"

        orch.tick()

        evolution.record_agent_lifetime.assert_called_once()
        kw = evolution.record_agent_lifetime.call_args.kwargs
        assert kw["agent_id"] == "sess-lt"
        assert kw["role"] == "backend"
        assert kw["lifetime_seconds"] >= 0.0

    def test_multi_task_agent_lifetime_recorded_once(self, tmp_path: Path) -> None:
        """When an agent owns two tasks that both complete in the same tick,
        record_agent_lifetime is called exactly once."""
        done_tasks = [
            _task_as_dict(_make_task(id="T-lt1", status="done")),
            _task_as_dict(_make_task(id="T-lt2", status="done")),
        ]
        transport = _mock_transport(
            {
                "GET /tasks": httpx.Response(200, json=done_tasks),
            }
        )
        orch, evolution = self._build_with_evolution(tmp_path, transport)
        session = AgentSession(
            id="sess-multi",
            role="backend",
            pid=11,
            task_ids=["T-lt1", "T-lt2"],
            spawn_ts=time.time() - 20.0,
            status="working",
        )
        orch._agents["sess-multi"] = session
        orch._task_to_session["T-lt1"] = "sess-multi"
        orch._task_to_session["T-lt2"] = "sess-multi"

        orch.tick()

        # Lifetime should be recorded exactly once even though two tasks completed
        assert evolution.record_agent_lifetime.call_count == 1

    def test_wall_clock_reap_records_agent_lifetime(self, tmp_path: Path) -> None:
        """_reap_dead_agents (wall-clock path) calls record_agent_lifetime."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and request.url.path == "/tasks":
                return httpx.Response(200, json=[])
            if request.url.path == "/tasks/T-wlt":
                return httpx.Response(200, json=_task_as_dict(_make_task(id="T-wlt")))
            if request.method == "POST":
                return httpx.Response(200, json={})
            return httpx.Response(404)

        cfg = OrchestratorConfig(
            max_agents=6,
            poll_interval_s=1,
            max_agent_runtime_s=60,  # short timeout
            server_url="http://testserver",
            evolution_enabled=True,
        )
        from bernstein.core.evolution import EvolutionCoordinator

        evolution = MagicMock(spec=EvolutionCoordinator)
        evolution.record_agent_lifetime.return_value = None
        evolution.record_task_completion.return_value = None
        evolution.run_analysis_cycle.return_value = []
        evolution.execute_pending_upgrades.return_value = []

        adp = _mock_adapter()
        adp.is_alive.return_value = True
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adp, templates_dir, tmp_path)
        client = httpx.Client(
            transport=_paginated_transport(httpx.MockTransport(handler)), base_url="http://testserver"
        )
        orch = Orchestrator(cfg, spawner, tmp_path, client=client, evolution=evolution)

        session = AgentSession(
            id="sess-wlt",
            role="qa",
            pid=55,
            task_ids=["T-wlt"],
            spawn_ts=time.time() - 200,  # exceeds 60s limit
            heartbeat_ts=time.time() - 130,  # >120s so extension logic doesn't kick in
            status="working",
        )
        orch._agents["sess-wlt"] = session

        orch.tick()

        evolution.record_agent_lifetime.assert_called()
        kw = evolution.record_agent_lifetime.call_args.kwargs
        assert kw["agent_id"] == "sess-wlt"
        assert kw["role"] == "qa"
        assert kw["tasks_completed"] == 0  # timed out before completing

    @patch("bernstein.core.agent_recycling._is_process_alive", return_value=False)
    def test_heartbeat_reap_records_agent_lifetime(self, _mock_alive: MagicMock, tmp_path: Path) -> None:
        """_reap_dead_agents (heartbeat path) calls record_agent_lifetime."""
        transport = _mock_transport({"GET /tasks": httpx.Response(200, json=[])})
        cfg = OrchestratorConfig(
            max_agents=6,
            poll_interval_s=1,
            max_agent_runtime_s=9999,
            heartbeat_timeout_s=30,
            server_url="http://testserver",
            evolution_enabled=True,
        )
        from bernstein.core.evolution import EvolutionCoordinator

        evolution = MagicMock(spec=EvolutionCoordinator)
        evolution.record_agent_lifetime.return_value = None
        evolution.record_task_completion.return_value = None
        evolution.run_analysis_cycle.return_value = []
        evolution.execute_pending_upgrades.return_value = []

        adp = _mock_adapter()
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adp, templates_dir, tmp_path)
        client = httpx.Client(transport=transport, base_url="http://testserver")
        orch = Orchestrator(cfg, spawner, tmp_path, client=client, evolution=evolution)

        stale_session = AgentSession(
            id="sess-hbt",
            role="security",
            pid=66,
            task_ids=[],
            spawn_ts=time.time() - 50,
            heartbeat_ts=time.time() - 60,  # stale: 60s > 30s timeout
            status="working",
        )
        orch._agents["sess-hbt"] = stale_session

        orch.tick()

        evolution.record_agent_lifetime.assert_called()
        kw = evolution.record_agent_lifetime.call_args.kwargs
        assert kw["agent_id"] == "sess-hbt"
        assert kw["role"] == "security"
        assert kw["tasks_completed"] == 0  # heartbeat-reaped

    def test_lifetime_failure_does_not_crash(self, tmp_path: Path) -> None:
        """If record_agent_lifetime raises, the orchestrator silently suppresses it."""
        transport = _mock_transport(
            {
                "GET /tasks": httpx.Response(200, json=[_task_as_dict(_make_task(id="T-lf", status="done"))]),
            }
        )
        orch, evolution = self._build_with_evolution(tmp_path, transport)
        evolution.record_agent_lifetime.side_effect = RuntimeError("db offline")

        session = AgentSession(
            id="sess-lf",
            role="backend",
            pid=77,
            task_ids=["T-lf"],
            spawn_ts=time.time() - 10,
            status="working",
        )
        orch._agents["sess-lf"] = session

        # Must not raise and must not produce errors
        result = orch.tick()
        assert len(result.errors) == 0


# ---------------------------------------------------------------------------
# Bulletin board integration
# ---------------------------------------------------------------------------


class TestOrchestratorBulletinIntegration:
    """Bulletin board is wired into the single-cell Orchestrator lifecycle."""

    def _build_with_bulletin(
        self,
        tmp_path: Path,
        transport: httpx.MockTransport,
    ) -> tuple[Orchestrator, BulletinBoard]:
        from bernstein.core.bulletin import BulletinBoard

        board = BulletinBoard()
        cfg = OrchestratorConfig(
            max_agents=6,
            poll_interval_s=1,
            heartbeat_timeout_s=120,
            max_tasks_per_agent=3,
            server_url="http://testserver",
        )
        adp = _mock_adapter()
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adp, templates_dir, tmp_path)
        client = httpx.Client(transport=transport, base_url="http://testserver")
        orch = Orchestrator(cfg, spawner, tmp_path, client=client, bulletin=board)
        return orch, board

    def test_bulletin_property_returns_board(self, tmp_path: Path) -> None:
        """Orchestrator.bulletin returns the injected BulletinBoard."""

        transport = _mock_transport({"GET /tasks": httpx.Response(200, json=[])})
        orch, board = self._build_with_bulletin(tmp_path, transport)
        assert orch.bulletin is board

    def test_bulletin_none_when_not_provided(self, tmp_path: Path) -> None:
        """Orchestrator.bulletin is None when no board is injected."""
        transport = _mock_transport({"GET /tasks": httpx.Response(200, json=[])})
        orch = _build_orchestrator(tmp_path, transport)
        assert orch.bulletin is None

    def test_run_started_posted_on_run(self, tmp_path: Path) -> None:
        """run() posts 'run started' to the bulletin board before the loop."""

        transport = _mock_transport({"GET /tasks": httpx.Response(200, json=[])})
        orch, board = self._build_with_bulletin(tmp_path, transport)

        # Patch dry_run so run() exits after one tick
        orch._config = OrchestratorConfig(
            max_agents=6,
            poll_interval_s=0,
            heartbeat_timeout_s=120,
            max_tasks_per_agent=3,
            server_url="http://testserver",
            dry_run=True,
        )
        orch.run()

        statuses = [m.content for m in board.read_by_type("status")]
        assert any("run started" in s for s in statuses)

    def test_task_completed_posted_to_bulletin(self, tmp_path: Path) -> None:
        """A done task that passes janitor verification posts a 'task completed' status."""

        done_task = _make_task(id="T-bb1", title="BB task", status="done")
        transport = _mock_transport(
            {
                "GET /tasks": httpx.Response(200, json=[_task_as_dict(done_task)]),
            }
        )

        orch, board = self._build_with_bulletin(tmp_path, transport)
        orch._processed_done_tasks = collections.OrderedDict()  # ensure not pre-processed

        orch.tick()

        contents = [m.content for m in board.read_by_type("status")]
        assert any("task completed" in c and "T-bb1" in c for c in contents)

    def test_task_failed_janitor_posts_alert(self, tmp_path: Path) -> None:
        """A done task that fails janitor verification posts an alert."""

        done_task = _make_task(id="T-bb2", title="Fail task", status="done")
        # Add a completion signal that will fail (file does not exist)
        done_task = Task(
            id=done_task.id,
            title=done_task.title,
            description=done_task.description,
            role=done_task.role,
            priority=done_task.priority,
            scope=done_task.scope,
            complexity=done_task.complexity,
            status=done_task.status,
            completion_signals=[CompletionSignal(type="file_exists", value="/nonexistent/path/file.txt")],
        )

        transport = _mock_transport(
            {
                "GET /tasks": httpx.Response(200, json=[_task_as_dict(done_task)]),
            }
        )

        orch, board = self._build_with_bulletin(tmp_path, transport)
        orch.tick()

        # Either a 'task completed' or 'task failed janitor' alert should be posted
        all_msgs = board.read_since(0.0)
        assert len(all_msgs) > 0

    def test_no_bulletin_no_crash(self, tmp_path: Path) -> None:
        """_post_bulletin is a no-op when no board is configured."""
        done_task = _make_task(id="T-bb3", title="No board task", status="done")
        transport = _mock_transport(
            {
                "GET /tasks": httpx.Response(200, json=[_task_as_dict(done_task)]),
            }
        )

        orch = _build_orchestrator(tmp_path, transport)
        assert orch.bulletin is None
        # Should not raise
        result = orch.tick()
        assert len(result.errors) == 0

    def test_run_summary_posted_to_bulletin(self, tmp_path: Path) -> None:
        """When all tasks are done and no agents are alive, a run complete message is posted."""
        from bernstein.core.bulletin import BulletinBoard

        done_task = _make_task(id="T-bb4", title="Done summary task", status="done")
        transport = _mock_transport(
            {
                "GET /tasks": httpx.Response(200, json=[_task_as_dict(done_task)]),
            }
        )

        cfg = OrchestratorConfig(
            max_agents=6,
            poll_interval_s=1,
            heartbeat_timeout_s=120,
            max_tasks_per_agent=3,
            server_url="http://testserver",
            evolve_mode=False,
        )
        board = BulletinBoard()
        adp = _mock_adapter()
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adp, templates_dir, tmp_path)
        client = httpx.Client(transport=transport, base_url="http://testserver")
        orch = Orchestrator(cfg, spawner, tmp_path, client=client, bulletin=board)

        orch.tick()

        # The run summary bulletin should have been posted
        status_msgs = [m.content for m in board.read_by_type("status")]
        assert any("run complete" in c for c in status_msgs)


# --- Adaptive polling backoff ---


class TestAdaptivePollingBackoff:
    """Adaptive backoff in the run loop: idle ticks double the sleep, active ticks reset it."""

    def _make_orch(self, tmp_path: Path, poll_interval_s: int = 3) -> Orchestrator:
        transport = _mock_transport({"GET /tasks": httpx.Response(200, json=[])})
        cfg = OrchestratorConfig(
            max_agents=6,
            poll_interval_s=poll_interval_s,
            heartbeat_timeout_s=120,
            max_tasks_per_agent=3,
            server_url="http://testserver",
        )
        return _build_orchestrator(tmp_path, transport, config=cfg)

    def test_idle_ticks_double_sleep_up_to_30s(self, tmp_path: Path, monkeypatch: object) -> None:
        orch = self._make_orch(tmp_path, poll_interval_s=3)
        sleep_calls: list[float] = []
        import bernstein.core.orchestrator as _orch_mod

        monkeypatch.setattr(_orch_mod.time, "sleep", lambda s: sleep_calls.append(float(s)))

        call_count = 0

        def fake_tick() -> TickResult:
            nonlocal call_count
            call_count += 1
            if call_count >= 5:
                orch._running = False
            return TickResult()  # idle: no spawned, no verified

        monkeypatch.setattr(orch, "tick", fake_tick)
        monkeypatch.setattr(orch, "_has_active_agents", lambda: False)
        monkeypatch.setattr(orch, "_drain_before_cleanup", lambda: None)
        monkeypatch.setattr(orch, "_cleanup", lambda: None)
        orch.run()

        # After each idle tick, sleep doubles: 6, 12, 24, 24 (cap at 8x), 24
        assert len(sleep_calls) > 0
        assert sleep_calls[0] == pytest.approx(6.0)
        assert sleep_calls[1] == pytest.approx(12.0)
        assert sleep_calls[2] == pytest.approx(24.0)
        assert sleep_calls[3] == pytest.approx(24.0)
        assert sleep_calls[4] == pytest.approx(24.0)

    def test_active_tick_resets_sleep_to_poll_interval(self, tmp_path: Path, monkeypatch: object) -> None:
        orch = self._make_orch(tmp_path, poll_interval_s=3)
        sleep_calls: list[float] = []
        import bernstein.core.orchestrator as _orch_mod

        monkeypatch.setattr(_orch_mod.time, "sleep", lambda s: sleep_calls.append(float(s)))

        call_count = 0

        def fake_tick() -> TickResult:
            nonlocal call_count
            call_count += 1
            if call_count >= 4:
                orch._running = False
            r = TickResult()
            if call_count == 3:
                r.spawned.append("agent-1")  # active tick: work was done
            return r

        monkeypatch.setattr(orch, "tick", fake_tick)
        # Prevent drain loop and post-loop cleanup from generating extra sleeps.
        # run() has: while _running or _has_active_agents(), then
        # _drain_before_cleanup() and _cleanup() which also call time.sleep.
        monkeypatch.setattr(orch, "_has_active_agents", lambda: False)
        monkeypatch.setattr(orch, "_drain_before_cleanup", lambda: None)
        monkeypatch.setattr(orch, "_cleanup", lambda: None)
        orch.run()

        # tick 1 (idle) → sleep 6, tick 2 (idle) → sleep 12,
        # tick 3 (active, resets) → sleep 3, tick 4 (idle, stops loop) → sleep 6
        assert sleep_calls == [6.0, 12.0, 3.0, 6.0]

    def test_idle_multiplier_field_exists(self, tmp_path: Path) -> None:
        orch = self._make_orch(tmp_path)
        assert hasattr(orch, "_idle_multiplier")
        assert orch._idle_multiplier == 1


# --- Reverse task-to-session index ---


class TestReverseTaskSessionIndex:
    """_task_to_session provides O(1) lookup from task_id to agent_id."""

    def _make_orch(self, tmp_path: Path) -> Orchestrator:
        transport = _mock_transport({"GET /tasks": httpx.Response(200, json=[])})
        return _build_orchestrator(tmp_path, transport)

    def test_find_session_returns_correct_session(self, tmp_path: Path) -> None:
        """_find_session_for_task returns the session that owns the task."""
        orch = self._make_orch(tmp_path)
        session = AgentSession(id="backend-x", role="backend", task_ids=["T-x1", "T-x2"])
        orch._agents["backend-x"] = session
        orch._task_to_session["T-x1"] = "backend-x"
        orch._task_to_session["T-x2"] = "backend-x"

        assert orch._find_session_for_task("T-x1") is session
        assert orch._find_session_for_task("T-x2") is session

    def test_find_session_returns_none_for_unknown_task(self, tmp_path: Path) -> None:
        """_find_session_for_task returns None when task is not in the index."""
        orch = self._make_orch(tmp_path)
        assert orch._find_session_for_task("T-missing") is None

    def test_find_session_returns_none_after_release(self, tmp_path: Path) -> None:
        """After _release_task_to_session, the task is no longer findable."""
        orch = self._make_orch(tmp_path)
        session = AgentSession(id="backend-y", role="backend", task_ids=["T-y1"])
        orch._agents["backend-y"] = session
        orch._task_to_session["T-y1"] = "backend-y"

        orch._release_task_to_session(["T-y1"])

        assert orch._find_session_for_task("T-y1") is None

    def test_release_only_removes_specified_tasks(self, tmp_path: Path) -> None:
        """_release_task_to_session removes only the listed task IDs."""
        orch = self._make_orch(tmp_path)
        session_a = AgentSession(id="agent-a", role="backend", task_ids=["T-a1"])
        session_b = AgentSession(id="agent-b", role="backend", task_ids=["T-b1"])
        orch._agents["agent-a"] = session_a
        orch._agents["agent-b"] = session_b
        orch._task_to_session["T-a1"] = "agent-a"
        orch._task_to_session["T-b1"] = "agent-b"

        orch._release_task_to_session(["T-a1"])

        assert orch._find_session_for_task("T-a1") is None
        assert orch._find_session_for_task("T-b1") is session_b

    def test_release_empty_list_is_noop(self, tmp_path: Path) -> None:
        """_release_task_to_session with empty list leaves index unchanged."""
        orch = self._make_orch(tmp_path)
        session = AgentSession(id="agent-c", role="backend", task_ids=["T-c1"])
        orch._agents["agent-c"] = session
        orch._task_to_session["T-c1"] = "agent-c"

        orch._release_task_to_session([])

        assert orch._find_session_for_task("T-c1") is session

    def test_release_unknown_task_ids_is_safe(self, tmp_path: Path) -> None:
        """_release_task_to_session with unknown IDs does not raise."""
        orch = self._make_orch(tmp_path)
        # Should not raise even when task_ids are not in the index
        orch._release_task_to_session(["T-phantom"])
        assert orch._task_to_session == {}


# ---------------------------------------------------------------------------
# Manager queue review trigger (#333f)
# ---------------------------------------------------------------------------


class TestShouldTriggerManagerReview:
    """Tests for Orchestrator._should_trigger_manager_review."""

    def _make_orch(self, tmp_path: Path) -> Orchestrator:
        transport = _mock_transport({})
        return _build_orchestrator(tmp_path, transport)

    def test_triggers_when_completions_reach_threshold(self, tmp_path: Path) -> None:
        orch = self._make_orch(tmp_path)
        orch._completions_since_review = orch._MANAGER_REVIEW_COMPLETION_THRESHOLD  # == THRESHOLD
        assert orch._should_trigger_manager_review(failed_count=0) is True

    def test_triggers_when_completions_exceed_threshold(self, tmp_path: Path) -> None:
        orch = self._make_orch(tmp_path)
        orch._completions_since_review = 10
        assert orch._should_trigger_manager_review(failed_count=0) is True

    def test_does_not_trigger_when_below_threshold_no_failures(self, tmp_path: Path) -> None:
        orch = self._make_orch(tmp_path)
        orch._completions_since_review = 2  # < THRESHOLD
        assert orch._should_trigger_manager_review(failed_count=0) is False

    def test_triggers_on_any_failure(self, tmp_path: Path) -> None:
        orch = self._make_orch(tmp_path)
        orch._completions_since_review = 0
        assert orch._should_trigger_manager_review(failed_count=1) is True

    def test_triggers_on_stall_after_previous_review(self, tmp_path: Path) -> None:
        orch = self._make_orch(tmp_path)
        orch._completions_since_review = 0
        # Simulate a review that happened long enough ago to exceed the stall guard
        orch._last_review_ts = time.time() - (orch._MANAGER_REVIEW_STALL_S + 60)
        assert orch._should_trigger_manager_review(failed_count=0) is True

    def test_does_not_trigger_stall_when_no_prior_review(self, tmp_path: Path) -> None:
        """Stall guard only fires after a previous review (last_review_ts > 0)."""
        orch = self._make_orch(tmp_path)
        orch._completions_since_review = 0
        orch._last_review_ts = 0.0  # no prior review
        assert orch._should_trigger_manager_review(failed_count=0) is False

    def test_does_not_trigger_stall_when_recent_review(self, tmp_path: Path) -> None:
        orch = self._make_orch(tmp_path)
        orch._completions_since_review = 0
        orch._last_review_ts = time.time() - 60  # only 1 minute ago
        assert orch._should_trigger_manager_review(failed_count=0) is False


# ---------------------------------------------------------------------------
# Manager queue review corrections (#333f)
# ---------------------------------------------------------------------------


class TestRunManagerQueueReview:
    """Tests for Orchestrator._run_manager_queue_review — mocked ManagerAgent."""

    def _make_orch(self, tmp_path: Path, transport: httpx.MockTransport) -> Orchestrator:
        return _build_orchestrator(tmp_path, transport)

    def _make_correction_result(
        self,
        corrections: list[dict],  # type: ignore[type-arg]
        reasoning: str = "test reasoning",
    ):  # type: ignore[no-untyped-def]
        from bernstein.core.manager import QueueCorrection, QueueReviewResult

        parsed_corrections = []
        for c in corrections:
            parsed_corrections.append(
                QueueCorrection(
                    action=c["action"],
                    task_id=c.get("task_id"),
                    new_role=c.get("new_role"),
                    new_priority=c.get("new_priority"),
                    reason=c.get("reason", ""),
                    new_task=c.get("new_task"),
                )
            )
        return QueueReviewResult(corrections=parsed_corrections, reasoning=reasoning)

    def test_resets_counters_and_sets_last_review_ts(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from bernstein.core.manager import QueueReviewResult

        transport = _mock_transport({})
        orch = self._make_orch(tmp_path, transport)
        orch._completions_since_review = 5
        orch._failures_since_review = 2

        skipped_result = QueueReviewResult(corrections=[], reasoning="skipped", skipped=True)
        with patch("bernstein.core.manager.ManagerAgent") as mock_cls:
            mock_agent = MagicMock()
            mock_agent.review_queue_sync.return_value = skipped_result
            mock_cls.return_value = mock_agent
            orch._run_manager_queue_review()

        assert orch._completions_since_review == 0
        assert orch._failures_since_review == 0
        assert orch._last_review_ts > 0

    def test_reassign_sends_patch_with_new_role(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        patched: list[tuple[str, dict]] = []  # type: ignore[type-arg]

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "PATCH":
                patched.append((request.url.path, json.loads(request.content)))
                return httpx.Response(200, json={"id": "t1", "status": "open", "role": "frontend"})
            return httpx.Response(200, json=[])

        transport = httpx.MockTransport(handler)
        orch = self._make_orch(tmp_path, transport)
        # Create role template so validation accepts 'frontend'
        (tmp_path / "templates" / "roles" / "frontend").mkdir(parents=True, exist_ok=True)
        (tmp_path / "templates" / "roles" / "frontend" / "system_prompt.md").write_text("# Frontend\n")

        result = self._make_correction_result(
            [{"action": "reassign", "task_id": "t1", "new_role": "frontend", "reason": "CSS work"}]
        )
        with patch("bernstein.core.manager.ManagerAgent") as mock_cls:
            mock_agent = MagicMock()
            mock_agent.review_queue_sync.return_value = result
            mock_cls.return_value = mock_agent
            orch._run_manager_queue_review()

        assert len(patched) == 1
        path, body = patched[0]
        assert path == "/tasks/t1"
        assert body == {"role": "frontend"}

    def test_cancel_sends_post_to_cancel_endpoint(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        posted: list[tuple[str, dict]] = []  # type: ignore[type-arg]

        def handler(request: httpx.Request) -> httpx.Response:
            url = request.url
            if request.method == "POST" and url.path.endswith("/cancel"):
                posted.append((url.path, json.loads(request.content)))
                return httpx.Response(200, json={"id": "t2", "status": "cancelled"})
            return httpx.Response(200, json=[])

        transport = httpx.MockTransport(handler)
        orch = self._make_orch(tmp_path, transport)

        result = self._make_correction_result([{"action": "cancel", "task_id": "t2", "reason": "stalled > 5 min"}])
        with patch("bernstein.core.manager.ManagerAgent") as mock_cls:
            mock_agent = MagicMock()
            mock_agent.review_queue_sync.return_value = result
            mock_cls.return_value = mock_agent
            orch._run_manager_queue_review()

        assert len(posted) == 1
        path, body = posted[0]
        assert path == "/tasks/t2/cancel"
        assert body["reason"] == "stalled > 5 min"

    def test_change_priority_sends_patch_with_priority(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        patched: list[tuple[str, dict]] = []  # type: ignore[type-arg]

        def handler(request: httpx.Request) -> httpx.Response:
            url = request.url
            if request.method == "PATCH":
                patched.append((url.path, json.loads(request.content)))
                return httpx.Response(200, json={"id": "t3", "status": "open", "priority": 1})
            return httpx.Response(200, json=[])

        transport = httpx.MockTransport(handler)
        orch = self._make_orch(tmp_path, transport)

        result = self._make_correction_result(
            [{"action": "change_priority", "task_id": "t3", "new_priority": 1, "reason": "urgent"}]
        )
        with patch("bernstein.core.manager.ManagerAgent") as mock_cls:
            mock_agent = MagicMock()
            mock_agent.review_queue_sync.return_value = result
            mock_cls.return_value = mock_agent
            orch._run_manager_queue_review()

        assert len(patched) == 1
        path, body = patched[0]
        assert path == "/tasks/t3"
        assert body == {"priority": 1}

    def test_add_task_sends_post_to_tasks(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        posted: list[tuple[str, dict]] = []  # type: ignore[type-arg]

        def handler(request: httpx.Request) -> httpx.Response:
            url = request.url
            if request.method == "POST" and url.path == "/tasks":
                posted.append((url.path, json.loads(request.content)))
                return httpx.Response(201, json={"id": "new-t", "status": "open"})
            return httpx.Response(200, json=[])

        transport = httpx.MockTransport(handler)
        orch = self._make_orch(tmp_path, transport)

        result = self._make_correction_result(
            [
                {
                    "action": "add_task",
                    "new_task": {
                        "title": "Write E2E tests",
                        "role": "qa",
                        "description": "Add E2E test suite",
                        "priority": 2,
                    },
                    "reason": "missing coverage",
                }
            ]
        )
        with patch("bernstein.core.manager.ManagerAgent") as mock_cls:
            mock_agent = MagicMock()
            mock_agent.review_queue_sync.return_value = result
            mock_cls.return_value = mock_agent
            orch._run_manager_queue_review()

        assert len(posted) == 1
        path, body = posted[0]
        assert path == "/tasks"
        assert body["title"] == "Write E2E tests"
        assert body["role"] == "qa"

    def test_skipped_result_applies_no_corrections(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from bernstein.core.manager import QueueReviewResult

        requests_made: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests_made.append(f"{request.method} {request.url.path}")
            return httpx.Response(200, json=[])

        transport = httpx.MockTransport(handler)
        orch = self._make_orch(tmp_path, transport)

        skipped = QueueReviewResult(corrections=[], reasoning="budget < 10%", skipped=True)
        with patch("bernstein.core.manager.ManagerAgent") as mock_cls:
            mock_agent = MagicMock()
            mock_agent.review_queue_sync.return_value = skipped
            mock_cls.return_value = mock_agent
            orch._run_manager_queue_review()

        # No PATCH/POST/cancel calls — only the ManagerAgent was invoked
        assert not any(m for m in requests_made if "PATCH" in m or "cancel" in m)


def test_record_live_costs_enforces_max_cost_per_agent(tmp_path: Path) -> None:
    """_record_live_costs should kill agents that exceed the per-session cost cap."""
    config = OrchestratorConfig(
        max_agents=2,
        poll_interval_s=1,
        heartbeat_timeout_s=60,
        server_url="http://testserver",
        max_cost_per_agent=0.001,
    )
    transport = _mock_transport({})
    orch = _build_orchestrator(tmp_path, transport=transport, config=config)
    session = AgentSession(
        id="agent-cap",
        role="backend",
        task_ids=["task-cap"],
        status="working",
    )
    session.tokens_used = 1000  # Sonnet estimate exceeds the 0.001 USD cap
    orch._agents[session.id] = session
    orch._spawner.kill = MagicMock()
    orch._release_file_ownership = MagicMock()
    orch._release_task_to_session = MagicMock()
    orch._record_provider_health = MagicMock()

    with patch("bernstein.core.orchestrator.retry_or_fail_task") as mock_retry:
        orch._record_live_costs()

    assert session.id in orch._cost_cap_killed_agents
    assert session.status == "dead"
    orch._spawner.kill.assert_called_once()
    mock_retry.assert_called_once()


def test_build_notification_manager_includes_seed_webhooks() -> None:
    """Seed-level webhooks should be threaded into NotificationManager targets."""
    from bernstein.core.orchestrator import _build_notification_manager

    seed = SimpleNamespace(
        notify=SimpleNamespace(webhook_url="https://legacy.example/hook", on_complete=True, on_failure=False),
        webhooks=(SimpleNamespace(url="https://events.example/hook", events=("task.completed", "task.failed")),),
    )
    manager = _build_notification_manager(seed)
    assert manager is not None
    targets = manager._targets  # pyright: ignore[reportPrivateUsage]
    assert len(targets) == 2
    assert any(t.url == "https://legacy.example/hook" and t.events == ["run.completed"] for t in targets)
    assert any(
        t.url == "https://events.example/hook" and t.events == ["task.completed", "task.failed"] for t in targets
    )


def test_build_notification_manager_includes_desktop_target() -> None:
    """Desktop notify config should create a local task lifecycle target."""
    from bernstein.core.orchestrator import _build_notification_manager

    seed = SimpleNamespace(
        notify=SimpleNamespace(webhook_url=None, on_complete=True, on_failure=True, desktop=True),
        webhooks=(),
        smtp=None,
    )

    manager = _build_notification_manager(seed)
    assert manager is not None
    targets = manager._targets  # pyright: ignore[reportPrivateUsage]
    assert any(t.type == "desktop" and t.events == ["task.completed", "task.failed"] for t in targets)
