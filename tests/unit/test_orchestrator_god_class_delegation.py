"""Tests for audit-009: Orchestrator God-class delegation.

After the audit-009 refactor, the Orchestrator class methods for
evolve/cleanup/summary subsystems are thin delegators to free functions
in orchestrator_evolve.py and orchestrator_cleanup.py.

These tests verify:
1. The public method surface on Orchestrator is preserved (backward compat).
2. Each method body actually forwards to the module-level function
   so we don't accidentally carry a divergent copy.
3. Static / non-orch helpers still produce the same outputs.
"""

from __future__ import annotations

import inspect
from typing import Any
from unittest.mock import MagicMock

import pytest

from bernstein.core.orchestration import (
    orchestrator_cleanup,
    orchestrator_evolve,
)
from bernstein.core.orchestration.orchestrator import Orchestrator

# Each tuple is (method_name_on_class, module_level_function, module).
#
# These delegations were extracted from Orchestrator in audit-009 so the
# Orchestrator class can shrink below its 4496-line God-class footprint.
_EVOLVE_DELEGATIONS: list[tuple[str, str]] = [
    ("_check_evolve", "check_evolve"),
    ("_run_ruff_check", "run_ruff_check"),
    ("_create_ruff_tasks", "create_ruff_tasks"),
    ("_replenish_backlog", "replenish_backlog"),
    ("_run_pytest", "run_pytest"),
    ("_evolve_run_tests", "evolve_run_tests"),
    ("_evolve_auto_commit", "evolve_auto_commit"),
    ("_evolve_spawn_manager", "evolve_spawn_manager"),
    ("_log_evolve_cycle", "log_evolve_cycle"),
    ("make_evolution_loop", "make_evolution_loop"),
    ("_run_evolution_cycle", "run_evolution_cycle"),
    ("_persist_pending_proposals", "persist_pending_proposals"),
]


_CLEANUP_DELEGATIONS: list[tuple[str, str]] = [
    ("stop", "stop"),
    ("is_shutting_down", "is_shutting_down"),
    ("_drain_before_cleanup", "drain_before_cleanup"),
    ("_save_session_state", "save_session_state"),
    ("_cleanup", "cleanup"),
    ("_restart", "restart"),
]


@pytest.mark.parametrize(("method_name", "target"), _EVOLVE_DELEGATIONS)
def test_evolve_method_delegates_to_module(method_name: str, target: str) -> None:
    """Every evolve method on Orchestrator must forward to orchestrator_evolve.

    Verified by reading the source of the method and checking it references
    the module-level function name.  This guards against someone adding an
    inline copy of the body again.
    """
    method = getattr(Orchestrator, method_name)
    src = inspect.getsource(method)
    assert f"orchestrator_evolve.{target}" in src, (
        f"Orchestrator.{method_name} body does not delegate to "
        f"orchestrator_evolve.{target}: {src!r}"
    )
    assert hasattr(orchestrator_evolve, target), (
        f"orchestrator_evolve.{target} does not exist"
    )


@pytest.mark.parametrize(("method_name", "target"), _CLEANUP_DELEGATIONS)
def test_cleanup_method_delegates_to_module(method_name: str, target: str) -> None:
    """Every cleanup method on Orchestrator must forward to orchestrator_cleanup."""
    method = getattr(Orchestrator, method_name)
    src = inspect.getsource(method)
    assert f"orchestrator_cleanup.{target}" in src, (
        f"Orchestrator.{method_name} body does not delegate to "
        f"orchestrator_cleanup.{target}: {src!r}"
    )
    assert hasattr(orchestrator_cleanup, target), (
        f"orchestrator_cleanup.{target} does not exist"
    )


def test_evolve_focus_areas_still_on_class() -> None:
    """Tests access ``Orchestrator._EVOLVE_FOCUS_AREAS``; keep the ClassVar."""
    assert hasattr(Orchestrator, "_EVOLVE_FOCUS_AREAS")
    assert isinstance(Orchestrator._EVOLVE_FOCUS_AREAS, list)
    assert "new_features" in Orchestrator._EVOLVE_FOCUS_AREAS
    assert "code_quality" in Orchestrator._EVOLVE_FOCUS_AREAS


def test_replenish_constants_preserved() -> None:
    """Cooldown / max-task class constants stay on the class for back-compat."""
    assert pytest.approx(60.0) == Orchestrator._REPLENISH_COOLDOWN_S
    assert Orchestrator._REPLENISH_MAX_TASKS == 5


def test_generate_evolve_commit_msg_passes_through_staticmethod() -> None:
    """Static delegator must return the same string the module function does."""
    files = ["src/bernstein/cli/main.py", "docs/readme.md"]
    from_class = Orchestrator._generate_evolve_commit_msg(files)
    from_module = orchestrator_evolve.generate_evolve_commit_msg(files)
    assert from_class == from_module
    assert from_class.startswith("Evolve: ")


def test_generate_evolve_commit_msg_empty_files_housekeeping() -> None:
    """Empty input short-circuits to the fixed housekeeping label."""
    assert Orchestrator._generate_evolve_commit_msg([]) == "Evolve: housekeeping"


def test_stop_delegation_calls_module(monkeypatch: pytest.MonkeyPatch) -> None:
    """Orchestrator.stop must invoke orchestrator_cleanup.stop with self."""
    captured: dict[str, Any] = {}

    def fake_stop(orch: Any) -> None:
        captured["orch"] = orch

    monkeypatch.setattr(orchestrator_cleanup, "stop", fake_stop)

    fake_self = MagicMock()
    Orchestrator.stop(fake_self)

    assert captured.get("orch") is fake_self


def test_is_shutting_down_returns_module_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return value must flow through unmodified."""
    monkeypatch.setattr(
        orchestrator_cleanup,
        "is_shutting_down",
        lambda _orch: True,
    )
    assert Orchestrator.is_shutting_down(MagicMock()) is True

    monkeypatch.setattr(
        orchestrator_cleanup,
        "is_shutting_down",
        lambda _orch: False,
    )
    assert Orchestrator.is_shutting_down(MagicMock()) is False


def test_drain_before_cleanup_forwards_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """timeout_s keyword must reach the module function unchanged."""
    received: dict[str, Any] = {}

    def fake_drain(orch: Any, *, timeout_s: float | None = None) -> None:
        received["orch"] = orch
        received["timeout"] = timeout_s

    monkeypatch.setattr(orchestrator_cleanup, "drain_before_cleanup", fake_drain)

    fake_self = MagicMock()
    Orchestrator._drain_before_cleanup(fake_self, timeout_s=5.5)
    assert received == {"orch": fake_self, "timeout": 5.5}


def test_check_evolve_forwards_args(monkeypatch: pytest.MonkeyPatch) -> None:
    """Positional args for check_evolve must reach the module function."""
    received: dict[str, Any] = {}

    def fake_check_evolve(
        orch: Any,
        result: Any,
        tasks_by_status: dict[str, list[Any]],
    ) -> None:
        received["orch"] = orch
        received["result"] = result
        received["tasks_by_status"] = tasks_by_status

    monkeypatch.setattr(orchestrator_evolve, "check_evolve", fake_check_evolve)

    fake_self = MagicMock()
    fake_result = object()
    tasks = {"open": []}
    Orchestrator._check_evolve(fake_self, fake_result, tasks)

    assert received["orch"] is fake_self
    assert received["result"] is fake_result
    assert received["tasks_by_status"] is tasks


def test_make_evolution_loop_forwards_kwargs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """make_evolution_loop must pass through extra kwargs to the module."""
    captured: dict[str, Any] = {}

    def fake_make(orch: Any, **kwargs: Any) -> str:
        captured["orch"] = orch
        captured["kwargs"] = kwargs
        return "sentinel-loop"

    monkeypatch.setattr(orchestrator_evolve, "make_evolution_loop", fake_make)

    fake_self = MagicMock()
    result = Orchestrator.make_evolution_loop(fake_self, interval_s=123)
    assert result == "sentinel-loop"
    assert captured["orch"] is fake_self
    assert captured["kwargs"] == {"interval_s": 123}


def test_evolve_run_tests_returns_module_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return value of evolve_run_tests must propagate back through the method."""
    sentinel = {"passed": 1, "failed": 0, "summary": "ok"}
    monkeypatch.setattr(orchestrator_evolve, "evolve_run_tests", lambda _o: sentinel)
    assert Orchestrator._evolve_run_tests(MagicMock()) is sentinel


def test_run_ruff_check_returns_module_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ruff check output must be returned unchanged."""
    sentinel: list[dict[str, Any]] = [{"code": "E501"}]
    monkeypatch.setattr(orchestrator_evolve, "run_ruff_check", lambda _o: sentinel)
    assert Orchestrator._run_ruff_check(MagicMock()) is sentinel
