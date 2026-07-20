"""The internal-LLM preflight notice is emitted at most once per process.

``bernstein gui serve`` parses the seed twice (``create_app`` reads CORS, then
the app-startup reload), and every ``SeedConfig`` construction used to log the
same ``internal_llm_provider`` notice -- so operators saw it twice on startup.
"""

from __future__ import annotations

import logging

import pytest

from bernstein.core.config.seed_config import (
    SeedConfig,
    check_internal_llm_preflight,
    reset_internal_llm_preflight_cache,
    warn_internal_llm_preflight_once,
)

_MODULE = "bernstein.core.config.seed_config"


@pytest.fixture(autouse=True)
def _clear_env_and_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY_FREE", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY_PAID", raising=False)
    reset_internal_llm_preflight_cache()


def _preflight_warnings(records: list[logging.LogRecord]) -> list[logging.LogRecord]:
    return [r for r in records if r.name == _MODULE and "preflight" in r.getMessage()]


def test_repeated_seed_construction_warns_once(caplog: pytest.LogCaptureFixture) -> None:
    """Two SeedConfig constructions (the gui-serve shape) -> one warning."""
    with caplog.at_level(logging.WARNING, logger=_MODULE):
        SeedConfig(goal="demo", internal_llm_provider="openrouter_free")
        SeedConfig(goal="demo", internal_llm_provider="openrouter_free")
    assert len(_preflight_warnings(caplog.records)) == 1


def test_warn_once_helper_dedupes(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger=_MODULE):
        first = warn_internal_llm_preflight_once("openrouter_free")
        second = warn_internal_llm_preflight_once("openrouter_free")
    # Hint text is returned both times so callers can still react...
    assert first is not None
    assert second == first
    # ...but only the first call logs.
    assert len(_preflight_warnings(caplog.records)) == 1


def test_no_warning_when_provider_is_none(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger=_MODULE):
        SeedConfig(goal="demo", internal_llm_provider="none")
    assert _preflight_warnings(caplog.records) == []


def test_reset_cache_allows_reemission(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger=_MODULE):
        warn_internal_llm_preflight_once("openrouter_free")
        reset_internal_llm_preflight_cache()
        warn_internal_llm_preflight_once("openrouter_free")
    assert len(_preflight_warnings(caplog.records)) == 2


def test_message_is_calm_and_actionable() -> None:
    """Hint states the fact and both remedies without alarmist phrasing."""
    hint = check_internal_llm_preflight("openrouter_free")
    assert hint is not None
    assert "internal_llm_provider: none" in hint
    assert "OPENROUTER_API_KEY_FREE" in hint
    # No alarmist verbs.
    lowered = hint.lower()
    assert "crash" not in lowered
    assert "fatal" not in lowered
