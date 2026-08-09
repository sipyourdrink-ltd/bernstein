"""Unit tests for #3100: Kimchi CLI adapter driven over ACP event channel."""

from __future__ import annotations

import json
import subprocess
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
from bernstein.adapters.conformance import replay_acp_event_fixture
from bernstein.adapters.kimchi import KimchiAdapter
from bernstein.adapters.registry import get_adapter, selectable_adapter_names
from bernstein.core.replay.journal import EventJournal
from bernstein.core.tasks.checkpoint_retry import (
    CheckpointRef,
    CheckpointRetryCapability,
    RetryMode,
    build_retry_prompt,
    checkpoint_retry_capability,
    decide_retry,
)

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "acp" / "lifecycle" / "kimchi_acp_task.jsonl"


def _inner_argv(cmd: list[str]) -> list[str]:
    """Return the adapter's own argv from a bernstein-worker wrapped command.

    ``build_worker_cmd`` prefixes the worker invocation, which carries its own
    ``--session <id>`` and ``--model <name>`` flags, and separates it from the
    wrapped command with ``--``. Assertions about the adapter's flags must run
    on this slice: asserting ``"--session" in cmd`` over the whole list passes
    for every adapter in the repo whether or not the adapter emits it.
    """
    return cmd[cmd.index("--") + 1 :]


def _spawn_and_capture(adapter: KimchiAdapter, tmp_path: Path, **overrides: Any) -> tuple[list[str], dict[str, Any]]:
    """Spawn with the kwarg set the orchestrator supplies; return argv and Popen kwargs."""
    kwargs: dict[str, Any] = {
        "prompt": "Refactor auth module",
        "workdir": tmp_path,
        "model_config": ModelConfig(model="open-weight-7b", effort="normal"),
        "session_id": "backend-kimchi-task-1",
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


# ---------------------------------------------------------------------------
# Registration and declared strategy
# ---------------------------------------------------------------------------


def test_kimchi_adapter_registered_and_strategy_matrix_matches() -> None:
    assert "kimchi" in selectable_adapter_names()
    adapter = get_adapter("kimchi")
    assert isinstance(adapter, KimchiAdapter)

    strategy = STRATEGY_MATRIX.get("kimchi")
    assert strategy is not None
    assert strategy.resume == ResumeStrategy.UNSUPPORTED
    assert strategy.dangerous_mode == DangerousModeStrategy.CLI_FLAG
    assert strategy.event_channel == EventChannel.ACP
    assert strategy.output_mode == OutputMode.GIT_DIFF

    # The matrix row is keyed by the registry name; the adapter resolves its
    # own row through ``name()``. A rename that breaks that resolution would
    # silently fall back to DEFAULT_ADAPTER_STRATEGY, so pin the round-trip.
    assert adapter.name() == "Kimchi"
    assert adapter.strategy() == strategy

    assert "kimchi" not in undeclared_strategies(selectable_adapter_names())


# ---------------------------------------------------------------------------
# The command the orchestrator actually produces
# ---------------------------------------------------------------------------


def test_orchestrator_spawn_argv_is_pinned(tmp_path: Path) -> None:
    """The argv for the orchestrator's own call shape is exact.

    ``spawner_core`` passes no adapter-specific keywords, so this is the only
    command Kimchi is ever launched with. ``--yolo`` in particular must be
    here: the declared ``DangerousModeStrategy.CLI_FLAG`` describes nothing if
    the flag depends on a keyword no caller supplies, and an unattended run
    without it stalls on the first tool-approval prompt.
    """
    argv, _ = _spawn_and_capture(KimchiAdapter(), tmp_path)
    assert _inner_argv(argv) == [
        "kimchi",
        "--mode",
        "acp",
        "--yolo",
        "--prompt",
        "Refactor auth module",
        "--model",
        "open-weight-7b",
    ]


def test_worker_session_flag_is_not_an_adapter_flag(tmp_path: Path) -> None:
    """``--session`` belongs to the worker wrapper, never to the Kimchi argv.

    Kimchi's own ``--session <path>`` resume is not wired (see the strategy
    matrix comment), and the wrapper's ``--session <id>`` sits in the prefix.
    Without this split an assertion on the whole command reads as adapter
    coverage while proving only that ``build_worker_cmd`` ran.
    """
    argv, _ = _spawn_and_capture(KimchiAdapter(), tmp_path)
    assert "--session" in argv[: argv.index("--")]
    assert "--session" not in _inner_argv(argv)


def test_spawn_env_is_isolated_and_telemetry_pinned(tmp_path: Path) -> None:
    with patch.dict("os.environ", {"KIMCHI_API_KEY": "secret-key", "SECRET_VAR": "leave-me-out"}):
        _, popen_kwargs = _spawn_and_capture(KimchiAdapter(), tmp_path)

    env = popen_kwargs["env"]
    assert env.get("KIMCHI_API_KEY") == "secret-key"
    assert env.get("KIMCHI_TELEMETRY_ENABLED") == "0"
    assert "SECRET_VAR" not in env


def test_spawn_closes_stdin(tmp_path: Path) -> None:
    """``--mode acp`` expects a JSON-RPC peer that this spawn does not provide.

    With the orchestrator's stdin inherited, such a process waits for an
    ``initialize`` request that never arrives and burns the whole timeout;
    with ``DEVNULL`` it reads EOF and exits, which the commit check sees.
    """
    _, popen_kwargs = _spawn_and_capture(KimchiAdapter(), tmp_path)
    assert popen_kwargs["stdin"] is subprocess.DEVNULL


def test_spawn_reports_a_missing_binary_as_an_actionable_error(tmp_path: Path) -> None:
    adapter = KimchiAdapter()
    with patch("subprocess.Popen", side_effect=FileNotFoundError), pytest.raises(RuntimeError, match="not found"):
        adapter.spawn(
            prompt="p",
            workdir=tmp_path,
            model_config=ModelConfig(model="open-weight-7b", effort="normal"),
            session_id="backend-kimchi-task-9",
            timeout_seconds=0,
        )


# ---------------------------------------------------------------------------
# Resume axis: the declaration must match what the spawn can deliver
# ---------------------------------------------------------------------------


def test_retry_capability_matches_what_the_spawn_can_deliver(tmp_path: Path) -> None:
    """No spawn path passes a Kimchi session file, so no warm retry is offered.

    ``decide_retry`` reads the resume axis. Declaring native resume promotes a
    failed task's retry to warm, and ``build_retry_prompt`` then sends only the
    corrective instruction, assuming the adapter reattached to the prior
    session. Kimchi never emits ``--session``, so that retry would reach a
    fresh agent carrying none of the original task.
    """
    assert checkpoint_retry_capability("kimchi") is CheckpointRetryCapability.NONE

    checkpoint = CheckpointRef(
        task_id="T-1",
        adapter="kimchi",
        session_id="kimchi-session-1",
        workspace_hash="deadbeef",
        worktree_path=str(tmp_path),
        journal_index=0,
        event_hash="h0",
    )
    decision = decide_retry(
        task_id="T-1",
        requested_mode=RetryMode.WARM,
        checkpoint=checkpoint,
        actual_workspace_hash="deadbeef",
        gate_name="tests",
        gate_output="1 failed",
    )
    assert decision.effective_mode is RetryMode.COLD
    assert decision.downgrade_reason == "adapter_capability_none"
    assert build_retry_prompt(decision, cold_prompt="the full original task") == "the full original task"


# ---------------------------------------------------------------------------
# Tier detection
# ---------------------------------------------------------------------------


def _home(monkeypatch: pytest.MonkeyPatch, root: Path) -> Path:
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: root))
    cfg = root / ".config" / "kimchi" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    return cfg


def test_detect_tier_none_without_credential(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _home(monkeypatch, tmp_path)
    monkeypatch.delenv("KIMCHI_API_KEY", raising=False)
    assert KimchiAdapter().detect_tier() is None


@pytest.mark.parametrize("body", ["", "   ", "{", "[]", "{}", '{"api_key": ""}', '{"api_key": null}'])
def test_detect_tier_none_for_a_config_without_a_usable_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    """An empty, truncated or logged-out config must not advertise an account.

    ``detect_tier`` feeds provider selection: reporting an active PRO account
    from a file's mere existence routes work to an adapter whose spawn cannot
    authenticate.
    """
    cfg = _home(monkeypatch, tmp_path)
    cfg.write_text(body, encoding="utf-8")
    monkeypatch.delenv("KIMCHI_API_KEY", raising=False)
    assert KimchiAdapter().detect_tier() is None


def test_detect_tier_from_env_credential(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from bernstein.core.models import ApiTier, ProviderType

    _home(monkeypatch, tmp_path)
    monkeypatch.setenv("KIMCHI_API_KEY", "secret-key")
    info = KimchiAdapter().detect_tier()
    assert info is not None
    assert info.provider is ProviderType.KIMCHI
    assert info.tier is ApiTier.PRO
    assert info.is_active is True


def test_detect_tier_from_config_credential(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _home(monkeypatch, tmp_path)
    cfg.write_text(json.dumps({"api_key": "from-config"}), encoding="utf-8")
    monkeypatch.delenv("KIMCHI_API_KEY", raising=False)
    info = KimchiAdapter().detect_tier()
    assert info is not None
    assert info.is_active is True


# ---------------------------------------------------------------------------
# ACP fixture replay (rewritten in the next commit)
# ---------------------------------------------------------------------------


def test_acp_fixture_replay_parity(tmp_path: Path) -> None:
    dir_1 = tmp_path / "sdd1"
    dir_2 = tmp_path / "sdd2"

    res1 = replay_acp_event_fixture(FIXTURE, sdd_dir=dir_1)
    res2 = replay_acp_event_fixture(FIXTURE, sdd_dir=dir_2)

    assert res1.ok is True
    assert res2.ok is True
    assert res1.journal_head == res2.journal_head


def test_acp_stdout_drift_insensitivity(tmp_path: Path) -> None:
    clean_dir = tmp_path / "clean_sdd"
    drift_dir = tmp_path / "drift_sdd"

    clean_res = replay_acp_event_fixture(FIXTURE, sdd_dir=clean_dir)

    raw_lines = FIXTURE.read_text().splitlines()
    drift_lines = [
        "Kimchi v0.1.74 (c) 2026",
        raw_lines[0],
        "[progress] Loading model weights...",
        raw_lines[1],
        "\033[32mOK\033[0m",
        raw_lines[2],
        raw_lines[3],
    ]

    from bernstein.adapters.acp_channel import run_acp_channel

    journal = EventJournal("acp-conf-drift", drift_dir)
    json_lines = (line for line in drift_lines if line.strip().startswith("{"))
    drift_res = run_acp_channel(json_lines, journal=journal, session_id="kimchi-s1")

    assert clean_res.ok is True
    assert drift_res.ok is True
    assert clean_res.journal_head == journal.head()
