"""Cross-adapter model_id consistency checks (audit-140).

These tests guard against model-id drift between the Claude adapter — which
owns the canonical Opus pin — and the secondary adapters that wrap other
CLI tools (aider, amp, cody, goose).  Whenever the Claude adapter bumps
``opus`` to a new generation, the secondary adapters must follow in the
form each tool expects.

Rules enforced:

1. Every adapter's ``opus`` alias resolves to an identifier containing
   the canonical Opus generation string (``claude-opus-4-7``).
2. No adapter references the previous generation (``claude-opus-4-6``)
   anywhere in ``_MODEL_MAP`` *except* via an explicit ``opus-4-6``
   pinned-fallback key.
"""

from __future__ import annotations

import pytest

from bernstein.adapters.aider import _MODEL_MAP as AIDER_MAP
from bernstein.adapters.amp import _MODEL_MAP as AMP_MAP
from bernstein.adapters.claude import _MODEL_MAP as CLAUDE_MAP
from bernstein.adapters.cody import _MODEL_MAP as CODY_MAP
from bernstein.adapters.goose import _MODEL_MAP as GOOSE_MAP

# Canonical identifier segment every Anthropic-capable adapter must pin for
# ``opus``.  Bump this string together with ``claude.py::_MODEL_MAP["opus"]``.
CURRENT_OPUS = "claude-opus-4-7"
PREVIOUS_OPUS = "claude-opus-4-6"

# Anthropic-capable adapters (exclude openai/gemini-only providers).
ANTHROPIC_ADAPTERS: dict[str, dict[str, str]] = {
    "claude": CLAUDE_MAP,
    "aider": AIDER_MAP,
    "amp": AMP_MAP,
    "cody": CODY_MAP,
    "goose": GOOSE_MAP,
}


@pytest.mark.parametrize(("name", "model_map"), list(ANTHROPIC_ADAPTERS.items()))
def test_opus_alias_points_to_current_generation(name: str, model_map: dict[str, str]) -> None:
    """Every adapter's ``opus`` alias must resolve to the current Opus ID."""
    assert "opus" in model_map, f"{name} adapter missing 'opus' alias"
    resolved = model_map["opus"]
    assert CURRENT_OPUS in resolved, (
        f"{name} adapter 'opus' alias resolves to {resolved!r}, expected to contain {CURRENT_OPUS!r}. "
        "Bump the adapter to match claude.py's canonical Opus pin."
    )


@pytest.mark.parametrize(("name", "model_map"), list(ANTHROPIC_ADAPTERS.items()))
def test_no_stale_opus_except_pinned_fallback(name: str, model_map: dict[str, str]) -> None:
    """Previous-gen Opus may appear only under the explicit ``opus-4-6`` key."""
    offenders: list[tuple[str, str]] = []
    for alias, model_id in model_map.items():
        if PREVIOUS_OPUS in model_id and alias != "opus-4-6":
            offenders.append((alias, model_id))
    assert not offenders, (
        f"{name} adapter has stale {PREVIOUS_OPUS} references outside the pinned fallback: {offenders}. "
        "Only the 'opus-4-6' alias is allowed to pin the previous generation."
    )


def test_opus_ids_agree_on_generation_across_adapters() -> None:
    """All Anthropic-capable adapters agree on the same Opus generation."""
    pins = {name: mp["opus"] for name, mp in ANTHROPIC_ADAPTERS.items()}
    mismatches = {name: pin for name, pin in pins.items() if CURRENT_OPUS not in pin}
    assert not mismatches, f"Adapters disagree on Opus generation (expected {CURRENT_OPUS}): {mismatches}"
