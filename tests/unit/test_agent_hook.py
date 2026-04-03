"""Tests for T457 — agent hook with forked LLM context."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bernstein.plugins import hookimpl
from bernstein.plugins.hookspecs import BernsteinSpec
from bernstein.plugins.manager import PluginManager


class _AgentHookPlugin:
    """Fake plugin implementing on_agent_hook."""

    def __init__(self, result: dict[str, Any] | None) -> None:
        self._result = result
        self.call_count = 0
        self.last_session_id: str | None = None
        self.last_hook_name: str | None = None

    @hookimpl
    def on_agent_hook(
        self,
        session_id: str,
        hook_name: str,
        hook_input: dict[str, Any],
        conversation_context: list[dict[str, str]],
        model: str | None = None,
        max_tokens: int = 4096,
        timeout_seconds: float = 30.0,
    ) -> dict[str, Any] | None:
        self.call_count += 1
        self.last_session_id = session_id
        self.last_hook_name = hook_name
        return self._result


class TestAgentHookSpecExists:
    """Verify on_agent_hook hookspec is defined."""

    def test_hook_spec_defined(self) -> None:
        assert hasattr(BernsteinSpec, "on_agent_hook")

    def test_firstresult_marker(self) -> None:
        import inspect

        src = inspect.getsource(BernsteinSpec.on_agent_hook)
        assert "firstresult" in src


class TestFireAgentHook:
    """Test fire_agent_hook method on PluginManager."""

    @pytest.fixture()
    def mgr(self, tmp_path: Path) -> PluginManager:
        return PluginManager(workdir=tmp_path)

    def test_returns_none_without_plugins(self, mgr: PluginManager) -> None:
        result = mgr.fire_agent_hook(
            "sess1",
            "policy_check",
            {"action": "read"},
            [{"role": "user", "content": "Is this safe?"}],
        )
        assert result is None

    def test_returns_plugin_decision(self, mgr: PluginManager) -> None:
        plugin = _AgentHookPlugin({"decision": "allow", "reason": "looks safe"})
        mgr.register(plugin, name="test_hook")

        result = mgr.fire_agent_hook(
            "sess1",
            "policy_check",
            {"action": "read", "path": "src/foo.py"},
            [{"role": "user", "content": "Context"}],
        )
        assert result == {"decision": "allow", "reason": "looks safe"}
        assert plugin.call_count == 1
        assert plugin.last_session_id == "sess1"
        assert plugin.last_hook_name == "policy_check"

    def test_defaults_on_error(self, mgr: PluginManager) -> None:
        """When the hook raises, return safe deny default."""

        class FailingPlugin:
            @hookimpl
            def on_agent_hook(self, **_kw: Any) -> None:
                raise RuntimeError("LLM connection lost")

        mgr.register(FailingPlugin(), name="flaky")

        result = mgr.fire_agent_hook("sess1", "check", {}, [])
        assert result is not None
        assert result["decision"] == "deny"
        assert "timed_out_or_error" in result["reason"]

    def test_bounds_conversation_context(self, mgr: PluginManager) -> None:
        """Context longer than 20 messages should not cause errors."""
        plugin = _AgentHookPlugin({"decision": "allow"})
        mgr.register(plugin, name="ctx_hook")

        # 50 messages
        context = [{"role": "user", "content": f"msg-{i}"} for i in range(50)]
        result = mgr.fire_agent_hook("sess1", "summarize", {}, context)
        assert result == {"decision": "allow"}

    def test_short_context_passes(self, mgr: PluginManager) -> None:
        """Context shorter than 20 passes through unchanged."""
        plugin = _AgentHookPlugin({"decision": "allow"})
        mgr.register(plugin, name="short_ctx")

        context = [{"role": "user", "content": "hi"}]
        result = mgr.fire_agent_hook("sess1", "check", {}, context)
        assert result == {"decision": "allow"}
        assert plugin.call_count == 1

    def test_deny_decision(self, mgr: PluginManager) -> None:
        """Hook can return deny decision."""
        plugin = _AgentHookPlugin({"decision": "deny", "reason": "unsafe"})
        mgr.register(plugin, name="deny_plugin")

        result = mgr.fire_agent_hook("sess1", "policy", {}, [])
        assert result["decision"] == "deny"

    def test_ask_decision(self, mgr: PluginManager) -> None:
        """Hook can return ask decision."""
        plugin = _AgentHookPlugin({"decision": "ask", "reason": "needs review"})
        mgr.register(plugin, name="ask_plugin")

        result = mgr.fire_agent_hook("sess1", "policy", {}, [])
        assert result["decision"] == "ask"

    def test_hook_with_model_override(self, mgr: PluginManager) -> None:
        """Model parameter accepted without error."""
        plugin = _AgentHookPlugin({"decision": "allow"})
        mgr.register(plugin, name="model_plugin")
        # Should not raise even with model override
        result = mgr.fire_agent_hook("sess1", "check", {}, [], model="opus")
        assert result["decision"] == "allow"

    def test_hook_with_custom_budget(self, mgr: PluginManager) -> None:
        """Token budget parameter accepted without error."""
        plugin = _AgentHookPlugin({"decision": "allow"})
        mgr.register(plugin, name="budget_plugin")
        result = mgr.fire_agent_hook("sess1", "check", {}, [], max_tokens=16384)
        assert result["decision"] == "allow"

    def test_hook_with_timeout(self, mgr: PluginManager) -> None:
        """Timeout parameter accepted without error."""
        plugin = _AgentHookPlugin({"decision": "ask"})
        mgr.register(plugin, name="timeout_plugin")
        result = mgr.fire_agent_hook("sess1", "check", {}, [], timeout_seconds=60.0)
        assert result["decision"] == "ask"
