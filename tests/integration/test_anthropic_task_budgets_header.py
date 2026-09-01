"""Integration: ClaudeCodeAdapter propagates the task-budgets beta header.

The Anthropic Opus 4.7 ``task-budgets-2026-03-13`` header is gated
behind ``BERNSTEIN_ANTHROPIC_TASK_BUDGETS``. Bernstein cannot patch the
HTTP request because the Claude Code CLI shells out internally - the
documented channel is the ``ANTHROPIC_BETA`` env var which the upstream
SDK forwards onto every API call. These tests assert the env var is
propagated correctly when (and only when) the operator opts in.
"""

from __future__ import annotations

import subprocess
from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters.claude import ClaudeCodeAdapter
from bernstein.core.cost.budget_countdown import TASK_BUDGETS_OPT_IN_ENV
from bernstein.core.identity.agent_card import TASK_BUDGETS_BETA_HEADER


def _make_popen_mock(pid: int) -> MagicMock:
    """Return a minimal Popen mock with a stub stdout (mirrors unit tests)."""
    m = MagicMock(spec=subprocess.Popen)
    m.pid = pid
    m.stdout = MagicMock()
    return m


@pytest.fixture(autouse=True)
def _clean_state() -> Generator[None, None, None]:
    ClaudeCodeAdapter._procs.clear()
    ClaudeCodeAdapter._wrapper_pids.clear()
    yield
    ClaudeCodeAdapter._procs.clear()
    ClaudeCodeAdapter._wrapper_pids.clear()


def _spawn_and_capture_env(tmp_path: Path) -> dict[str, str]:
    """Spawn a fake claude run and return the env that was passed in."""
    adapter = ClaudeCodeAdapter()
    claude_mock = _make_popen_mock(pid=4242)
    wrapper_mock = _make_popen_mock(pid=4243)

    with patch("bernstein.adapters.claude.subprocess.Popen", side_effect=[claude_mock, wrapper_mock]) as popen:
        adapter.spawn(
            prompt="do thing",
            workdir=tmp_path,
            model_config=ModelConfig(model="opus", effort="high"),
            session_id="backend-deadbeef",
        )
        first_call_kwargs = popen.call_args_list[0].kwargs
        env = first_call_kwargs.get("env")
        assert isinstance(env, dict)
        return env


def test_beta_header_env_set_when_opt_in_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ANTHROPIC_BETA`` propagates the documented value when opt-in is on."""
    monkeypatch.setenv(TASK_BUDGETS_OPT_IN_ENV, "true")
    monkeypatch.delenv("ANTHROPIC_BETA", raising=False)

    env = _spawn_and_capture_env(tmp_path)

    assert env.get("ANTHROPIC_BETA") == TASK_BUDGETS_BETA_HEADER


def test_beta_header_env_not_set_without_opt_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing/false opt-in env leaves ``ANTHROPIC_BETA`` untouched."""
    monkeypatch.delenv(TASK_BUDGETS_OPT_IN_ENV, raising=False)
    monkeypatch.delenv("ANTHROPIC_BETA", raising=False)

    env = _spawn_and_capture_env(tmp_path)

    assert "ANTHROPIC_BETA" not in env


def test_beta_header_appends_to_existing_operator_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator-set ``ANTHROPIC_BETA`` is preserved and the new value appended."""
    monkeypatch.setenv(TASK_BUDGETS_OPT_IN_ENV, "true")
    monkeypatch.setenv("ANTHROPIC_BETA", "prompt-caching-2024-07-31")

    env = _spawn_and_capture_env(tmp_path)

    beta = env.get("ANTHROPIC_BETA", "")
    assert "prompt-caching-2024-07-31" in beta
    assert TASK_BUDGETS_BETA_HEADER in beta


def test_beta_header_idempotent_when_value_already_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the operator already set the same value the adapter does not duplicate it."""
    monkeypatch.setenv(TASK_BUDGETS_OPT_IN_ENV, "true")
    monkeypatch.setenv("ANTHROPIC_BETA", TASK_BUDGETS_BETA_HEADER)

    env = _spawn_and_capture_env(tmp_path)

    beta = env.get("ANTHROPIC_BETA", "")
    assert beta.count(TASK_BUDGETS_BETA_HEADER) == 1


def test_falsy_opt_in_value_does_not_emit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit ``false`` keeps the header off."""
    monkeypatch.setenv(TASK_BUDGETS_OPT_IN_ENV, "false")
    monkeypatch.delenv("ANTHROPIC_BETA", raising=False)

    env = _spawn_and_capture_env(tmp_path)

    assert "ANTHROPIC_BETA" not in env
