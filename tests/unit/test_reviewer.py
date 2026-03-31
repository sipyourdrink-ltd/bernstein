"""Unit tests for review and queue-review wrappers."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import bernstein.core.reviewer as reviewer
from bernstein.core.manager_models import QueueReviewResult
from bernstein.core.models import Complexity, Scope, Task


class _FakeCollector:
    def __init__(self) -> None:
        self.api_calls: list[dict[str, object]] = []
        self.errors: list[tuple[object, ...]] = []

    def record_api_call(self, **kwargs: object) -> None:
        self.api_calls.append(kwargs)

    def record_error(self, *args: object) -> None:
        self.errors.append(args)


def _task() -> Task:
    return Task(
        id="task-1",
        title="Review planner",
        description="Inspect planner changes",
        role="backend",
        scope=Scope.MEDIUM,
        complexity=Complexity.MEDIUM,
        result_summary="Implemented planner changes.",
    )


def test_review_parses_follow_up_tasks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    collector = _FakeCollector()

    async def _fake_call_llm(prompt: str, *, model: str, provider: str) -> str:
        return json.dumps(
            {
                "verdict": "request_changes",
                "reasoning": "Need tests.",
                "feedback": "Add regression coverage.",
                "follow_up_tasks": [{"title": "Add tests", "role": "qa", "description": "Cover edge cases"}],
            }
        )

    def _fake_context(_workdir: Path) -> str:
        return "CTX"

    def _fake_render_prompt(task: Task, context: str, templates_dir: Path) -> str:
        return "PROMPT"

    def _fake_get_collector() -> _FakeCollector:
        return collector

    monkeypatch.setattr(reviewer, "gather_project_context", _fake_context)
    monkeypatch.setattr(reviewer, "render_review_prompt", _fake_render_prompt)
    monkeypatch.setattr(reviewer, "call_llm", _fake_call_llm)
    monkeypatch.setattr(reviewer, "get_collector", _fake_get_collector)

    result = asyncio.run(reviewer.review(_task(), tmp_path, tmp_path / "templates", "sonnet", "anthropic"))

    assert result.verdict == "request_changes"
    assert result.feedback == "Add regression coverage."
    assert [task.id for task in result.follow_up_tasks] == ["followup-001"]
    assert collector.api_calls


def test_review_queue_skips_when_budget_is_low(tmp_path: Path) -> None:
    result = asyncio.run(
        reviewer.review_queue(
            completed_count=5,
            failed_count=1,
            budget_remaining_pct=0.05,
            server_url="http://server",
            model="haiku",
            provider="anthropic",
            templates_dir=tmp_path / "templates",
        )
    )

    assert result.skipped is True
    assert result.reasoning == "skipped: budget < 10%"


def test_review_queue_sync_runs_async_wrapper(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def _fake_review_queue(*args: object, **kwargs: object) -> QueueReviewResult:
        return QueueReviewResult(corrections=[], reasoning="ok")

    monkeypatch.setattr(reviewer, "review_queue", _fake_review_queue)

    result = reviewer.review_queue_sync(1, 0, 0.9, "http://server", "haiku", "anthropic", tmp_path / "templates")

    assert result.reasoning == "ok"
    assert result.skipped is False


def test_review_raises_runtime_error_when_llm_call_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    collector = _FakeCollector()

    async def _raise_llm(prompt: str, *, model: str, provider: str) -> str:
        raise RuntimeError("llm unavailable")

    def _fake_context(_workdir: Path) -> str:
        return "CTX"

    def _fake_render_prompt(task: Task, context: str, templates_dir: Path) -> str:
        return "PROMPT"

    def _fake_get_collector() -> _FakeCollector:
        return collector

    monkeypatch.setattr(reviewer, "gather_project_context", _fake_context)
    monkeypatch.setattr(reviewer, "render_review_prompt", _fake_render_prompt)
    monkeypatch.setattr(reviewer, "call_llm", _raise_llm)
    monkeypatch.setattr(reviewer, "get_collector", _fake_get_collector)

    with pytest.raises(RuntimeError, match="LLM review call failed"):
        asyncio.run(reviewer.review(_task(), tmp_path, tmp_path / "templates", "sonnet", "anthropic"))

    assert collector.errors


def test_review_queue_returns_skipped_on_parse_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> list[dict[str, object]]:
            return [{"id": "task-1", "title": "demo", "status": "open", "role": "backend"}]

    class _Client:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
            return None

        async def get(self, _url: str) -> _Response:
            return _Response()

    async def _invalid_json_llm(prompt: str, *, model: str, provider: str, max_tokens: int = 500) -> str:
        return "not-json"

    def _fake_queue_prompt(**kwargs: object) -> str:
        return "PROMPT"

    monkeypatch.setattr(reviewer.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(reviewer, "render_queue_review_prompt", _fake_queue_prompt)
    monkeypatch.setattr(reviewer, "call_llm", _invalid_json_llm)

    result = asyncio.run(
        reviewer.review_queue(
            completed_count=2,
            failed_count=1,
            budget_remaining_pct=0.9,
            server_url="http://server",
            model="haiku",
            provider="anthropic",
            templates_dir=tmp_path / "templates",
        )
    )

    assert result.skipped is True
    assert result.reasoning.startswith("parse error:")
