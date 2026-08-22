"""System-addendum delivery is a declared, enforced contract axis (#4256).

``system_addendum`` carries the completion and heartbeat instructions into a
spawn. Which channel an adapter delivers them on used to live only in
per-adapter docstring prose, so an adapter that discarded them looked exactly
like one that honoured them - and the run only failed later, when the
supervisor waited for signals the agent was never told to emit.

These tests pin the three parts of the contract: the declaration, the
conformance check that keeps the declaration honest against the adapter
sources, and the spawn-time surfacing that puts a dropped addendum in the run
record instead of nowhere.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters._contract import (
    STRATEGY_MATRIX,
    SYSTEM_ADDENDUM_CHANNEL_MATRIX,
    SystemAddendumChannel,
    system_addendum_channel,
)
from bernstein.adapters.base import CLIAdapter, SpawnResult, capability_notices_path
from bernstein.adapters.conformance import (
    SystemAddendumDeclarationError,
    assert_system_addendum_channels_declared,
    system_addendum_channel_discrepancies,
)

# ---------------------------------------------------------------------------
# Declaration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("adapter", "expected"),
    [
        ("claude", SystemAddendumChannel.SYSTEM_PROMPT),
        ("claude_routine", SystemAddendumChannel.SYSTEM_PROMPT),
        ("openai_agents", SystemAddendumChannel.SYSTEM_PROMPT),
        ("junie", SystemAddendumChannel.PROMPT_APPEND),
        ("devin_terminal", SystemAddendumChannel.PROMPT_APPEND),
        ("q_dev", SystemAddendumChannel.PROMPT_APPEND),
        ("plandex", SystemAddendumChannel.IGNORED),
        ("forge", SystemAddendumChannel.IGNORED),
    ],
)
def test_channel_is_declared_per_adapter(adapter: str, expected: SystemAddendumChannel) -> None:
    assert system_addendum_channel(adapter) is expected


def test_unknown_adapter_declares_ignored() -> None:
    """The conservative default: assume the addendum is dropped, and say so."""
    assert system_addendum_channel("some-third-party-cli") is SystemAddendumChannel.IGNORED


def test_namespace_form_resolves_to_the_registry_row() -> None:
    assert system_addendum_channel("claude code") is SystemAddendumChannel.SYSTEM_PROMPT


def test_matrix_has_one_row_per_declared_adapter() -> None:
    assert set(SYSTEM_ADDENDUM_CHANNEL_MATRIX) == set(STRATEGY_MATRIX)


# ---------------------------------------------------------------------------
# Conformance: the declaration must match what the adapter source does
# ---------------------------------------------------------------------------


def test_shipped_adapters_match_their_declared_channel() -> None:
    """Every shipped adapter's spawn body agrees with its declared bucket.

    This is what stops an adapter moving between buckets unnoticed: dropping
    the ``system_addendum`` use out of a declared-delivering adapter, or adding
    one to a declared-ignoring adapter, fails here.
    """
    assert system_addendum_channel_discrepancies() == []


def test_assert_helper_raises_on_a_declaration_that_does_not_match_source() -> None:
    with patch(
        "bernstein.adapters.conformance.system_addendum_channel_discrepancies",
        return_value=["plandex: declared ignored, spawn body uses system_addendum"],
    ):
        with pytest.raises(SystemAddendumDeclarationError, match="plandex"):
            assert_system_addendum_channels_declared()


# ---------------------------------------------------------------------------
# Spawn-time surfacing
# ---------------------------------------------------------------------------


class _RecordingAdapter(CLIAdapter):
    """Minimal adapter that records the argv it would have executed."""

    def __init__(self, adapter_name: str) -> None:
        super().__init__()
        self._adapter_name = adapter_name
        self.argv: list[str] = []

    def name(self) -> str:
        return self._adapter_name

    def spawn(
        self,
        *,
        prompt: str,
        workdir: Path,
        model_config: ModelConfig,
        session_id: str,
        mcp_config: dict[str, Any] | None = None,
        timeout_seconds: int = 1800,
        task_scope: str = "medium",
        budget_multiplier: float = 1.0,
        system_addendum: str = "",
        multimodal_context: Any | None = None,
    ) -> SpawnResult:
        """Build argv the way the declared channel says, then fake the launch."""
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.touch()
        self.argv = [self._adapter_name, prompt]
        if system_addendum and self.system_addendum_channel() is SystemAddendumChannel.SYSTEM_PROMPT:
            self.argv.extend(["--append-system-prompt", system_addendum])
        proc = MagicMock(spec=subprocess.Popen)
        proc.pid = 4256
        return SpawnResult(pid=4256, log_path=log_path, proc=proc)


_ADDENDUM = "When done: curl -X POST .../complete. Heartbeat every 60s."


def _spawn(adapter: CLIAdapter, workdir: Path, *, addendum: str = _ADDENDUM) -> SpawnResult:
    return adapter.spawn(
        prompt="do the thing",
        workdir=workdir,
        model_config=ModelConfig("sonnet", "low"),
        session_id="backend-4256abcd",
        system_addendum=addendum,
    )


def _notices(workdir: Path) -> list[dict[str, Any]]:
    path = capability_notices_path(workdir)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_adapter_with_a_system_prompt_channel_receives_the_addendum_unchanged(tmp_path: Path) -> None:
    adapter = _RecordingAdapter("claude")

    _spawn(adapter, tmp_path)

    assert "--append-system-prompt" in adapter.argv
    assert adapter.argv[adapter.argv.index("--append-system-prompt") + 1] == _ADDENDUM
    recorded = _notices(tmp_path)
    assert len(recorded) == 1
    assert recorded[0]["channel"] == "system-prompt"
    assert recorded[0]["delivered"] is True


def test_adapter_that_ignores_the_addendum_surfaces_it_at_spawn(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The whole point: a dropped addendum is visible now, not three minutes later."""
    adapter = _RecordingAdapter("plandex")

    with caplog.at_level(logging.WARNING, logger="bernstein.adapters.base"):
        _spawn(adapter, tmp_path)

    assert _ADDENDUM not in adapter.argv
    recorded = _notices(tmp_path)
    assert len(recorded) == 1
    assert recorded[0]["channel"] == "ignored"
    assert recorded[0]["delivered"] is False
    assert recorded[0]["adapter"] == "plandex"
    assert recorded[0]["session_id"] == "backend-4256abcd"
    assert recorded[0]["addendum_sha256"]
    assert any("system_addendum" in rec.getMessage() for rec in caplog.records)


def test_no_notice_is_written_when_there_is_no_addendum_to_deliver(tmp_path: Path) -> None:
    adapter = _RecordingAdapter("plandex")

    _spawn(adapter, tmp_path, addendum="")

    assert _notices(tmp_path) == []


def test_notice_is_recorded_once_per_session(tmp_path: Path) -> None:
    """Decorated / re-entrant spawn paths must not multiply the record."""
    adapter = _RecordingAdapter("plandex")

    _spawn(adapter, tmp_path)
    _spawn(adapter, tmp_path)

    assert len(_notices(tmp_path)) == 1


def test_reporting_failure_never_breaks_the_spawn(tmp_path: Path) -> None:
    adapter = _RecordingAdapter("plandex")

    with patch("bernstein.adapters.base.capability_notices_path", side_effect=OSError("read-only fs")):
        result = _spawn(adapter, tmp_path)

    assert result.pid == 4256
