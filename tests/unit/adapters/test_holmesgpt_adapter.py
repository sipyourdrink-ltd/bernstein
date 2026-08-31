"""Unit tests for HolmesGPT adapter."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters._contract import (
    STRATEGY_MATRIX,
    DangerousModeStrategy,
    EventChannel,
    OutputMode,
    ResumeStrategy,
    undeclared_strategies,
)
from bernstein.adapters.holmesgpt import (
    HolmesGPTAdapter,
    HolmesGPTDriverError,
    HolmesGPTStructuredOutput,
    HolmesGPTTerminalState,
    classify_terminal_state,
    parse_structured_output,
)
from bernstein.adapters.registry import get_adapter, selectable_adapter_names


def _inner_argv(cmd: list[str]) -> list[str]:
    """Return the adapter's own argv from a bernstein-worker wrapped command.

    ``build_worker_cmd`` prefixes the worker invocation and separates it from the
    wrapped command with ``--``. Assertions about the adapter's flags must run
    on this slice.
    """
    return cmd[cmd.index("--") + 1 :]


def _spawn_and_capture(adapter: HolmesGPTAdapter, tmp_path: Path, **overrides: Any) -> tuple[list[str], dict[str, Any]]:
    """Spawn with the kwargs the orchestrator supplies; return argv and Popen kwargs."""
    kwargs: dict[str, Any] = {
        "prompt": "Investigate the auth module",
        "workdir": tmp_path,
        "model_config": ModelConfig(model="open-weight-7b", effort="normal"),
        "session_id": "analyst-holmes-task-1",
        "mcp_config": None,
        "task_scope": "medium",
        "budget_multiplier": 1.0,
        "system_addendum": "",
        "timeout_seconds": 0,
    }
    kwargs.update(overrides)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_popen.return_value = mock_proc
        result = adapter.spawn(**kwargs)
    assert result.pid == 12345
    return list(mock_popen.call_args[0][0]), dict(mock_popen.call_args[1])


# -------------------------------------------------------------------------------------------------
# Registration and declared strategy
# -------------------------------------------------------------------------------------------------


def test_adapter_registered_and_strategy_declared() -> None:
    assert "holmesgpt" in selectable_adapter_names()
    adapter = get_adapter("holmesgpt")
    assert isinstance(adapter, HolmesGPTAdapter)

    strategy = STRATEGY_MATRIX.get("holmesgpt")
    assert strategy is not None
    assert strategy.resume == ResumeStrategy.UNSUPPORTED
    assert strategy.dangerous_mode == DangerousModeStrategy.UNSUPPORTED
    assert strategy.event_channel == EventChannel.TEXT_SIGNALS
    assert strategy.output_mode == OutputMode.ARTIFACT

    assert adapter.name() == "HolmesGPT"
    assert adapter.strategy() == strategy

    assert "holmesgpt" not in undeclared_strategies(selectable_adapter_names())


# -------------------------------------------------------------------------------------------------
# Command construction
# -------------------------------------------------------------------------------------------------


def test_spawn_builds_correct_command(tmp_path: Path) -> None:
    """The argv includes the holmes ask subcommand with required flags."""
    adapter = HolmesGPTAdapter()
    argv, _ = _spawn_and_capture(adapter, tmp_path)
    inner = _inner_argv(argv)

    # Verify the core command structure
    assert inner[0] == "holmes"
    assert "ask" in inner
    assert "--no-interactive" in inner
    assert "--json-output" in inner

    # The path argument follows --json-output
    json_output_idx = inner.index("--json-output")
    json_output_path = inner[json_output_idx + 1]
    assert json_output_path.endswith("output.json")

    # Prompt follows the -- separator
    prompt_idx = inner.index("--")
    assert inner[prompt_idx + 1] == "Investigate the auth module"


# -------------------------------------------------------------------------------------------------
# Structured output parsing
# -------------------------------------------------------------------------------------------------


def test_parse_structured_output_happy_path() -> None:
    json_text = json.dumps(
        {
            "conclusion": "The auth module is secure",
            "observations": ["Checked auth.py", "Ran static analysis"],
            "sources": [
                {"path": "/src/auth.py", "hash": "abc123"},
            ],
            "inconclusive": False,
            "reasoning": "All checks passed",
        }
    )
    result = parse_structured_output(json_text)

    assert isinstance(result, HolmesGPTStructuredOutput)
    assert result.conclusion == "The auth module is secure"
    assert result.observations == ["Checked auth.py", "Ran static analysis"]
    assert result.sources == [{"path": "/src/auth.py", "hash": "abc123"}]
    assert result.inconclusive is False
    assert result.reasoning == "All checks passed"


def test_parse_structured_output_malformed_json_raises() -> None:
    with pytest.raises(json.JSONDecodeError):
        parse_structured_output('{"conclusion": "broken"')


# -------------------------------------------------------------------------------------------------
# Terminal state classification
# -------------------------------------------------------------------------------------------------


def test_classify_terminal_state() -> None:
    # OK: exit_code == 0, not timed_out, not inconclusive
    assert classify_terminal_state(exit_code=0, timed_out=False) is HolmesGPTTerminalState.OK
    assert (
        classify_terminal_state(exit_code=0, timed_out=False, inconclusive_detected=False) is HolmesGPTTerminalState.OK
    )

    # TIMEOUT: timed_out wins over everything
    assert classify_terminal_state(exit_code=0, timed_out=True) is HolmesGPTTerminalState.TIMEOUT
    assert classify_terminal_state(exit_code=1, timed_out=True) is HolmesGPTTerminalState.TIMEOUT

    # INCONCLUSIVE: timed_out is False, inconclusive_detected is True
    assert (
        classify_terminal_state(exit_code=0, timed_out=False, inconclusive_detected=True)
        is HolmesGPTTerminalState.INCONCLUSIVE
    )
    assert (
        classify_terminal_state(exit_code=1, timed_out=False, inconclusive_detected=True)
        is HolmesGPTTerminalState.INCONCLUSIVE
    )

    # DRIVER_FAILURE: non-zero exit code, not timed_out, not inconclusive
    assert classify_terminal_state(exit_code=1, timed_out=False) is HolmesGPTTerminalState.DRIVER_FAILURE
    assert classify_terminal_state(exit_code=127, timed_out=False) is HolmesGPTTerminalState.DRIVER_FAILURE

    # DRIVER_FAILURE: None exit code, not timed_out, not inconclusive
    assert classify_terminal_state(exit_code=None, timed_out=False) is HolmesGPTTerminalState.DRIVER_FAILURE


# -------------------------------------------------------------------------------------------------
# Driver error propagation
# -------------------------------------------------------------------------------------------------


def test_driver_error_carries_typed_state(tmp_path: Path) -> None:
    adapter = HolmesGPTAdapter()

    with patch("subprocess.Popen", side_effect=FileNotFoundError("holmes not found")):
        with pytest.raises(HolmesGPTDriverError) as exc_info:
            adapter.spawn(
                prompt="Investigate auth",
                workdir=tmp_path,
                model_config=ModelConfig(model="open-weight-7b", effort="normal"),
                session_id="analyst-holmes-err-1",
                timeout_seconds=0,
            )

    assert exc_info.value.terminal_state is HolmesGPTTerminalState.DRIVER_FAILURE
