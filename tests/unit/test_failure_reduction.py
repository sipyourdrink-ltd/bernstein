"""Tests for agent failure rate reduction — task #738.

Covers:
- Progressive timeout: estimated_minutes grows on each retry
- Opus/max routing for large-scope tasks on retry
- Auto-decompose triggered for tasks that have failed 2+ times
- File context discovery when owned_files is empty
- _maybe_retry_task progressive timeout and high-stakes escalation
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import httpx
from bernstein.core.context import TaskContextBuilder
from bernstein.core.models import (
    Complexity,
    OrchestratorConfig,
    Scope,
    Task,
    TaskStatus,
)
from bernstein.core.orchestrator import Orchestrator
from bernstein.core.spawner import AgentSpawner

from bernstein.adapters.base import CLIAdapter, SpawnResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
    estimated_minutes: int = 30,
    model: str | None = None,
    effort: str | None = None,
    retry_count: int = 0,
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
        estimated_minutes=estimated_minutes,
        model=model,
        effort=effort,
        retry_count=retry_count,
    )


def _mock_adapter(pid: int = 42) -> MagicMock:
    adapter = MagicMock(spec=CLIAdapter)
    adapter.spawn.return_value = SpawnResult(pid=pid, log_path=Path("/tmp/test.log"))
    adapter.is_alive.return_value = True
    adapter.is_rate_limited.return_value = False
    adapter.kill.return_value = None
    return adapter


def _build_orchestrator(
    tmp_path: Path,
    task: Task,
    *,
    max_retries: int = 2,
) -> tuple[Orchestrator, list[dict]]:
    """Return (orchestrator, captured_post_bodies) with task-specific mock transport."""
    posted: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == f"/tasks/{task.id}":
            raw: dict = {
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
                "model": task.model,
                "effort": task.effort,
                # audit-017: typed retry fields must ride along with the
                # serialised task so retry_or_fail_task sees the real counter.
                "retry_count": task.retry_count,
                "max_retries": task.max_retries,
                "retry_delay_s": task.retry_delay_s,
                "terminal_reason": task.terminal_reason,
            }
            return httpx.Response(200, json=raw)
        if request.method == "POST" and path == "/tasks":
            posted.append(json.loads(request.content))
            return httpx.Response(201, json={"id": "NEW-001"})
        if request.method == "POST" and path.endswith("/fail"):
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"detail": f"No mock for {request.method} {path}"})

    transport = httpx.MockTransport(handler)
    cfg = OrchestratorConfig(
        server_url="http://testserver",
        max_task_retries=max_retries,
    )
    adp = _mock_adapter()
    templates_dir = tmp_path / "templates" / "roles"
    templates_dir.mkdir(parents=True)
    spawner = AgentSpawner(adp, templates_dir, tmp_path)
    client = httpx.Client(transport=transport, base_url="http://testserver")
    return Orchestrator(cfg, spawner, tmp_path, client=client), posted


# ---------------------------------------------------------------------------
# Fix A: Progressive timeout — estimated_minutes doubles on each retry
# ---------------------------------------------------------------------------


class TestProgressiveTimeout:
    """_retry_or_fail_task multiplies estimated_minutes by (retry_count + 2)."""

    def test_first_retry_doubles_estimated_minutes(self, tmp_path: Path) -> None:
        task = _make_task(
            id="T-prog-1",
            description="Do the thing.",
            estimated_minutes=30,
        )
        orch, posted = _build_orchestrator(tmp_path, task)

        orch._retry_or_fail_task("T-prog-1", "agent died")

        assert len(posted) == 1
        # retry_count=0, new retry_count=1 → multiplier = 0+2 = 2
        assert posted[0]["estimated_minutes"] == 60  # 30 * 2

    def test_second_retry_triples_estimated_minutes(self, tmp_path: Path) -> None:
        # audit-017: retry_count is the typed source of truth.
        task = _make_task(
            id="T-prog-2",
            description="Do the thing.",
            estimated_minutes=30,
            retry_count=1,
        )
        orch, posted = _build_orchestrator(tmp_path, task)

        orch._retry_or_fail_task("T-prog-2", "agent died again")

        assert len(posted) == 1
        # retry_count=1, new retry_count=2 → multiplier = 1+2 = 3
        assert posted[0]["estimated_minutes"] == 90  # 30 * 3

    def test_progressive_timeout_not_applied_when_max_retries_exceeded(self, tmp_path: Path) -> None:
        task = _make_task(
            id="T-prog-3",
            description="Do the thing.",
            estimated_minutes=30,
            retry_count=2,
        )
        orch, posted = _build_orchestrator(tmp_path, task, max_retries=2)

        orch._retry_or_fail_task("T-prog-3", "agent died yet again")

        # No retry created — max retries hit
        assert posted == []


# ---------------------------------------------------------------------------
# Fix B: Opus/max for large-scope tasks on any retry
# ---------------------------------------------------------------------------


class TestLargeScopeOpusMaxOnRetry:
    """Large-scope tasks always get opus/max on retry regardless of retry count."""

    def test_large_scope_first_retry_uses_opus_max(self, tmp_path: Path) -> None:
        task = _make_task(
            id="T-large-1",
            scope="large",
            description="Do the thing.",
        )
        orch, posted = _build_orchestrator(tmp_path, task)

        orch._retry_or_fail_task("T-large-1", "agent died")

        assert len(posted) == 1
        assert posted[0]["model"] == "opus"
        assert posted[0]["effort"] == "max"

    def test_architect_role_first_retry_uses_opus_max(self, tmp_path: Path) -> None:
        task = _make_task(
            id="T-arch-1",
            role="architect",
            description="Design the system.",
        )
        orch, posted = _build_orchestrator(tmp_path, task)

        orch._retry_or_fail_task("T-arch-1", "agent died")

        assert len(posted) == 1
        assert posted[0]["model"] == "opus"
        assert posted[0]["effort"] == "max"

    def test_security_role_first_retry_uses_opus_max(self, tmp_path: Path) -> None:
        task = _make_task(
            id="T-sec-1",
            role="security",
            description="Audit the auth.",
        )
        orch, posted = _build_orchestrator(tmp_path, task)

        orch._retry_or_fail_task("T-sec-1", "agent died")

        assert len(posted) == 1
        assert posted[0]["model"] == "opus"
        assert posted[0]["effort"] == "max"

    def test_medium_scope_first_retry_does_not_use_max_effort(self, tmp_path: Path) -> None:
        """Medium scope backend task should NOT get opus/max on first retry."""
        task = _make_task(
            id="T-med-1",
            scope="medium",
            role="backend",
            description="Do the thing.",
            model="sonnet",
            effort="high",
        )
        orch, posted = _build_orchestrator(tmp_path, task)

        orch._retry_or_fail_task("T-med-1", "agent died")

        assert len(posted) == 1
        # First retry of medium scope: effort bumped, but not opus/max
        assert posted[0]["effort"] != "max" or posted[0]["model"] != "opus"


# ---------------------------------------------------------------------------
# Fix C: Auto-decompose on 2nd+ retry
# ---------------------------------------------------------------------------


class TestAutoDecomposeOnRepeatedFailure:
    """Tasks that have failed 2+ times should be auto-decomposed."""

    def _build_orch(self, tmp_path: Path) -> Orchestrator:
        cfg = OrchestratorConfig(server_url="http://testserver", auto_decompose=True)
        adp = _mock_adapter()
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adp, templates_dir, tmp_path)
        client = httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
            base_url="http://testserver",
        )
        return Orchestrator(cfg, spawner, tmp_path, client=client)

    def test_retry_2_task_triggers_auto_decompose(self, tmp_path: Path) -> None:
        """A task with [RETRY 2] prefix should be flagged for decomposition."""
        orch = self._build_orch(tmp_path)
        task = _make_task(
            id="T-decomp-1",
            title="[RETRY 2] Build distributed cluster mode",
            scope="medium",  # not large, but has failed twice
        )

        result = orch._should_auto_decompose(task)

        assert result is True

    def test_retry_1_task_does_not_trigger_auto_decompose(self, tmp_path: Path) -> None:
        """A task with [RETRY 1] should NOT be decomposed yet (give it one more try)."""
        orch = self._build_orch(tmp_path)
        task = _make_task(
            id="T-decomp-2",
            title="[RETRY 1] Build distributed cluster mode",
            scope="medium",
        )

        result = orch._should_auto_decompose(task)

        assert result is False

    def test_fresh_task_does_not_trigger_auto_decompose_for_medium_scope(self, tmp_path: Path) -> None:
        """A fresh medium-scope task should not be decomposed."""
        orch = self._build_orch(tmp_path)
        task = _make_task(
            id="T-decomp-3",
            title="Implement login form",
            scope="medium",
        )

        result = orch._should_auto_decompose(task)

        assert result is False

    def test_large_scope_fresh_task_still_decomposes(self, tmp_path: Path) -> None:
        """Large scope fresh task should still trigger decomposition."""
        orch = self._build_orch(tmp_path)
        task = _make_task(
            id="T-decomp-4",
            title="Build full cluster mode",
            scope="large",
        )

        result = orch._should_auto_decompose(task)

        assert result is True

    def test_retry_2_task_already_decomposed_not_decomposed_again(self, tmp_path: Path) -> None:
        """A [RETRY 2] task already decomposed should not be decomposed again."""
        orch = self._build_orch(tmp_path)
        task = _make_task(
            id="T-decomp-5",
            title="[RETRY 2] Build distributed cluster mode",
            scope="medium",
        )
        orch._decomposed_task_ids.add(task.id)

        result = orch._should_auto_decompose(task)

        assert result is False

    def test_decompose_prefix_task_never_decomposed(self, tmp_path: Path) -> None:
        """[DECOMPOSE] tasks are never re-decomposed."""
        orch = self._build_orch(tmp_path)
        task = _make_task(
            id="T-decomp-6",
            title="[DECOMPOSE] Build distributed cluster mode",
            scope="large",
        )

        result = orch._should_auto_decompose(task)

        assert result is False


# ---------------------------------------------------------------------------
# Fix D: File context discovery when owned_files is empty
# ---------------------------------------------------------------------------


class TestFileContextDiscovery:
    """TaskContextBuilder provides file context for task prompts."""

    def test_task_context_includes_file_info(self, tmp_path: Path) -> None:
        """task_context returns file-level context for the given files."""
        (tmp_path / "src" / "bernstein" / "core").mkdir(parents=True)
        (tmp_path / "src" / "bernstein" / "core" / "router.py").write_text('"""Router module."""\ndef route(): pass\n')
        builder = TaskContextBuilder(tmp_path)
        result = builder.task_context(["src/bernstein/core/router.py"])
        assert "router.py" in result


# ---------------------------------------------------------------------------
# Fix E: _maybe_retry_task progressive timeout + high-stakes escalation
# ---------------------------------------------------------------------------


class TestMaybeRetryProgressiveTimeout:
    """_maybe_retry_task applies progressive timeout and high-stakes routing."""

    def _build_orch_for_maybe_retry(self, tmp_path: Path, task: Task) -> tuple[Orchestrator, list[dict]]:
        """Build orchestrator that captures POSTed retry tasks."""
        posted: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST" and request.url.path == "/tasks":
                posted.append(json.loads(request.content))
                return httpx.Response(201, json={"id": "RETRY-001"})
            return httpx.Response(200, json={})

        transport = httpx.MockTransport(handler)
        cfg = OrchestratorConfig(
            server_url="http://testserver",
            max_task_retries=2,
        )
        adp = _mock_adapter()
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adp, templates_dir, tmp_path)
        client = httpx.Client(transport=transport, base_url="http://testserver")
        return Orchestrator(cfg, spawner, tmp_path, client=client), posted

    def test_maybe_retry_applies_progressive_timeout(self, tmp_path: Path) -> None:
        """First retry via _maybe_retry_task doubles estimated_minutes."""
        task = _make_task(
            id="T-mr-1",
            estimated_minutes=30,
            status="failed",
        )
        orch, posted = self._build_orch_for_maybe_retry(tmp_path, task)

        orch._maybe_retry_task(task)

        assert len(posted) == 1
        # retry_count=0, multiplier=(0+2)=2 → 30*2=60
        assert posted[0]["estimated_minutes"] == 60

    def test_maybe_retry_large_scope_uses_opus_max(self, tmp_path: Path) -> None:
        """Large-scope tasks get opus/max on any retry via _maybe_retry_task."""
        task = _make_task(
            id="T-mr-2",
            scope="large",
            status="failed",
            model="sonnet",
            effort="high",
        )
        orch, posted = self._build_orch_for_maybe_retry(tmp_path, task)

        orch._maybe_retry_task(task)

        assert len(posted) == 1
        assert posted[0]["model"] == "opus"
        assert posted[0]["effort"] == "max"

    def test_maybe_retry_architect_uses_opus_max(self, tmp_path: Path) -> None:
        """Architect role gets opus/max on any retry via _maybe_retry_task."""
        task = _make_task(
            id="T-mr-3",
            role="architect",
            status="failed",
        )
        orch, posted = self._build_orch_for_maybe_retry(tmp_path, task)

        orch._maybe_retry_task(task)

        assert len(posted) == 1
        assert posted[0]["model"] == "opus"
        assert posted[0]["effort"] == "max"

    def test_maybe_retry_high_complexity_large_scope_uses_opus_max(self, tmp_path: Path) -> None:
        """High-complexity large-scope tasks get opus/max on retry."""
        task = _make_task(
            id="T-mr-4",
            complexity="high",
            scope="large",
            role="backend",
            status="failed",
        )
        orch, posted = self._build_orch_for_maybe_retry(tmp_path, task)

        orch._maybe_retry_task(task)

        assert len(posted) == 1
        assert posted[0]["model"] == "opus"
        assert posted[0]["effort"] == "max"


# ---------------------------------------------------------------------------
# Fix F: route_task legacy function — LARGE and architect/security → opus/max
# ---------------------------------------------------------------------------


class TestRouteTaskLegacyFunction:
    """The legacy route_task() function should use opus/max for high-stakes routing."""

    def test_large_scope_routes_to_opus_max(self) -> None:
        from bernstein.core.router import route_task

        task = _make_task(scope="large")
        cfg = route_task(task)
        assert cfg.model == "opus"
        assert cfg.effort == "max"

    def test_architect_role_routes_to_opus_max(self) -> None:
        from bernstein.core.router import route_task

        task = _make_task(role="architect")
        cfg = route_task(task)
        assert cfg.model == "opus"
        assert cfg.effort == "max"

    def test_security_role_routes_to_opus_max(self) -> None:
        from bernstein.core.router import route_task

        task = _make_task(role="security")
        cfg = route_task(task)
        assert cfg.model == "opus"
        assert cfg.effort == "max"

    def test_manager_role_routes_to_opus_max(self) -> None:
        from bernstein.core.router import route_task

        task = _make_task(role="manager")
        cfg = route_task(task)
        assert cfg.model == "opus"
        assert cfg.effort == "max"

    def test_medium_scope_backend_uses_sonnet(self) -> None:
        from bernstein.core.router import route_task

        task = _make_task(role="backend", scope="medium", complexity="medium")
        cfg = route_task(task)
        # Should not escalate to opus for ordinary tasks
        assert cfg.model in ("sonnet", "haiku")
