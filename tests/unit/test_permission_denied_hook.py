"""Unit tests for permission denied hooks and retry hints."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from bernstein.core.guardrails import run_guardrails, GuardrailsConfig
from bernstein.core.models import Task
from bernstein.plugins import hookimpl

class HintPlugin:
    @hookimpl
    def on_permission_denied(self, task_id, reason, tool, args):
        if tool == "secret_detection":
            return "Try rotating your credentials."
        return None

def test_guardrail_triggers_hook_and_appends_hint(tmp_path: Path):
    # Setup plugin manager with our test plugin
    from bernstein.plugins.manager import PluginManager
    pm = PluginManager()
    pm._pm.register(HintPlugin())
    
    # Mock get_plugin_manager to return our custom pm
    with patch("bernstein.plugins.manager.get_plugin_manager", return_value=pm):
        # Diff that triggers secret detection
        diff = "+AWS_KEY=AKIAIOSFODNN7EXAMPLE\n"
        task = Task(id="T1", title="test", role="backend", description="test")
        config = GuardrailsConfig(secrets=True)
        
        results = run_guardrails(diff, task, config, tmp_path)
        
        secret_res = next(r for r in results if r.check == "secret_detection")
        assert secret_res.blocked is True
        # Hint from plugin should be appended
        assert "Retry Hint: Try rotating your credentials." in secret_res.detail

def test_hook_not_triggered_on_allow(tmp_path: Path):
    from bernstein.plugins.manager import PluginManager
    pm = PluginManager()
    
    class MockPlugin:
        def __init__(self):
            self.mock = MagicMock(return_value="should not happen")
        @hookimpl
        def on_permission_denied(self, **kwargs):
            return self.mock(**kwargs)
            
    plugin = MockPlugin()
    pm._pm.register(plugin)

    with patch("bernstein.plugins.manager.get_plugin_manager", return_value=pm):
        # Clean diff
        diff = "+def ok(): pass\n"
        task = Task(id="T1", title="test", role="backend", description="test")
        config = GuardrailsConfig(secrets=True)
        
        results = run_guardrails(diff, task, config, tmp_path)
        
        # Hook should NOT be called for ALLOW
        assert not plugin.mock.called
