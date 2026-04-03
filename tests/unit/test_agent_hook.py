"""Tests for T457 — agent hook with forked LLM context."""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.plugins import hookimpl
from bernstein.plugins.hookspecs import BernsteinSpec
from bernstein.plugins.manager import PluginManager


class _AgentHookPlugin:
    """Fake plugin implementing on_agent_hook."""

    def __init__(self, result: dict | None, raise_exc: Exception | None = None) -> None:
        self._result = result
        self._raise_exc = raise_exc
        self.calls: list[dict] = []

    @hookimpl
    def on_agent_hook(
        self,
        session_id: str,
        hook_name: str,
        hook_input: dict,
        conversation_context: list,
        model: str | None = None,
        max_tokens: int = 4096,
        timeout_seconds: float = 30.0,
    ) -> dict | None:
        self.calls.append(
            {
                "session_id": session_id,
                "hook_name": hook_name,
                "hook_input": hook_input,
                "conversation_context": conversation_context,
                "model": model,
                "max_tokens": max_tokens,
                "timeout_seconds": timeout_seconds,
            }
        )
        if self._raise_exc:
            raise self._raise_exc
        return self._result


class TestAgentHookSpecExists:
    """Verify on_agent_hook hookspec is defined."""

    def test_hook_spec_defined(self) -> None:
        assert hasattr(BernsteinSpec, "on_agent_hook")

    def test_firstresult_marker(self) -> None:
        """on_agent_hook should be firstresult (single decision winner)."""
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

    def test_calls_plugin_hook(self, mgr: PluginManager) -> None:
        plugin = _AgentHookPlugin({"decision": "allow", "reason": "looks safe"})
        mgr.register(plugin, name="test_hook")

        result = mgr.fire_agent_hook(
            "sess1",
            "policy_check",
            {"action": "read", "path": "src/foo.py"},
            [{"role": "user", "content": "Context"}],
        )
        assert result == {"decision": "allow", "reason": "looks safe"}
        assert len(plugin.calls) == 1
        assert plugin.calls[0]["session_id"] == "sess1"
        assert plugin.calls[0]["hook_name"] == "policy_check"

    def test_defaults_on_error(self, mgr: PluginManager) -> None:
        """When the hook raises, return safe deny default."""
        plugin = _AgentHookPlugin(None, raise_exc=RuntimeError("LLM connection lost"))
        mgr.register(plugin, name="flaky_hook")

        result = mgr.fire_agent_hook("sess1", "check", {}, [])
        assert result is not None
        assert result["decision"] == "deny"
        assert "timed_out_or_error" in result["reason"]

    def test_bounds_conversation_context(self, mgr: PluginManager) -> None:
        """Context longer than 20 messages should be truncated."""
        plugin = _AgentHookPlugin({"decision": "allow"})
        mgr.register(plugin, name="ctx_hook")

        context = [{"role": "user", "content": f"msg-{i}"} for i in range(30)]
        mgr.fire_agent_hook("sess1", "summarize", {}, context)

        assert len(plugin.calls) == 1
        passed_context = plugin.calls[0]["conversation_context"]
        assert len(passed_context) == 20
        assert passed_context[0]["content"] == "msg-10"

    def test_short_context_not_truncated(self, mgr: PluginManager) -> None:
        """Context shorter than 20 passes through unchanged."""
        plugin = _AgentHookPlugin({"decision": "allow"})
        mgr.register(plugin, name="short_ctx")

        context = [{"role": "user", "content": "only three"} for _ in range(3)]
        mgr.fire_agent_hook("sess1", "check", {}, context)

        assert len(plugin.calls[0]["conversation_context"]) == 3

    def test_model_override_passed(self, mgr: PluginManager) -> None:
        """Override model should be forwarded to the hook."""
        plugin = _AgentHookPlugin({"decision": "allow"})
        mgr.register(plugin, name="model_override")

        mgr.fire_agent_hook("sess1", "check", {}, [], model="opus")

        assert plugin.calls[0]["model"] == "opus"

    def test_token_budget_passed(self, mgr: PluginManager) -> None:
        """max_tokens should be forwarded to the hook."""
        plugin = _AgentHookPlugin({"decision": "allow"})
        mgr.register(plugin, name="budget_override")

        mgr.fire_agent_hook("sess1", "check", {}, [], max_tokens=8192)

        assert plugin.calls[0]["max_tokens"] == 8192

    def test_timeout_passed(self, mgr: PluginManager) -> None:
        """timeout_seconds should be forwarded to the hook."""
        plugin = _AgentHookPlugin({"decision": "ask"})
        mgr.register(plugin, name="timeout_override")

        mgr.fire_agent_hook("sess1", "check", {}, [], timeout_seconds=60.0)

        assert plugin.calls[0]["timeout_seconds"] == 60.0

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
