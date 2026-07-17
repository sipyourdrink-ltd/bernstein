"""Browser / computer-use adapter family wiring + isolation (#2606).

Covers:
* Registration via the registry and dispatch parity with coding adapters.
* Strategy declaration in ``STRATEGY_MATRIX`` (conformance gate).
* YAML contract + golden transcript presence.
* Per-task isolation: two concurrent sessions share no profile state.
* Typed terminal states for driver failure / timeout.
* Multimodal-attachment refusal (this adapter fronts actions, not images).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bernstein.adapters._contract import EventChannel, strategy_for
from bernstein.adapters.base import CLIAdapter, SpawnResult
from bernstein.adapters.computer_use import (
    ComputerUseAdapter,
    ComputerUseDriverError,
    ComputerUseTerminalState,
    ReferenceComputerUseAdapter,
    classify_terminal_state,
)
from bernstein.adapters.conformance import assert_strategies_declared
from bernstein.adapters.registry import get_adapter, iter_adapter_specs
from bernstein.core.tasks.models import ModelConfig


def _popen_mock(pid: int = 4242) -> MagicMock:
    import subprocess

    m = MagicMock(spec=subprocess.Popen)
    m.pid = pid
    m.stdout = MagicMock()
    return m


# ---------------------------------------------------------------------------
# Registration + conformance
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_registered_under_computer_use(self) -> None:
        adapter = get_adapter("computer_use")
        assert isinstance(adapter, ReferenceComputerUseAdapter)
        assert isinstance(adapter, ComputerUseAdapter)
        assert isinstance(adapter, CLIAdapter)

    def test_appears_in_registry_enumeration(self) -> None:
        names = {name for name, _ in iter_adapter_specs()}
        assert "computer_use" in names

    def test_strategy_declared(self) -> None:
        # Missing declaration is a hard conformance failure; this must not raise.
        assert_strategies_declared()

    def test_strategy_is_poll_pty(self) -> None:
        # The external agent owns its loop; Bernstein polls for liveness and the
        # per-action record is the signed lineage chain, not a stdout stream.
        assert strategy_for("computer_use").event_channel is EventChannel.POLL_PTY

    def test_name_is_string(self) -> None:
        assert get_adapter("computer_use").name() == "Computer Use"


# ---------------------------------------------------------------------------
# Contract + golden transcript presence
# ---------------------------------------------------------------------------


class TestContractAndGolden:
    def test_yaml_contract_present(self) -> None:
        repo_root = Path(__file__).resolve().parents[3]
        contract = repo_root / "tests" / "contract" / "contracts" / "computer_use.yaml"
        assert contract.exists(), f"missing YAML contract at {contract}"

    def test_golden_transcript_loads_and_passes(self, tmp_path: Path) -> None:
        from bernstein.adapters.conformance import ConformanceHarness, load_golden_transcripts

        repo_root = Path(__file__).resolve().parents[3]
        golden_dir = repo_root / "tests" / "golden"
        transcripts = [t for t in load_golden_transcripts(golden_dir) if "computer_use" in t.adapter_class]
        assert transcripts, "computer-use golden transcript not found"

        harness = ConformanceHarness()
        report = harness.run_all(transcripts, workdir=tmp_path)
        assert report.passed, report.regressions


# ---------------------------------------------------------------------------
# Spawn + dispatch parity
# ---------------------------------------------------------------------------


class TestSpawn:
    def test_spawn_returns_spawn_result(self, tmp_path: Path) -> None:
        adapter = ReferenceComputerUseAdapter()
        (tmp_path / ".sdd" / "runtime").mkdir(parents=True, exist_ok=True)
        with patch("bernstein.adapters.computer_use.subprocess.Popen", return_value=_popen_mock()):
            result = adapter.spawn(
                prompt="fill the signup form",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="low"),
                session_id="cu-sess-1",
                timeout_seconds=0,
            )
        assert isinstance(result, SpawnResult)
        assert result.pid == 4242
        assert isinstance(result.log_path, Path)

    def test_spawn_refuses_multimodal_attachment(self, tmp_path: Path) -> None:
        from bernstein.core.agents.multimodal import ModalityType, MultiModalContext, MultiModalInput
        from bernstein.core.agents.multimodal_attestation import CapabilityRefusal

        adapter = ReferenceComputerUseAdapter()
        attachment = tmp_path / "shot.png"
        attachment.write_bytes(b"fake")
        ctx = MultiModalContext(
            inputs=(
                MultiModalInput(
                    modality=ModalityType.IMAGE,
                    content_path=attachment,
                    mime_type="image/png",
                    description="shot",
                ),
            ),
            primary_modality=ModalityType.IMAGE,
        )
        with pytest.raises(CapabilityRefusal):
            adapter.spawn(
                prompt="x",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="low"),
                session_id="cu-sess-2",
                timeout_seconds=0,
                multimodal_context=ctx,
            )


# ---------------------------------------------------------------------------
# AC: isolation -- concurrent browser tasks share no profile state
# ---------------------------------------------------------------------------


class TestIsolation:
    def test_distinct_sessions_get_disjoint_profile_dirs(self, tmp_path: Path) -> None:
        adapter = ReferenceComputerUseAdapter()
        a = adapter.isolated_profile_dir(workdir=tmp_path, session_id="task-a")
        b = adapter.isolated_profile_dir(workdir=tmp_path, session_id="task-b")
        assert a != b
        # Neither is a parent of the other.
        assert a not in b.parents
        assert b not in a.parents

    def test_no_cookie_bleed_between_concurrent_tasks(self, tmp_path: Path) -> None:
        adapter = ReferenceComputerUseAdapter()
        a = adapter.prepare_isolation(workdir=tmp_path, session_id="task-a")
        b = adapter.prepare_isolation(workdir=tmp_path, session_id="task-b")

        (a / "cookies.sqlite").write_text("session=aaa")
        assert not (b / "cookies.sqlite").exists(), "cookie bled into the other task's profile"

    def test_same_session_is_stable(self, tmp_path: Path) -> None:
        adapter = ReferenceComputerUseAdapter()
        one = adapter.isolated_profile_dir(workdir=tmp_path, session_id="task-a")
        two = adapter.isolated_profile_dir(workdir=tmp_path, session_id="task-a")
        assert one == two

    def test_spawn_creates_isolated_profile(self, tmp_path: Path) -> None:
        adapter = ReferenceComputerUseAdapter()
        (tmp_path / ".sdd" / "runtime").mkdir(parents=True, exist_ok=True)
        with patch("bernstein.adapters.computer_use.subprocess.Popen", return_value=_popen_mock()):
            adapter.spawn(
                prompt="x",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="low"),
                session_id="cu-iso",
                timeout_seconds=0,
            )
        assert adapter.isolated_profile_dir(workdir=tmp_path, session_id="cu-iso").is_dir()


# ---------------------------------------------------------------------------
# AC: typed terminal states, never free text
# ---------------------------------------------------------------------------


class TestTerminalStates:
    def test_clean_exit_is_ok(self) -> None:
        assert classify_terminal_state(exit_code=0, timed_out=False) is ComputerUseTerminalState.OK

    def test_nonzero_exit_is_driver_failure(self) -> None:
        assert classify_terminal_state(exit_code=1, timed_out=False) is ComputerUseTerminalState.DRIVER_FAILURE

    def test_timeout_is_timeout(self) -> None:
        assert classify_terminal_state(exit_code=None, timed_out=True) is ComputerUseTerminalState.TIMEOUT

    def test_driver_error_carries_typed_state(self, tmp_path: Path) -> None:
        adapter = ReferenceComputerUseAdapter(cli_command="definitely-not-on-path-xyz")
        (tmp_path / ".sdd" / "runtime").mkdir(parents=True, exist_ok=True)
        with patch(
            "bernstein.adapters.computer_use.subprocess.Popen",
            side_effect=FileNotFoundError("no such binary"),
        ):
            with pytest.raises(ComputerUseDriverError) as exc:
                adapter.spawn(
                    prompt="x",
                    workdir=tmp_path,
                    model_config=ModelConfig(model="sonnet", effort="low"),
                    session_id="cu-fail",
                    timeout_seconds=0,
                )
        assert exc.value.terminal_state is ComputerUseTerminalState.DRIVER_FAILURE
