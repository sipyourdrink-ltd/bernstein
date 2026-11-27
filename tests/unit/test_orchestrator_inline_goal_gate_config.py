"""Unit test that inline goal should resolve gate config from bernstein.yaml."""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import Mock

import pytest

from bernstein.core.orchestration import bootstrap
from bernstein.core.orchestration.bootstrap import bootstrap_from_goal, bootstrap_from_seed


@pytest.fixture
def fake_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run bootstrap with no server, planning requests, or child processes."""
    for name, result in {
        "preflight_checks": None,
        "_acquire_pid_lock": None,
        "_clean_stale_runtime": None,
        "_reconcile_dead_owner_before_runtime_cleanup": None,
        "_run_git_hygiene": None,
        "_discover_catalog": None,
        "_index_codebase_with_timeout": None,
        "_build_codebase_index": None,
        "_resolve_auth_token": "bootstrap-test-token",
        "supervised_server": 101,
        "_wait_for_server": True,
        "_sync_and_plan_tasks": (0, None, None),
        "_goal_sync_and_plan": (0, None, None),
        "_start_spawner": 102,
        "_start_watchdog": 103,
        "_register_mcp_discovery": None,
    }.items():
        monkeypatch.setattr(bootstrap, name, Mock(return_value=result))


def test_inline_goal_resolves_gates_from_yaml(tmp_path: Path, fake_launch: None) -> None:
    """Two runs over the same project (inline goal and seed file) record the same gate configuration."""
    # Create a bernstein.yaml with quality_gates
    seed_yaml = tmp_path / "bernstein.yaml"
    seed_yaml.write_text(
        """goal: "test"
cli: claude
max_agents: 1
quality_gates:
  enabled: true
  lint: true
  lint_command: "echo lint"
  tests: true
  test_command: "echo test"
"""
    )

    result_from_seed = bootstrap_from_seed(seed_yaml, workdir=tmp_path, port=8052)
    seed_from_seed = result_from_seed.seed
    shutil.rmtree(tmp_path / ".sdd")

    result_from_goal = bootstrap_from_goal(goal="test", workdir=tmp_path, cli="claude", model=None, port=8052)
    seed_from_goal = result_from_goal.seed

    # They should have the same quality_gates
    assert seed_from_seed.quality_gates == seed_from_goal.quality_gates

    # Also, we can check that the quality_gates are not None and have the expected values
    assert seed_from_goal.quality_gates is not None
    assert seed_from_goal.quality_gates.enabled is True  # type: ignore[union-attr]


def test_trace_export_accepts_result(tmp_path: Path, fake_launch: None) -> None:
    """Trace export accepts the result (seed is present in BootstrapResult)."""
    seed_yaml = tmp_path / "bernstein.yaml"
    seed_yaml.write_text(
        """goal: "trace test"
cli: claude
max_agents: 1
quality_gates:
  enabled: false
"""
    )

    result = bootstrap_from_goal(goal="trace test", workdir=tmp_path, cli="claude", model=None, port=8052)

    # The BootstrapResult must contain a seed (used for tracing)
    assert result.seed is not None
    # The seed must have quality_gates (even if None or False)
    assert hasattr(result.seed, "quality_gates")
