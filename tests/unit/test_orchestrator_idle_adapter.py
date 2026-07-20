"""Adapter-resolution precedence for the orchestrator __main__ launch path.

Regression coverage for the ``bernstein run --idle`` contract: the launcher
exports ``BERNSTEIN_ADAPTER=mock`` (and passes ``--adapter mock``) so the
orchestrator subprocess never spawns real Claude agents. That contract broke
because the orchestrator overrode the explicit adapter with the seed's ``cli``
field, which defaults to ``auto`` -> Claude, whenever a ``bernstein.yaml`` was
present (i.e. always).
"""

from __future__ import annotations

from bernstein.core.orchestration.orchestrator import _resolve_spawner_adapter_name


def test_explicit_adapter_wins_over_seed_cli_auto_default() -> None:
    """--idle exports BERNSTEIN_ADAPTER=mock; the seed's ``auto`` must not win.

    This is the exact failure: a plain ``bernstein.yaml`` leaves ``cli`` at
    ``auto``. If ``auto`` overrides the explicit ``mock`` the orchestrator
    spawns real Claude agents (401 / token burn) despite ``--idle``.
    """
    assert _resolve_spawner_adapter_name("mock", "auto") == "mock"


def test_explicit_adapter_wins_over_configured_seed_cli() -> None:
    """An explicit ``--adapter``/env choice beats even a non-default seed cli."""
    assert _resolve_spawner_adapter_name("claude", "codex") == "claude"


def test_seed_cli_used_when_no_explicit_adapter() -> None:
    """Normal runs (no --adapter, no env) still honour the seed's cli."""
    assert _resolve_spawner_adapter_name(None, "codex") == "codex"
    assert _resolve_spawner_adapter_name("", "auto") == "auto"


def test_no_adapter_configured_returns_none() -> None:
    """Neither source configured -> None (caller treats as fatal misconfig)."""
    assert _resolve_spawner_adapter_name(None, None) is None
    assert _resolve_spawner_adapter_name("", "") is None


def test_whitespace_only_values_are_ignored() -> None:
    """Blank/whitespace explicit adapter falls back to the seed cli."""
    assert _resolve_spawner_adapter_name("   ", "codex") == "codex"
