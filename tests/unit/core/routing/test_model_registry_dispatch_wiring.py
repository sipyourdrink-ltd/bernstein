"""Model-registry enforcement is wired into every real dispatch route.

``BERNSTEIN_MODEL_REGISTRY_ENFORCEMENT`` must change what the spawner and the
provider-batch path actually start: with the flag on, a model without a live
admission is refused before any process or provider job exists, and the
refusal is appended to the audit chain. With the flag unset nothing changes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.batch_api import ProviderBatchManager
from bernstein.core.models import BatchConfig, ModelConfig
from bernstein.core.router import ModelConfig as RouterModelConfig
from bernstein.core.router import ProviderConfig, Tier, TierAwareRouter
from bernstein.core.spawner import AgentSpawner

from bernstein.adapters.base import SpawnError
from bernstein.core.routing.model_registry import (
    ANY_TASK_CLASS,
    format_timestamp,
    record_model_admission,
)
from bernstein.core.security.audit_chain import EVENT_MODEL_REFUSED, AuditChainStore

_ENV = "BERNSTEIN_MODEL_REGISTRY_ENFORCEMENT"


def _chain(workdir: Path) -> AuditChainStore:
    return AuditChainStore(workdir / ".sdd" / "audit")


def _admit(workdir: Path, provider: str, model: str, task_class: str = ANY_TASK_CLASS) -> None:
    record_model_admission(
        chain=_chain(workdir),
        provider=provider,
        model=model,
        version=None,
        task_classes=(task_class,),
        admitted_by="operator@example.test",
        expires_at=format_timestamp(datetime.now(tz=UTC) + timedelta(days=30)),
    )


def _refusals(workdir: Path) -> list[Any]:
    return list(_chain(workdir).query(event_type=EVENT_MODEL_REFUSED))


def _spawner(tmp_path: Path, adapter: MagicMock, **kwargs: Any) -> AgentSpawner:
    templates_dir = tmp_path / "templates" / "roles"
    templates_dir.mkdir(parents=True, exist_ok=True)
    kwargs.setdefault("default_model", "mock-model")
    return AgentSpawner(adapter, templates_dir, tmp_path, use_worktrees=False, **kwargs)


# --- fresh spawn ------------------------------------------------------------


def test_fresh_spawn_refuses_unregistered_model_when_enforced(
    tmp_path: Path, make_task: Any, mock_adapter_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_ENV, "1")
    adapter = mock_adapter_factory(pid=11)
    spawner = _spawner(tmp_path, adapter)

    with pytest.raises(SpawnError, match="model registry refused"):
        spawner.spawn_for_tasks([make_task()])

    adapter.spawn.assert_not_called()
    refusals = _refusals(tmp_path)
    assert len(refusals) == 1
    assert refusals[0].details["model_requested"] == "mock-model"
    assert refusals[0].details["routing_path"] == "spawner"
    assert refusals[0].details["task_class"] == "backend"
    ok, errors = _chain(tmp_path).verify()
    assert ok, errors


def test_fresh_spawn_proceeds_for_an_admitted_model(
    tmp_path: Path, make_task: Any, mock_adapter_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_ENV, "1")
    adapter = mock_adapter_factory(pid=12)
    _admit(tmp_path, "MockCLI", "mock-model")

    session = _spawner(tmp_path, adapter).spawn_for_tasks([make_task()])

    assert session.pid == 12
    assert _refusals(tmp_path) == []


def test_admission_is_scoped_to_the_task_class(
    tmp_path: Path, make_task: Any, mock_adapter_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_ENV, "1")
    adapter = mock_adapter_factory(pid=13)
    _admit(tmp_path, "MockCLI", "mock-model", task_class="qa")

    with pytest.raises(SpawnError, match="model registry refused"):
        _spawner(tmp_path, adapter).spawn_for_tasks([make_task(role="backend")])

    adapter.spawn.assert_not_called()


def test_fresh_spawn_unchanged_when_flag_unset(
    tmp_path: Path, make_task: Any, mock_adapter_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(_ENV, raising=False)
    adapter = mock_adapter_factory(pid=14)

    # Off means off: the registry's audit chain is not even opened.
    with patch("bernstein.core.routing.route_decision.AuditChainStore", side_effect=AssertionError("opened")):
        session = _spawner(tmp_path, adapter).spawn_for_tasks([make_task()])

    assert session.pid == 14
    assert _refusals(tmp_path) == []


def test_operator_model_override_is_gated(
    tmp_path: Path, make_task: Any, mock_adapter_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_ENV, "1")
    adapter = mock_adapter_factory(pid=15)
    _admit(tmp_path, "MockCLI", "mock-model")

    with pytest.raises(SpawnError, match="model registry refused"):
        _spawner(tmp_path, adapter).spawn_for_tasks([make_task()], model_override="other-model")

    adapter.spawn.assert_not_called()
    assert _refusals(tmp_path)[0].details["model_requested"] == "other-model"


def test_role_policy_pin_is_gated(
    tmp_path: Path, make_task: Any, mock_adapter_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_ENV, "1")
    adapter = mock_adapter_factory(pid=16)
    _admit(tmp_path, "MockCLI", "mock-model")
    spawner = _spawner(tmp_path, adapter, role_model_policy={"backend": {"model": "pinned-model"}})

    with pytest.raises(SpawnError, match="model registry refused"):
        spawner.spawn_for_tasks([make_task(role="backend")])

    adapter.spawn.assert_not_called()


def test_enabled_but_unopenable_chain_fails_closed(
    tmp_path: Path, make_task: Any, mock_adapter_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_ENV, "1")
    adapter = mock_adapter_factory(pid=17)
    spawner = _spawner(tmp_path, adapter)

    with patch("bernstein.core.routing.route_decision.AuditChainStore", side_effect=OSError("disk gone")):
        with pytest.raises(SpawnError, match="could not be checked"):
            spawner.spawn_for_tasks([make_task()])

    adapter.spawn.assert_not_called()


# --- router failover --------------------------------------------------------


def test_failover_to_an_unadmitted_model_is_refused(
    tmp_path: Path, make_task: Any, mock_adapter_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_ENV, "1")
    router = TierAwareRouter()
    router.state.preferred_tier = Tier.FREE
    router.register_provider(
        ProviderConfig(
            name="primary",
            models={"sonnet": RouterModelConfig("sonnet", "high")},
            tier=Tier.FREE,
            cost_per_1k_tokens=0.0,
        )
    )
    router.register_provider(
        ProviderConfig(
            name="backup",
            models={"haiku": RouterModelConfig("haiku", "high")},
            tier=Tier.STANDARD,
            cost_per_1k_tokens=0.003,
        )
    )
    # Only the primary choice is admitted; the failover target is not.
    _admit(tmp_path, "primary", "sonnet")

    failing = mock_adapter_factory(pid=0)
    failing.spawn.side_effect = RuntimeError("rate limit exceeded")
    failing.name.return_value = "claude"
    backup = mock_adapter_factory(pid=901)
    backup.name.return_value = "gemini"
    primary = mock_adapter_factory(pid=123)
    primary.name.return_value = "claude"
    spawner = _spawner(tmp_path, primary, router=router, default_model="sonnet")

    with patch.object(spawner, "_get_adapter_by_name", side_effect=[failing, backup]):
        with pytest.raises(SpawnError, match="model registry refused"):
            spawner.spawn_for_tasks([make_task()])

    backup.spawn.assert_not_called()
    paths = [r.details["routing_path"] for r in _refusals(tmp_path)]
    assert paths == ["spawner_failover"]


# --- crash resume -----------------------------------------------------------


def test_resume_refuses_unregistered_model_when_enforced(
    tmp_path: Path, make_task: Any, mock_adapter_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_ENV, "1")
    adapter = mock_adapter_factory(pid=21)
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    with pytest.raises(SpawnError, match="model registry refused"):
        _spawner(tmp_path, adapter).spawn_for_resume([make_task()], worktree_path=worktree, changed_files=["a.py"])

    adapter.spawn.assert_not_called()
    assert _refusals(tmp_path)[0].details["routing_path"] == "spawner_resume"


def test_resume_unchanged_when_flag_unset(
    tmp_path: Path, make_task: Any, mock_adapter_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(_ENV, raising=False)
    adapter = mock_adapter_factory(pid=22)
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    session = _spawner(tmp_path, adapter).spawn_for_resume(
        [make_task()], worktree_path=worktree, changed_files=["a.py"]
    )

    assert session.pid == 22


# --- provider batch ---------------------------------------------------------


class _BatchRouter:
    def __init__(self) -> None:
        self.state = SimpleNamespace(providers={"openai": SimpleNamespace(cost_per_1k_tokens=0.02)})

    def select_provider_for_task(self, task: Any, base_config: ModelConfig | None = None, **_: Any) -> Any:
        del task
        return SimpleNamespace(provider="openai", model_config=base_config)


def _batch_orch(tmp_path: Path) -> Any:
    worktree_mgr = MagicMock()
    worktree_mgr.create.return_value = tmp_path / "batch-worktree"
    return SimpleNamespace(
        _router=_BatchRouter(),
        _spawner=SimpleNamespace(
            _worktree_mgr=worktree_mgr,
            _worktree_paths={},
            _default_model="mock-model",
        ),
        _batch_sessions={},
        _task_to_session={},
        _file_ownership={},
        _recorder=MagicMock(),
        _workdir=tmp_path,
    )


def _batch_manager(tmp_path: Path, client: MagicMock) -> ProviderBatchManager:
    manager = ProviderBatchManager(
        tmp_path,
        BatchConfig(enabled=True, eligible=["docs"]),
        provider_clients={"openai": client},
    )
    manager._trace_store = MagicMock()
    return manager


def test_provider_batch_declines_unregistered_model_when_enforced(
    tmp_path: Path, make_task: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_ENV, "1")
    client = MagicMock()
    manager = _batch_manager(tmp_path, client)
    orch = _batch_orch(tmp_path)

    with patch("bernstein.core.tasks.batch_api.get_collector", return_value=MagicMock()):
        result = manager.try_submit(orch, make_task(id="T-docs", title="Update docs", description="Refresh docs."))

    assert result.handled is False
    assert result.submitted is False
    assert "model registry refused" in (result.reason or "")
    client.submit.assert_not_called()
    orch._spawner._worktree_mgr.create.assert_not_called()
    assert _refusals(tmp_path)[0].details["routing_path"] == "provider_batch"


def test_provider_batch_submits_an_admitted_model(
    tmp_path: Path, make_task: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_ENV, "1")
    _admit(tmp_path, "openai", "mock-model")
    client = MagicMock()
    client.submit.return_value = SimpleNamespace(external_id="ext-1")
    manager = _batch_manager(tmp_path, client)
    orch = _batch_orch(tmp_path)

    with patch("bernstein.core.tasks.batch_api.get_collector", return_value=MagicMock()):
        result = manager.try_submit(orch, make_task(id="T-docs", title="Update docs", description="Refresh docs."))

    assert result.submitted is True
    client.submit.assert_called_once()
    assert _refusals(tmp_path) == []
