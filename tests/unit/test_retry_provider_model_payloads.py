"""Resolved provider-model identity survives both real retry POST paths (#6605)."""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

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
) -> dict[str, Any]:
    task = Task(
        id="task-6605",
        title="Synthetic ordinary task",
        description="Synthetic local work",
        role="backend",
        model=model,
        effort="low",
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
