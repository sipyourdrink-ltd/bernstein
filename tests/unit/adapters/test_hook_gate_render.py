"""Adapter-side in-process gate capability + hook rendering (issue #2360).

The in-process gate is a per-adapter capability: an adapter with a blocking
hook surface (Claude Code family) renders a completion gate plus a
tool-permission matcher into its worker config; an adapter without one renders
nothing and degrades to the authoritative scheduler-side gate with no policy
weakening (AC4).
"""

from __future__ import annotations

from bernstein.adapters._contract import (
    IN_PROCESS_GATE_CAPABILITY_MATRIX,
    STRATEGY_MATRIX,
    InProcessGateCapability,
    in_process_gate_capability,
)
from bernstein.adapters.hook_gate_render import gate_capable, render_gate_hooks

# ---------------------------------------------------------------------------
# Capability map
# ---------------------------------------------------------------------------


def test_claude_family_is_blocking_capable() -> None:
    assert in_process_gate_capability("claude") is InProcessGateCapability.BLOCKING
    assert in_process_gate_capability("claude_routine") is InProcessGateCapability.BLOCKING


def test_non_capable_adapters_degrade_to_none() -> None:
    assert in_process_gate_capability("gemini") is InProcessGateCapability.NONE
    assert in_process_gate_capability("mock") is InProcessGateCapability.NONE
    assert in_process_gate_capability("opencode") is InProcessGateCapability.NONE


def test_unknown_adapter_defaults_to_none() -> None:
    assert in_process_gate_capability("totally-made-up") is InProcessGateCapability.NONE


def test_capability_matrix_covers_every_declared_adapter() -> None:
    # Derived, never hand-maintained: one row per adapter in the strategy matrix.
    assert set(IN_PROCESS_GATE_CAPABILITY_MATRIX) == set(STRATEGY_MATRIX)


def test_namespace_alias_resolves() -> None:
    # The session-namespace form resolves to the same capability.
    assert in_process_gate_capability("claude code") is InProcessGateCapability.BLOCKING


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


def test_render_gate_hooks_for_capable_adapter() -> None:
    hooks = render_gate_hooks("claude", session_id="qa-abc12345")
    assert hooks is not None
    assert "PreToolUse" in hooks
    assert "Stop" in hooks
    # The PreToolUse matcher targets the write tools.
    matcher = hooks["PreToolUse"][0]["matcher"]
    for tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        assert tool in matcher
    # Both hooks invoke the in-process gate CLI carrying the session id.
    stop_cmd = hooks["Stop"][0]["hooks"][0]["command"]
    assert "hook-gate" in stop_cmd
    assert "qa-abc12345" in stop_cmd
    pre_cmd = hooks["PreToolUse"][0]["hooks"][0]["command"]
    assert "hook-gate" in pre_cmd


def test_render_gate_hooks_none_for_incapable_adapter() -> None:
    # AC4: no blocking surface -> nothing injected, scheduler-side gate intact.
    assert render_gate_hooks("gemini", session_id="qa-abc12345") is None
    assert render_gate_hooks("mock", session_id="qa-abc12345") is None


def test_gate_capable_predicate() -> None:
    assert gate_capable("claude") is True
    assert gate_capable("gemini") is False
