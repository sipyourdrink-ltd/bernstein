"""Resolved provider-model identity survives both real retry POST paths (#6605)."""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from bernstein.adapters.base import CLIAdapter
from bernstein.core.agents.spawner_core import AgentSpawner
from bernstein.core.tasks import task_lifecycle as lifecycle
from bernstein.core.tasks.models import Scope, Task

_EXPECTED_SOURCE = Path(__file__).resolve().parents[2] / "src/bernstein/core/tasks/task_lifecycle.py"
for _creator in (lifecycle.maybe_retry_task, lifecycle.retry_or_fail_task):
    _source = inspect.getsourcefile(_creator)
    assert _source is not None
    assert Path(_source).resolve() == _EXPECTED_SOURCE.resolve()

_PATHS = [("tick", None), ("reap", None), ("reap", "claude"), ("reap", "codex")]


def _post_retry(
    path: str,
    *,
    model: str | None = "openai/review-combo",
    adapter: str | None = None,
    scenario: str = "large",
    role_pin: str | None = None,
    run_pin: str | None = None,
    effort: str | None = "low",
) -> dict[str, Any]:
    task = Task(
        id="task-6605",
        title="Synthetic ordinary task",
        description="Synthetic local work",
        role="backend",
        model=model,
        effort=effort,
        scope=Scope.LARGE if scenario == "large" else Scope.MEDIUM,
        retry_count=1 if scenario == "second" else 0,
        max_retries=2,
        terminal_reason="blocking_limit" if scenario == "blocking" else None,
        deadline=1.0 if scenario == "deadline" else None,
        depends_on=["synthetic-dependency"],
        metadata={"source": "synthetic-local-fixture"},
    )
    if scenario == "security":
        task.role = "security"
    role_policy: dict[str, dict[str, Any]] | None = {task.role: {"model": role_pin}} if role_pin is not None else None
    payloads: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/tasks":
            payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "retry-6605"})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        if path == "tick":
            assert lifecycle.maybe_retry_task(
                task,
                retried_task_ids=set(),
                max_task_retries=2,
                client=client,
                server_url="http://synthetic.invalid",
                quarantine=SimpleNamespace(),
                role_model_policy=role_policy,
                run_pinned_model=run_pin,
            )
        else:
            lifecycle.retry_or_fail_task(
                task.id,
                "Task failed",
                client=client,
                server_url="http://synthetic.invalid",
                max_task_retries=2,
                retried_task_ids=set(),
                tasks_snapshot={"failed": [task]},
                default_adapter_name=adapter,
                role_model_policy=role_policy,
                run_pinned_model=run_pin,
            )
    assert len(payloads) == 1
    return payloads[0]


@pytest.mark.parametrize("path,adapter", _PATHS)
@pytest.mark.parametrize("scenario", ["large", "second", "blocking", "deadline", "security"])
@pytest.mark.parametrize("model", ["openai/review-combo", "gateway/sonnet-shadow"])
def test_retry_payload_retains_the_resolved_provider_model(
    path: str, adapter: str | None, scenario: str, model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lifecycle.time, "time", lambda: 1000.0)
    body = _post_retry(path, model=model, adapter=adapter, scenario=scenario)
    assert body["model"] == model
    expected_effort = (
        "high" if scenario == "second" else "max" if path == "tick" or scenario in {"large", "security"} else "low"
    )
    assert body["effort"] == expected_effort
    assert body["retry_count"] == (2 if scenario == "second" else 1)
    assert body["depends_on"] == ["synthetic-dependency"]
    assert body["metadata"]["retry_of"] == "task-6605"


@pytest.mark.parametrize("path", ["tick", "reap"])
@pytest.mark.parametrize(
    "role_pin,run_pin,expected",
    [
        ("openai/role-choice", "openai/run-choice", "openai/role-choice"),
        ("sonnet", "openai/run-choice", "sonnet"),
        (None, "openai/run-choice", "openai/run-choice"),
    ],
)
def test_explicit_operator_pin_wins_in_the_real_retry_payload(
    path: str, role_pin: str | None, run_pin: str, expected: str
) -> None:
    body = _post_retry(path, role_pin=role_pin, run_pin=run_pin)
    assert body["model"] == expected


@pytest.mark.parametrize("path", ["tick", "reap"])
@pytest.mark.parametrize("scenario", ["large", "second"])
def test_claude_retry_payload_keeps_the_existing_tier_policy(path: str, scenario: str) -> None:
    body = _post_retry(path, model="sonnet", scenario=scenario, adapter="claude")
    assert body["model"] == "opus"
    assert body["effort"] == ("max" if scenario == "large" else "high")


@pytest.mark.parametrize("path", ["tick", "reap"])
def test_existing_run_pin_resolves_an_absent_task_model(path: str) -> None:
    body = _post_retry(path, model=None, run_pin="openai/operator-pin")
    assert body["model"] == "openai/operator-pin"


@pytest.mark.parametrize("path,adapter", _PATHS)
@pytest.mark.parametrize("scenario", ["large", "second", "blocking", "deadline", "security"])
@pytest.mark.parametrize("effort", [None, "low"])
def test_retry_without_model_context_leaves_model_resolution_to_the_adapter(
    path: str, adapter: str | None, scenario: str, effort: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unset model never becomes a bare tier the configured endpoint may lack."""
    monkeypatch.setenv("OPENAI_API_BASE", "https://synthetic.invalid/v1")
    monkeypatch.setattr(lifecycle.time, "time", lambda: 1000.0)
    body = _post_retry(path, model=None, adapter=adapter, scenario=scenario, effort=effort)
    assert body["model"] is None
    assert body["effort"] == effort


@pytest.mark.parametrize("path", ["tick", "reap"])
def test_unresolved_retry_reaches_the_same_adapter_default_as_the_first_attempt(tmp_path: Path, path: str) -> None:
    """Exercise the real spawn selector up to routing, before any process work."""
    templates = tmp_path / "roles"
    role = templates / "backend"
    role.mkdir(parents=True)
    (role / "config.yaml").write_text("default_model: sonnet\ndefault_effort: high\n", encoding="utf-8")
    adapter = MagicMock(spec=CLIAdapter)
    adapter.name.return_value = "codex"
    adapter.default_model = "openai/configured-default"
    spawner = AgentSpawner(adapter, templates, tmp_path, use_worktrees=False)
    resolve = spawner._resolve_routing
    resolved: list[tuple[str, str]] = []

    class RoutingReached(Exception):
        pass

    def stop_after_routing(*args: Any, **kwargs: Any) -> None:
        config = resolve(*args, **kwargs)[0]
        resolved.append((config.model, config.effort))
        raise RoutingReached

    spawner._resolve_routing = stop_after_routing
    first = Task(id="first", title="Synthetic", description="Local fixture", role="backend")
    body = _post_retry(path, model=None, effort=None, adapter="codex", scenario="large")
    retry = Task.from_dict({"id": "retry", **body})
    for task in (first, retry):
        with pytest.raises(RoutingReached):
            spawner.spawn_for_tasks([task])
    assert resolved == [("openai/configured-default", "high")] * 2
    adapter.spawn.assert_not_called()
