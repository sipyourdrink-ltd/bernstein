"""Schema / seed / default-off wiring for task-tier models (#4854)."""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.agents.spawner_core import AgentSpawner
from bernstein.core.config.config_schema import RoleModelPolicyEntry
from bernstein.core.config.seed_parser import SeedError, _parse_single_role_policy
from bernstein.core.routing import task_tier as tier_mod
from bernstein.core.routing.task_tier import TIER_ERROR
from bernstein.core.security.audit_chain import (
    EVENT_TASK_TIER_DECISION,
    AuditChainStore,
    record_task_tier_decision,
)
from bernstein.core.tasks.models import Task


def test_tier_models_rejects_error_marker() -> None:
    with pytest.raises(ValueError, match="error"):
        RoleModelPolicyEntry(model="sonnet", tier_models={TIER_ERROR: "haiku"})


def test_tier_models_accepts_closed_set() -> None:
    entry = RoleModelPolicyEntry(
        model="sonnet",
        tier_models={"light": "haiku", "heavy": "opus"},
    )
    assert entry.tier_models == {"light": "haiku", "heavy": "opus"}


def test_seed_parser_accepts_tier_models() -> None:
    parsed = _parse_single_role_policy(
        "backend",
        {"model": "sonnet", "tier_models": {"light": "haiku", "critical": "opus"}},
    )
    assert parsed["tier_models"] == {"light": "haiku", "critical": "opus"}


def test_seed_parser_rejects_unknown_tier() -> None:
    with pytest.raises(SeedError, match="unknown tier"):
        _parse_single_role_policy("backend", {"model": "sonnet", "tier_models": {"cheap": "haiku"}})


def test_unset_tier_models_preserves_model_pin_resolution() -> None:
    """A role without ``tier_models`` resolves exactly the pinned ``model``."""
    spawner = object.__new__(AgentSpawner)
    task = Task(id="t1", title="docs", description="fix typo", role="docs")
    model, record = AgentSpawner._resolve_tier_model(spawner, task, {"model": "sonnet"})
    assert model == "sonnet"
    assert record is None


def test_tier_models_selects_mapped_model() -> None:
    spawner = object.__new__(AgentSpawner)
    task = Task(
        id="t1",
        title="docs",
        description="fix typo",
        role="docs",
        metadata={"labels": ["size/xs"], "changed_files": ["README.md"]},
    )
    model, record = AgentSpawner._resolve_tier_model(
        spawner,
        task,
        {"model": "sonnet", "tier_models": {"light": "haiku", "standard": "sonnet", "heavy": "opus"}},
    )
    assert record is not None
    assert record["tier"] == "light"
    assert model == "haiku"


def test_classifier_exception_records_error_not_cheap_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(_task: object) -> object:
        raise RuntimeError("boom")

    monkeypatch.setattr(tier_mod, "classify_task", _boom)
    spawner = object.__new__(AgentSpawner)
    task = Task(id="t1", title="x", description="y", role="backend")
    model, record = AgentSpawner._resolve_tier_model(
        spawner,
        task,
        {"model": "sonnet", "tier_models": {"light": "haiku", "critical": "opus"}},
    )
    assert model == "sonnet"
    assert record is not None
    assert record["tier"] == TIER_ERROR


def test_record_task_tier_decision_on_chain(tmp_path: Path) -> None:
    chain = AuditChainStore(tmp_path / "audit")
    event = record_task_tier_decision(
        chain=chain,
        run_id="run-1",
        task_id="t1",
        tier="standard",
        tier_policy_version=1,
        feature_digest="abc",
        features={"size_rank": 2},
        score=5,
    )
    assert event.event_type == EVENT_TASK_TIER_DECISION
    assert event.details["tier"] == "standard"
