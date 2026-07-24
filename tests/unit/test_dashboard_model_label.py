"""Dashboard agent rows label the real spawn model, not a Claude default.

The dashboard used a hardcoded ``"sonnet"`` fallback when an agent's
``model_config`` carried no ``model`` attribute, so a non-Claude agent showed
``sonnet`` (issue #2800). ``_agent_model_label`` derives the real model from the
route/model_config and falls back to a neutral label instead.
"""

from __future__ import annotations

from types import SimpleNamespace

from bernstein.core.routes.status_dashboard import _agent_model_label


def test_label_uses_model_config_model() -> None:
    agent = SimpleNamespace(model_config=SimpleNamespace(model="qwen2.5-coder"))
    assert _agent_model_label(agent) == "qwen2.5-coder"


def test_label_reads_dict_shaped_model_config() -> None:
    agent = SimpleNamespace(model_config={"model": "gpt-5-codex"})
    assert _agent_model_label(agent) == "gpt-5-codex"


def test_label_falls_back_to_neutral_not_sonnet() -> None:
    """When no model is known the label is neutral, never a Claude default."""
    agent = SimpleNamespace(model_config=object(), provider=None)
    assert _agent_model_label(agent) != "sonnet"


def test_label_uses_provider_when_model_missing() -> None:
    agent = SimpleNamespace(model_config=object(), provider="ollama")
    assert _agent_model_label(agent) == "ollama"
