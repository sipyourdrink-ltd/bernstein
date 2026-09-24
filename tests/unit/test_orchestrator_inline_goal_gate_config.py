"""Unit test that inline goal should resolve gate config from bernstein.yaml."""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import socket
from pathlib import Path
from unittest.mock import patch

from bernstein.core.orchestration.bootstrap import BootstrapResult, bootstrap_from_goal, bootstrap_from_seed


def get_free_port() -> int:
    """Return a free port number."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _kill_bernstein_processes(result: BootstrapResult | None) -> None:
    """Kill the server and spawner processes from a BootstrapResult."""
    if result is None:
        return
    with contextlib.suppress(Exception):
        os.kill(result.server_pid, signal.SIGTERM)
    with contextlib.suppress(Exception):
        os.kill(result.spawner_pid, signal.SIGTERM)


def test_inline_goal_resolves_gates_from_yaml(tmp_path: Path) -> None:
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

    port1 = get_free_port()
    port2 = get_free_port()

    # Mock preflight_checks to skip binary existence check (claude not required for test)
    with patch("bernstein.core.orchestration.bootstrap.preflight_checks"):
        # Run without inline goal (from seed file)
        result_from_seed = None
        try:
            result_from_seed = bootstrap_from_seed(seed_yaml, workdir=tmp_path, port=port1)
            seed_from_seed = result_from_seed.seed
        finally:
            _kill_bernstein_processes(result_from_seed)
            # Clean up .sdd to remove PID file and reset state for second run
            sdd_dir = tmp_path / ".sdd"
            if sdd_dir.exists():
                shutil.rmtree(sdd_dir)

        # Run with inline goal (same goal as in the yaml)
        result_from_goal = None
        try:
            result_from_goal = bootstrap_from_goal(
                goal="test",
                workdir=tmp_path,
                cli="claude",
                model=None,
                port=port2,
            )
            seed_from_goal = result_from_goal.seed
        finally:
            _kill_bernstein_processes(result_from_goal)
            # Clean up .sdd after second run (not strictly necessary for test, but good practice)
            sdd_dir = tmp_path / ".sdd"
            if sdd_dir.exists():
                shutil.rmtree(sdd_dir)

    # They should have the same quality_gates
    assert seed_from_seed.quality_gates == seed_from_goal.quality_gates

    # Also, we can check that the quality_gates are not None and have the expected values
    assert seed_from_goal.quality_gates is not None
    assert seed_from_goal.quality_gates.enabled is True  # type: ignore[union-attr]


def test_trace_export_accepts_result(tmp_path: Path) -> None:
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

    port = get_free_port()
    result = None
    with patch("bernstein.core.orchestration.bootstrap.preflight_checks"):
        try:
            result = bootstrap_from_goal(
                goal="trace test",
                workdir=tmp_path,
                cli="claude",
                model=None,
                port=port,
            )
        finally:
            _kill_bernstein_processes(result)
            # Clean up .sdd
            sdd_dir = tmp_path / ".sdd"
            if sdd_dir.exists():
                shutil.rmtree(sdd_dir)

    # The BootstrapResult must contain a seed (used for tracing)
    assert result.seed is not None
    # The seed must have quality_gates (even if None or False)
    assert hasattr(result.seed, "quality_gates")
