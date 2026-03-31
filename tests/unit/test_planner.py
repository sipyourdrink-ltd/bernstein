"""Unit tests for planner HTTP and LLM orchestration."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

import bernstein.core.planner as planner
from bernstein.core.models import (
    CompletionSignal,
    Complexity,
    RiskAssessment,
    RollbackPlan,
    Scope,
    Task,
    TaskType,
    UpgradeProposalDetails,
)


class _FakeResponse:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._payload


class _FakePlannerClient:
    def __init__(self) -> None:
        self.posts: list[dict[str, object]] = []

    async def post(self, url: str, json: dict[str, object]) -> _FakeResponse:
        self.posts.append({"url": url, "json": json})
        return _FakeResponse({"id": "server-task-1"})


class _FakeCollector:
    def __init__(self) -> None:
        self.api_calls: list[dict[str, object]] = []
        self.errors: list[tuple[object, ...]] = []

    def record_api_call(self, **kwargs: object) -> None:
        self.api_calls.append(kwargs)

    def record_error(self, *args: object) -> None:
        self.errors.append(args)


def test_post_task_to_server_boosts_upgrade_priority_and_sets_planned_status() -> None:
    client = _FakePlannerClient()
    task = Task(
        id="local-1",
        title="Upgrade router",
        description="Improve routing policy",
        role="backend",
        priority=3,
        scope=Scope.MEDIUM,
        complexity=Complexity.MEDIUM,
        task_type=TaskType.UPGRADE_PROPOSAL,
        upgrade_details=UpgradeProposalDetails(
            current_state="old",
            proposed_change="new",
            benefits=["better"],
            risk_assessment=RiskAssessment(level="medium"),
            rollback_plan=RollbackPlan(steps=["revert"]),
        ),
    )

    created_id = asyncio.run(planner._post_task_to_server(cast("Any", client), "http://server", task, plan_mode=True))

    assert created_id == "server-task-1"
    body = cast("dict[str, object]", client.posts[0]["json"])
    assert body["priority"] == 2
    assert body["status"] == "planned"
    assert body["task_type"] == "upgrade_proposal"
    assert "upgrade_details" in body


def test_fetch_existing_tasks_parses_upgrade_details_from_server() -> None:
    class _Client:
        async def get(self, _url: str) -> _FakeResponse:
            return _FakeResponse(
                [
                    {
                        "id": "task-1",
                        "title": "Upgrade planner",
                        "description": "Refine planner",
                        "role": "backend",
                        "scope": "large",
                        "complexity": "high",
                        "status": "open",
                        "task_type": "upgrade_proposal",
                        "upgrade_details": {
                            "current_state": "old",
                            "proposed_change": "new",
                            "risk_assessment": {"level": "high"},
                            "rollback_plan": {"steps": ["revert"]},
                        },
                    }
                ]
            )

    tasks = asyncio.run(planner._fetch_existing_tasks(cast("Any", _Client()), "http://server"))

    assert len(tasks) == 1
    assert tasks[0].task_type is TaskType.UPGRADE_PROPOSAL
    assert tasks[0].upgrade_details is not None
    assert tasks[0].upgrade_details.risk_assessment.level == "high"


def test_plan_uses_llm_output_and_posts_created_tasks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    collector = _FakeCollector()
    posted_titles: list[str] = []

    class _FakeCache:
        def __init__(self, _workdir: Path) -> None:
            self.saved = False

        def lookup(self, goal: str, *, model: str) -> tuple[str | None, float]:
            return None, 0.0

        def store(self, goal: str, raw_response: str, *, model: str) -> None:
            return None

        def save(self) -> None:
            self.saved = True

    class _AsyncClient:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        async def __aenter__(self) -> _AsyncClient:
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
            return None

    async def _fake_fetch_existing_tasks(_client: object, _server_url: str) -> list[Task]:
        return []

    async def _fake_post_task_to_server(
        _client: object, _server_url: str, task: Task, *, plan_mode: bool = False
    ) -> str:
        posted_titles.append(task.title)
        return f"server-{len(posted_titles)}"

    async def _fake_call_llm(prompt: str, *, model: str, provider: str) -> str:
        if "Do you need to search the web" in prompt:
            return "NONE"
        return json.dumps(
            [
                {
                    "title": "Implement planner test",
                    "description": "Add planner coverage",
                    "role": "backend",
                    "completion_signals": [
                        {"type": "test_passes", "value": "uv run pytest tests/unit/test_planner.py"}
                    ],
                }
            ]
        )

    def _fake_context(_workdir: Path) -> str:
        return "CTX"

    def _fake_roles(_roles_dir: Path) -> list[str]:
        return ["backend"]

    def _fake_prompt(**_: object) -> str:
        return "PROMPT"

    def _fake_get_collector() -> _FakeCollector:
        return collector

    monkeypatch.setattr(planner, "gather_project_context", _fake_context)
    monkeypatch.setattr(planner, "available_roles", _fake_roles)
    monkeypatch.setattr(planner, "_fetch_existing_tasks", _fake_fetch_existing_tasks)
    monkeypatch.setattr(planner, "_post_task_to_server", _fake_post_task_to_server)
    monkeypatch.setattr(planner, "call_llm", _fake_call_llm)
    monkeypatch.setattr(planner, "render_plan_prompt", _fake_prompt)
    monkeypatch.setattr(planner, "get_collector", _fake_get_collector)
    monkeypatch.setattr(planner, "SemanticCacheManager", _FakeCache)
    monkeypatch.setattr(planner.httpx, "AsyncClient", _AsyncClient)

    tasks = asyncio.run(
        planner.plan(
            goal="Add planner test coverage",
            server_url="http://server",
            workdir=tmp_path,
            templates_dir=tmp_path / "templates",
            model="sonnet",
            provider="anthropic",
        )
    )

    assert [task.id for task in tasks] == ["server-1"]
    assert [task.title for task in tasks] == ["Implement planner test"]
    assert posted_titles == ["Implement planner test"]
    assert collector.api_calls
    assert tasks[0].completion_signals == [
        CompletionSignal(type="test_passes", value="uv run pytest tests/unit/test_planner.py")
    ]


def test_plan_recovers_when_fetch_existing_tasks_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    collector = _FakeCollector()

    class _FakeCache:
        def __init__(self, _workdir: Path) -> None:
            return None

        def lookup(self, goal: str, *, model: str) -> tuple[str | None, float]:
            return None, 0.0

        def store(self, goal: str, raw_response: str, *, model: str) -> None:
            return None

        def save(self) -> None:
            return None

    class _AsyncClient:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        async def __aenter__(self) -> _AsyncClient:
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
            return None

    async def _raise_fetch(_client: object, _server_url: str) -> list[Task]:
        raise httpx.HTTPError("server unavailable")

    async def _post(_client: object, _server_url: str, task: Task, *, plan_mode: bool = False) -> str:
        return "server-recovered"

    async def _fake_call_llm(prompt: str, *, model: str, provider: str) -> str:
        if "Do you need to search the web" in prompt:
            return "NONE"
        return json.dumps([{"title": "Recovered planning task", "role": "backend"}])

    def _fake_context(_workdir: Path) -> str:
        return "CTX"

    def _fake_roles(_roles_dir: Path) -> list[str]:
        return ["backend"]

    def _fake_prompt(**_: object) -> str:
        return "PROMPT"

    def _fake_get_collector() -> _FakeCollector:
        return collector

    monkeypatch.setattr(planner, "gather_project_context", _fake_context)
    monkeypatch.setattr(planner, "available_roles", _fake_roles)
    monkeypatch.setattr(planner, "_fetch_existing_tasks", _raise_fetch)
    monkeypatch.setattr(planner, "_post_task_to_server", _post)
    monkeypatch.setattr(planner, "call_llm", _fake_call_llm)
    monkeypatch.setattr(planner, "render_plan_prompt", _fake_prompt)
    monkeypatch.setattr(planner, "get_collector", _fake_get_collector)
    monkeypatch.setattr(planner, "SemanticCacheManager", _FakeCache)
    monkeypatch.setattr(planner.httpx, "AsyncClient", _AsyncClient)

    tasks = asyncio.run(
        planner.plan(
            goal="Recover planning despite task fetch error",
            server_url="http://server",
            workdir=tmp_path,
            templates_dir=tmp_path / "templates",
            model="sonnet",
            provider="anthropic",
        )
    )

    assert [task.id for task in tasks] == ["server-recovered"]


def test_plan_raises_runtime_error_when_llm_planning_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    collector = _FakeCollector()
    call_count = 0

    class _FakeCache:
        def __init__(self, _workdir: Path) -> None:
            return None

        def lookup(self, goal: str, *, model: str) -> tuple[str | None, float]:
            return None, 0.0

        def store(self, goal: str, raw_response: str, *, model: str) -> None:
            return None

        def save(self) -> None:
            return None

    class _AsyncClient:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        async def __aenter__(self) -> _AsyncClient:
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
            return None

    async def _fetch(_client: object, _server_url: str) -> list[Task]:
        return []

    async def _fake_call_llm(prompt: str, *, model: str, provider: str) -> str:
        nonlocal call_count
        call_count += 1
        if "Do you need to search the web" in prompt:
            return "NONE"
        raise RuntimeError("llm down")

    def _fake_context(_workdir: Path) -> str:
        return "CTX"

    def _fake_roles(_roles_dir: Path) -> list[str]:
        return ["backend"]

    def _fake_prompt(**_: object) -> str:
        return "PROMPT"

    def _fake_get_collector() -> _FakeCollector:
        return collector

    monkeypatch.setattr(planner, "gather_project_context", _fake_context)
    monkeypatch.setattr(planner, "available_roles", _fake_roles)
    monkeypatch.setattr(planner, "_fetch_existing_tasks", _fetch)
    monkeypatch.setattr(planner, "call_llm", _fake_call_llm)
    monkeypatch.setattr(planner, "render_plan_prompt", _fake_prompt)
    monkeypatch.setattr(planner, "get_collector", _fake_get_collector)
    monkeypatch.setattr(planner, "SemanticCacheManager", _FakeCache)
    monkeypatch.setattr(planner.httpx, "AsyncClient", _AsyncClient)

    with pytest.raises(RuntimeError, match="LLM planning call failed"):
        asyncio.run(
            planner.plan(
                goal="Fail planner",
                server_url="http://server",
                workdir=tmp_path,
                templates_dir=tmp_path / "templates",
                model="sonnet",
                provider="anthropic",
            )
        )

    assert call_count == 2
    assert collector.errors
