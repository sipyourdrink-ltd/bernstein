"""Unit tests for OAuth refresh on 401/403 errors."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from bernstein.core.spawner import AgentSpawner
from bernstein.core.models import Task, ModelConfig, AgentSession
from bernstein.adapters.base import SpawnError

def test_spawner_retries_after_auth_refresh(tmp_path: Path):
    tracker = MagicMock()
    tracker.scan_log_for_auth_error.return_value = True
    
    adapter = MagicMock()
    adapter.name.return_value = "test-adapter"
    adapter.supports_auth_refresh.return_value = True
    adapter.refresh_auth.return_value = True
    # First spawn fails, second succeeds
    adapter.spawn.side_effect = [SpawnError("Auth failed"), MagicMock(pid=123, log_path=tmp_path/"test.log")]
    
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    
    spawner = AgentSpawner(adapter, templates_dir, tmp_path)
    spawner._rate_limit_tracker = tracker
    # Inject adapter lookup
    spawner._get_adapter_by_name = MagicMock(return_value=adapter)
    spawner._infer_adapter_name_for_provider = MagicMock(return_value="test-adapter")
    
    tasks = [Task(id="T1", title="test", role="backend", description="test")]
    
    spawner._router = MagicMock()
    spawner._router.route.return_value = ModelConfig("sonnet", "high")
    
    with patch("pathlib.Path.exists", return_value=True):
        session = spawner.spawn_for_tasks(tasks)
        
    assert adapter.refresh_auth.called
    assert adapter.spawn.call_count == 2
    assert session.pid == 123

def test_spawner_fails_if_refresh_unsupported(tmp_path: Path):
    tracker = MagicMock()
    tracker.scan_log_for_auth_error.return_value = True
    
    adapter = MagicMock()
    adapter.name.return_value = "test-adapter"
    adapter.supports_auth_refresh.return_value = False
    adapter.spawn.side_effect = SpawnError("Auth failed")
    
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()

    spawner = AgentSpawner(adapter, templates_dir, tmp_path)
    spawner._rate_limit_tracker = tracker
    spawner._get_adapter_by_name = MagicMock(return_value=adapter)
    spawner._infer_adapter_name_for_provider = MagicMock(return_value="test-adapter")
    spawner._router = MagicMock()
    spawner._router.route.return_value = ModelConfig("sonnet", "high")

    tasks = [Task(id="T1", title="test", role="backend", description="test")]
    
    with patch("pathlib.Path.exists", return_value=True):
        with pytest.raises(SpawnError):
            spawner.spawn_for_tasks(tasks)
            
    assert not adapter.refresh_auth.called
    assert adapter.spawn.call_count == 1
