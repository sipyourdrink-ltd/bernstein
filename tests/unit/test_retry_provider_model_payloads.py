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
    role_provider: str | None = None,
    run_pin: str | None = None,
    effort: str | None = "low",
    task_metadata: dict[str, Any] | None = None,
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
        metadata=task_metadata if task_metadata is not None else {"source": "synthetic-local-fixture"},
    )
    if scenario == "security":
        task.role = "security"
    role_entry: dict[str, Any] = {}
    if role_pin is not None:
        role_entry["model"] = role_pin
    if role_provider is not None:
        role_entry["provider"] = role_provider
    role_policy: dict[str, dict[str, Any]] | None = {task.role: role_entry} if role_entry else None
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
                default_adapter_name=adapter,
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


# ---------------------------------------------------------------------------
# Gateway-only configuration (#6605): the retry decision never selects a
# provider/model pair the configured adapter cannot reach.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["tick", "reap"])
@pytest.mark.parametrize("scenario", ["large", "second"])
def test_gateway_run_pin_keeps_the_provider_prefix_on_every_retry(
    path: str, scenario: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance: OPENAI_API_BASE set, run-level ``model: openai/<x>``.

    Every retry keeps the ``openai/`` provider from the run configuration;
    effort still follows the escalation schedule.
    """
    monkeypatch.setenv("OPENAI_API_BASE", "https://synthetic.invalid/v1")
    monkeypatch.setattr(lifecycle.time, "time", lambda: 1000.0)
    body = _post_retry(path, model=None, adapter="codex", scenario=scenario, run_pin="openai/research-combo")
    assert body["model"] == "openai/research-combo"
    assert body["model"].startswith("openai/")
    assert body["effort"] == ("max" if scenario == "large" else "high")


@pytest.mark.parametrize("path", ["tick", "reap"])
@pytest.mark.parametrize("adapter", ["codex", "qwen"])
@pytest.mark.parametrize("scenario", ["large", "second", "blocking", "deadline", "security"])
@pytest.mark.parametrize("model", [None, "sonnet", "opus", "haiku"])
@pytest.mark.parametrize("effort", [None, "low"])
def test_gateway_only_retry_decision_never_yields_a_bare_claude_tier(
    path: str,
    adapter: str,
    scenario: str,
    model: str | None,
    effort: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance: with a gateway-only configuration the retry decision never
    yields a bare ``sonnet``/``opus`` (or any Claude tier).

    A bare tier label on the failed task is an escalation artifact, not an
    operator pin: the first attempt's spawn coerced it to the adapter's
    configured default, so the retry leaves the model unset and replays the
    original effort, reaching the same resolution path as the first attempt.
    """
    monkeypatch.setenv("OPENAI_API_BASE", "https://synthetic.invalid/v1")
    monkeypatch.setattr(lifecycle.time, "time", lambda: 1000.0)
    body = _post_retry(path, model=model, adapter=adapter, scenario=scenario, effort=effort)
    assert body["model"] is None
    assert body["effort"] == effort


@pytest.mark.parametrize("path", ["tick", "reap"])
@pytest.mark.parametrize("scenario", ["large", "second", "blocking"])
def test_non_claude_adapter_retains_a_genuine_unprefixed_model(path: str, scenario: str) -> None:
    """A non-tier unprefixed name is a real model identity: retained verbatim,
    never escalated into a Claude tier the adapter cannot route."""
    body = _post_retry(path, model="MiniMax-M3", adapter="codex", scenario=scenario)
    assert body["model"] == "MiniMax-M3"


@pytest.mark.parametrize("path", ["tick", "reap"])
def test_metadata_pinned_tier_name_survives_a_non_claude_retry(path: str) -> None:
    """``metadata["pinned_model"]`` marks a tier name as a genuine pin (the
    A/B-test escape hatch); the non-Claude guard must not drop it."""
    body = _post_retry(
        path,
        model="opus",
        adapter="codex",
        scenario="large",
        task_metadata={"pinned_model": True},
    )
    assert body["model"] == "opus"


@pytest.mark.parametrize("path", ["tick", "reap"])
def test_role_policy_provider_pin_makes_the_retry_adapter_aware(path: str) -> None:
    """A ``role_model_policy.<role>.provider`` pin wins over the run-level
    adapter: the role spawns against a non-Claude provider even when the
    default adapter is Claude, so the retry must not stamp a tier name."""
    body = _post_retry(path, model="sonnet", adapter="claude", scenario="large", role_provider="qwen")
    assert body["model"] is None


@pytest.mark.parametrize("path", ["tick", "reap"])
def test_dropped_tier_label_reaches_the_same_adapter_default_as_the_first_attempt(tmp_path: Path, path: str) -> None:
    """A retry that drops an unpinned tier label resolves, through the real
    spawn selector, the same adapter default a first attempt with that label
    was coerced to."""
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
    first = Task(id="first", title="Synthetic", description="Local fixture", role="backend", model="sonnet")
    body = _post_retry(path, model="sonnet", effort=None, adapter="codex", scenario="large")
    assert body["model"] is None
    retry = Task.from_dict({"id": "retry", **body})
    for task in (first, retry):
        with pytest.raises(RoutingReached):
            spawner.spawn_for_tasks([task])
    # Both attempts resolve the adapter's configured default model; the retry
    # (model left unset) also picks up the role template's default effort,
    # exactly like a first attempt with no overrides.
    assert [model for model, _effort in resolved] == ["openai/configured-default"] * 2
    assert resolved[1] == ("openai/configured-default", "high")
    adapter.spawn.assert_not_called()


@pytest.mark.parametrize("path", ["tick", "reap"])
def test_provider_alias_anthropic_resolves_as_claude_compatible(path: str) -> None:
    """role_model_policy.<role>.provider='anthropic' is an alias for claude, so
    unpinned Claude tiers still advance along the Claude ladder (#6605)."""
    body = _post_retry(
        path,
        model="sonnet",
        effort="medium",
        role_provider="anthropic",
        adapter="codex",
        scenario="large",
    )
    assert body["model"] == "opus"
    assert body["effort"] == "max"
