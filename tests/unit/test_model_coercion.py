"""Adapter-aware model coercion for non-Claude adapters (issue #2075).

The batch/heuristic selector emits Claude cascade tier names (opus/sonnet/haiku)
with no adapter awareness. For a non-Claude adapter that produces an invalid
``-m`` value (e.g. ``codex exec -m opus``) and records a model that never ran.
``_coerce_model_for_non_claude_adapter`` normalises the selection so the recorded
and executed model agree, while leaving the Claude path byte-identical.
"""

from __future__ import annotations

from bernstein.core.models import ModelConfig

from bernstein.core.agents.spawner_warm_pool import _coerce_model_for_non_claude_adapter


def test_claude_tier_replaced_with_adapter_default_for_codex() -> None:
    out = _coerce_model_for_non_claude_adapter(
        ModelConfig(model="opus", effort="max"),
        adapter_name="Codex",
        adapter_default_model="gpt-5.4",
    )
    assert out.model == "gpt-5.4"
    # Effort and other fields are preserved.
    assert out.effort == "max"


def test_claude_adapter_left_unchanged() -> None:
    cfg = ModelConfig(model="opus", effort="max")
    out = _coerce_model_for_non_claude_adapter(
        cfg,
        adapter_name="claude",
        adapter_default_model="gpt-5.4",
    )
    assert out.model == "opus"


def test_non_tier_model_passed_through() -> None:
    out = _coerce_model_for_non_claude_adapter(
        ModelConfig(model="gpt-5.4", effort="high"),
        adapter_name="Codex",
        adapter_default_model="gpt-5.4",
    )
    assert out.model == "gpt-5.4"


def test_no_default_leaves_model_unchanged() -> None:
    out = _coerce_model_for_non_claude_adapter(
        ModelConfig(model="sonnet", effort="high"),
        adapter_name="Codex",
        adapter_default_model=None,
    )
    assert out.model == "sonnet"


def test_run_level_default_model_coerces_haiku_for_qwen() -> None:
    """The run-level model (e.g. threaded from ``bernstein run --model``
    through AgentSpawner's ``default_model``) is a valid ``adapter_default_model``
    source, same as an adapter class attribute - see spawner_core.py's call site."""
    out = _coerce_model_for_non_claude_adapter(
        ModelConfig(model="haiku", effort="normal"),
        adapter_name="qwen",
        adapter_default_model="MiniMax-M3",
    )
    assert out.model == "MiniMax-M3"
