"""The openai_agents runner sends the manifest's effort as ``reasoning.effort`` (#8076).

The spawner resolved ``effort=max`` from ``role_model_policy`` and logged it, but the runner built
the SDK ``Agent`` without ``ModelSettings.reasoning``. The Responses request then carried no
``reasoning.effort``, and the provider default applied.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any

import pytest

from bernstein.adapters.openai_agents_runner import RunnerManifest, _build_model_settings_kwargs


@dataclasses.dataclass
class _ModelSettings:
    """A stand-in for the SDK's ``ModelSettings`` that exposes ``reasoning``."""

    max_tokens: int | None = None
    reasoning: Any = None


@dataclasses.dataclass
class _OldModelSettings:
    """An SDK ``ModelSettings`` that predates ``reasoning``."""

    max_tokens: int | None = None


def _manifest(model: str = "gpt-6.2-astra", effort: str = "max", **extra: Any) -> RunnerManifest:
    return RunnerManifest(session_id="s", prompt="p", workdir="/workspace", model=model, effort=effort, **extra)


def _effort(kwargs: dict[str, Any]) -> str | None:
    reasoning = kwargs.get("reasoning")
    return None if reasoning is None else reasoning.effort


@pytest.mark.parametrize(
    ("effort", "sent"),
    [
        ("max", "max"),
        ("xhigh", "xhigh"),
        ("high", "high"),
        ("medium", "medium"),
        ("normal", "medium"),
        ("low", "low"),
        ("minimal", "minimal"),
        ("none", "none"),
    ],
)
def test_the_request_kwargs_carry_the_configured_effort(effort: str, sent: str) -> None:
    kwargs = _build_model_settings_kwargs(_manifest(effort=effort), model_settings_cls=_ModelSettings)
    assert _effort(kwargs) == sent


def test_the_effort_sent_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="bernstein.adapters.openai_agents_runner"):
        _build_model_settings_kwargs(_manifest(effort="max"), model_settings_cls=_ModelSettings)
    assert any("reasoning effort=max" in rec.getMessage() for rec in caplog.records)


def test_an_unsupported_effort_fails_before_the_call() -> None:
    with pytest.raises(ValueError, match="turbo"):
        _build_model_settings_kwargs(_manifest(effort="turbo"), model_settings_cls=_ModelSettings)


def test_a_model_without_reasoning_gets_no_effort() -> None:
    # gpt-4.1 rejects a reasoning parameter, so sending the default "high" would break every call.
    kwargs = _build_model_settings_kwargs(_manifest(model="gpt-4.1", effort="high"), model_settings_cls=_ModelSettings)
    assert "reasoning" not in kwargs


def test_a_custom_endpoint_gets_no_effort() -> None:
    kwargs = _build_model_settings_kwargs(
        _manifest(model="gpt-5", effort="high"),
        model_settings_cls=_ModelSettings,
        base_url="https://example.invalid/v1",
    )
    assert "reasoning" not in kwargs


def test_an_sdk_without_the_field_gets_no_effort() -> None:
    assert "reasoning" not in _build_model_settings_kwargs(_manifest(), model_settings_cls=_OldModelSettings)
    assert "reasoning" not in _build_model_settings_kwargs(_manifest())
