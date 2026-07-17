"""Adapter wave (issue #2521): grok, qoder, codebuddy, trae, mimo, warp, jules.

Each new CLI enters through the same gate as the rest of the fleet: it
resolves by name through the registry, declares a resume / dangerous-mode /
event-channel strategy, ships a capability contract, and joins the nightly
conformance canary matrix. These tests pin the exact headless invocation the
adapter builds (so a silent CLI-flag regression fails here, mirroring the
golden replay), and assert the substrate membership the acceptance criteria
require.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters._contract import (
    DEFAULT_ADAPTER_STRATEGY,
    DangerousModeStrategy,
    EventChannel,
    strategy_for,
    undeclared_strategies,
)
from bernstein.adapters.base import CLIAdapter, SpawnResult
from bernstein.adapters.canary import CANARY_MATRIX
from bernstein.adapters.registry import _ADAPTERS, get_adapter

if TYPE_CHECKING:
    from collections.abc import Iterable

# Registry keys landed by this wave.
WAVE_ADAPTERS = ("codebuddy", "grok", "jules", "mimo", "qoder", "trae", "warp")

# Binary each adapter shells out to (differs from the registry key for the
# tools whose executable is named differently).
WAVE_BINARIES = {
    "codebuddy": "codebuddy",
    "grok": "grok",
    "jules": "jules",
    "mimo": "mimo",
    "qoder": "qodercli",
    "trae": "trae-cli",
    "warp": "oz",
}

# Expected inner CLI argv (after the bernstein-worker ``--`` separator) for a
# fixed prompt/model, plus the credential env keys each adapter declares.
_PROMPT = "fix the bug in main.py"
WAVE_EXPECTED: dict[str, dict[str, Any]] = {
    "grok": {
        "model": "grok-code-fast-1",
        "argv": ["grok", "-p", _PROMPT, "--output-format", "json", "--always-approve", "--no-auto-update"],
        "env": {"XAI_API_KEY", "GROK_API_KEY"},
    },
    "codebuddy": {
        "model": "gpt-5",
        "argv": [
            "codebuddy",
            "-p",
            _PROMPT,
            "--model",
            "gpt-5",
            "--output-format",
            "stream-json",
            "--dangerously-skip-permissions",
        ],
        "env": {"CODEBUDDY_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"},
    },
    "qoder": {
        "model": "default",
        "argv": ["qodercli", "-p", _PROMPT],
        "env": {"QODER_API_KEY", "DASHSCOPE_API_KEY"},
    },
    "trae": {
        "model": "default",
        "argv": ["trae-cli", "run", _PROMPT],
        "env": {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY", "OPENROUTER_API_KEY"},
    },
    "mimo": {
        "model": "mimo-v2.5-pro",
        "argv": ["mimo", "run", "--model", "mimo-v2.5-pro", "--dangerously-skip-permissions", _PROMPT],
        "env": {"MIMO_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"},
    },
    "warp": {
        "model": "claude-sonnet-4-6",
        "argv": ["oz", "agent", "run", "--prompt", _PROMPT, "--model", "claude-sonnet-4-6"],
        "env": set(),
    },
    "jules": {
        "model": "default",
        "argv": ["jules", "remote", "new", "--repo", ".", "--session", _PROMPT],
        "env": {"JULES_API_KEY"},
    },
}


def _make_popen_mock(pid: int = 4242) -> MagicMock:
    m = MagicMock(spec=subprocess.Popen)
    m.pid = pid
    m.stdout = MagicMock()
    return m


def _inner_argv(argv: Iterable[str]) -> list[str]:
    """Return argv after the bernstein-worker ``--`` separator."""
    argv = list(argv)
    for i, tok in enumerate(argv):
        if tok == "--":
            return argv[i + 1 :]
    return argv


def _spawn_capture(adapter_name: str, tmp_path: Path) -> tuple[list[str], set[str]]:
    """Spawn with mocked Popen; return (inner_argv, captured env-extra keys)."""
    adapter = get_adapter(adapter_name)
    module = type(adapter).__module__
    captured: list[str] = []
    import bernstein.adapters.env_isolation as env_isolation

    original = env_isolation.build_filtered_env

    def _spy(extra_keys: Any = (), **kwargs: Any) -> dict[str, str]:
        captured.extend(extra_keys)
        return original(extra_keys, **kwargs)

    expected = WAVE_EXPECTED[adapter_name]
    with (
        patch(f"{module}.subprocess.Popen", return_value=_make_popen_mock()) as popen,
        patch(f"{module}.build_filtered_env", _spy),
    ):
        result = adapter.spawn(
            prompt=_PROMPT,
            workdir=tmp_path,
            model_config=ModelConfig(model=expected["model"], effort="low"),
            session_id="wave-0",
            timeout_seconds=0,
        )
    assert isinstance(result, SpawnResult)
    call_args, _ = popen.call_args_list[0]
    return _inner_argv(call_args[0]), set(captured)


# ---------------------------------------------------------------------------
# Registry + substrate membership (acceptance criteria)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", WAVE_ADAPTERS)
def test_adapter_resolves_by_name(name: str) -> None:
    adapter = get_adapter(name)
    assert isinstance(adapter, CLIAdapter)
    assert adapter.name()


def test_no_undeclared_strategies_for_wave() -> None:
    assert undeclared_strategies(list(WAVE_ADAPTERS)) == []


@pytest.mark.parametrize("name", WAVE_ADAPTERS)
def test_strategy_is_not_the_bare_default(name: str) -> None:
    # A declared row must exist; qoder/warp intentionally match the default's
    # values but are still explicit keys in STRATEGY_MATRIX (asserted above).
    strat = strategy_for(name)
    assert strat is not DEFAULT_ADAPTER_STRATEGY or name in {"qoder", "warp"}


def test_dangerous_mode_declarations() -> None:
    assert strategy_for("grok").dangerous_mode is DangerousModeStrategy.CLI_FLAG
    assert strategy_for("codebuddy").dangerous_mode is DangerousModeStrategy.CLI_FLAG
    assert strategy_for("mimo").dangerous_mode is DangerousModeStrategy.CLI_FLAG
    assert strategy_for("trae").dangerous_mode is DangerousModeStrategy.ALWAYS_ON
    assert strategy_for("jules").dangerous_mode is DangerousModeStrategy.ALWAYS_ON


def test_stream_json_event_channels() -> None:
    assert strategy_for("grok").event_channel is EventChannel.STREAM_JSON
    assert strategy_for("codebuddy").event_channel is EventChannel.STREAM_JSON


@pytest.mark.parametrize("name", WAVE_ADAPTERS)
def test_in_canary_matrix(name: str) -> None:
    targets = {t.adapter: t for t in CANARY_MATRIX}
    assert name in targets
    assert targets[name].binary == WAVE_BINARIES[name]


def test_canary_matrix_sorted_and_unique() -> None:
    adapters = [t.adapter for t in CANARY_MATRIX]
    assert adapters == sorted(adapters)
    assert len(adapters) == len(set(adapters))


@pytest.mark.parametrize("name", WAVE_ADAPTERS)
def test_contract_loads_and_names_binary(name: str) -> None:
    from bernstein.adapters._contract import ContractSpec

    spec = ContractSpec.load(name)
    assert spec.binary == WAVE_BINARIES[name]


# ---------------------------------------------------------------------------
# Exact headless invocation (silent CLI-flag regression guard)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", WAVE_ADAPTERS)
def test_spawn_builds_expected_argv(name: str, tmp_path: Path) -> None:
    inner, env_keys = _spawn_capture(name, tmp_path)
    expected = WAVE_EXPECTED[name]
    assert inner == expected["argv"], f"{name}: argv drift\n  expected {expected['argv']}\n  actual   {inner}"
    assert expected["env"] <= env_keys, f"{name}: env extras regression, saw {sorted(env_keys)}"


@pytest.mark.parametrize("name", WAVE_ADAPTERS)
def test_missing_binary_raises_runtime_error(name: str, tmp_path: Path) -> None:
    adapter = get_adapter(name)
    module = type(adapter).__module__
    with patch(f"{module}.subprocess.Popen", side_effect=FileNotFoundError()):
        with pytest.raises(RuntimeError):
            adapter.spawn(
                prompt=_PROMPT,
                workdir=tmp_path,
                model_config=ModelConfig(model="default", effort="low"),
                session_id="wave-err",
                timeout_seconds=0,
            )


def test_wave_registry_keys_present() -> None:
    for name in WAVE_ADAPTERS:
        assert name in _ADAPTERS
