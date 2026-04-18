"""Regression test for audit-062: compactor consolidation.

Three parallel token-compactor implementations previously coexisted:
``auto_compact.py``, ``claude_auto_compact.py``, and
``compaction_pipeline.py``.  Only ``compaction_pipeline`` is called in
production (``agent_lifecycle._try_compact_and_retry``); the circuit
breaker lives in ``token_monitor.AutoCompactCircuitBreaker``.

This test pins the single-source invariant: the dead modules must no
longer be importable, either through their real path or through the
``bernstein.core.<old_name>`` redirect shim.
"""

from __future__ import annotations

import importlib

import pytest

# ---------------------------------------------------------------------------
# The canonical compactor must still be importable.
# ---------------------------------------------------------------------------


def test_compaction_pipeline_is_importable() -> None:
    """``CompactionPipeline`` is the single surviving compactor module."""
    module = importlib.import_module("bernstein.core.tokens.compaction_pipeline")
    assert hasattr(module, "CompactionPipeline")
    assert hasattr(module, "CompactionResult")


def test_compaction_pipeline_redirect_shim_works() -> None:
    """The ``bernstein.core.compaction_pipeline`` back-compat shim resolves."""
    module = importlib.import_module("bernstein.core.compaction_pipeline")
    assert hasattr(module, "CompactionPipeline")


def test_token_monitor_exposes_circuit_breaker() -> None:
    """Circuit breaker lives in token_monitor, not a dead compactor module."""
    module = importlib.import_module("bernstein.core.tokens.token_monitor")
    assert hasattr(module, "AutoCompactCircuitBreaker")
    assert hasattr(module, "CircuitState")


# ---------------------------------------------------------------------------
# The dead modules must not be importable (audit-062).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "dead_module",
    [
        "bernstein.core.tokens.auto_compact",
        "bernstein.core.tokens.claude_auto_compact",
        "bernstein.core.auto_compact",
        "bernstein.core.claude_auto_compact",
    ],
)
def test_dead_compactor_modules_raise_module_not_found(dead_module: str) -> None:
    """Parallel compactor implementations are gone; importing them fails."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(dead_module)


def test_redirect_map_has_no_dead_compactor_entries() -> None:
    """The legacy ``_REDIRECT_MAP`` no longer routes to the deleted modules."""
    from bernstein.core import _REDIRECT_MAP

    assert "auto_compact" not in _REDIRECT_MAP
    assert "claude_auto_compact" not in _REDIRECT_MAP
    # Canonical compactor redirect is still present.
    assert _REDIRECT_MAP["compaction_pipeline"] == "bernstein.core.tokens.compaction_pipeline"
