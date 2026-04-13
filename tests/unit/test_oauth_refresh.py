"""Unit tests for OAuth refresh on 401/403 errors.

The spawner's error classifier marks auth failures as NO_RETRY, so
spawn_for_tasks raises RuntimeError immediately.  The auth-refresh
path (T499) only activates for errors classified as retryable.  These
tests verify the current fail-fast behaviour is consistent.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from bernstein.core.models import ModelConfig, Task
from bernstein.core.spawner import AgentSpawner

from bernstein.adapters.base import SpawnError


def _make_spawner(tmp_path: Path, adapter: MagicMock) -> AgentSpawner:
    """Create an AgentSpawner with a git-initialized tmp_path."""
    subprocess.run(["git", "init", str(tmp_path)], capture_output=True, check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@test.local"],
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Test"],
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "--allow-empty", "-m", "init"],
        capture_output=True,
        check=True,
    )
    templates_dir = tmp_path / "templates"
    backend_dir = templates_dir / "backend"
    backend_dir.mkdir(parents=True)
    (backend_dir / "system_prompt.md").write_text("You are a backend agent.")

    spawner = AgentSpawner(adapter, templates_dir, tmp_path)
    spawner._get_adapter_by_name = MagicMock(return_value=adapter)
    spawner._infer_adapter_name_for_provider = MagicMock(return_value="test-adapter")
    spawner._router = MagicMock()
    spawner._router.route.return_value = ModelConfig("sonnet", "high")
    return spawner


def test_auth_error_is_non_retryable(tmp_path: Path) -> None:
    """Auth failures are classified NO_RETRY — spawn_for_tasks raises immediately."""
    adapter = MagicMock()
    adapter.name.return_value = "test-adapter"
    adapter.spawn.side_effect = SpawnError("Auth failed")

    spawner = _make_spawner(tmp_path, adapter)
    tasks = [Task(id="T1", title="test", role="backend", description="test")]

    with pytest.raises(RuntimeError, match="All spawn attempts failed"):
        spawner.spawn_for_tasks(tasks)

    # Auth error is fail-fast — only 1 attempt, no retry
    assert adapter.spawn.call_count == 1


def test_spawner_fails_if_refresh_unsupported(tmp_path: Path) -> None:
    """When adapter doesn't support refresh, auth errors still fail fast."""
    adapter = MagicMock()
    adapter.name.return_value = "test-adapter"
    adapter.supports_auth_refresh.return_value = False
    adapter.spawn.side_effect = SpawnError("Auth failed")

    spawner = _make_spawner(tmp_path, adapter)
    tasks = [Task(id="T1", title="test", role="backend", description="test")]

    with pytest.raises(RuntimeError, match="All spawn attempts failed"):
        spawner.spawn_for_tasks(tasks)

    assert not adapter.refresh_auth.called
    assert adapter.spawn.call_count == 1
