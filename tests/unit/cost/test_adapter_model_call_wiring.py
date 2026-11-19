"""Tests for wiring adapter invocations, agent spawns, and cost CLI to ModelCallLedger."""

from __future__ import annotations

import json
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.models import ModelConfig
from bernstein.core.spawner import AgentSpawner
from click.testing import CliRunner

from bernstein.adapters.base import CLIAdapter, SpawnResult
from bernstein.adapters.registry import _ADAPTERS, register_adapter
from bernstein.cli.commands.cost import cost_cmd
from bernstein.core.agents.agent_lifecycle import _read_runner_cost_usd, _resolve_tokens_sidecar_path
from bernstein.core.cost.model_call_ledger import ModelCallLedger


class _DirectTestAdapter(CLIAdapter):
    parameter_schema_version = "direct-test/2"
    parameter_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "temperature": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "max_tokens": {"type": "integer", "minimum": 1},
        },
        "additionalProperties": False,
    }
    model_version = "20261101"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self._counter = 0

    def name(self) -> str:
        return "direct-test"

    def detect_binary(self) -> str | None:
        return None

    def build_command(
        self,
        prompt: str,
        workdir: Path,
        model_config: ModelConfig,
        session_id: str,
        mcp_config: dict[str, Any] | None = None,
    ) -> list[str]:
        return ["echo", prompt]

    def spawn(
        self,
        prompt: str,
        workdir: Path,
        model_config: ModelConfig,
        session_id: str,
        mcp_config: dict[str, Any] | None = None,
        sandbox_prefix: list[str] | None = None,
        timeout_seconds: int = 1800,
        stream: bool = False,
        env_overrides: dict[str, str] | None = None,
        **_kwargs: Any,
    ) -> SpawnResult:
        return SpawnResult(pid=12345, log_path=workdir / "agent.log")

    def invoke(
        self,
        *,
        model: str,
        input_text: str,
        parameters: dict[str, Any] | None = None,
    ) -> str:
        self._counter += 1
        self.calls.append((model, input_text, parameters))
        return f"reply-{self._counter}:{model}:{input_text}"


class _SpawnOnlyTestAdapter(CLIAdapter):
    def name(self) -> str:
        return "spawn-only-test"

    def detect_binary(self) -> str | None:
        return None

    def build_command(
        self,
        prompt: str,
        workdir: Path,
        model_config: ModelConfig,
        session_id: str,
        mcp_config: dict[str, Any] | None = None,
    ) -> list[str]:
        return ["echo", prompt]

    def spawn(
        self,
        prompt: str,
        workdir: Path,
        model_config: ModelConfig,
        session_id: str,
        mcp_config: dict[str, Any] | None = None,
        sandbox_prefix: list[str] | None = None,
        timeout_seconds: int = 1800,
        stream: bool = False,
        env_overrides: dict[str, str] | None = None,
        **_kwargs: Any,
    ) -> SpawnResult:
        log_path = workdir / f"{session_id}.log"
        log_path.write_text("agent completed cleanly\n")
        return SpawnResult(pid=12345, log_path=log_path)


def test_adapter_invoke_recorded_validates_parameters_and_persists(tmp_path: Path) -> None:
    adapter = _DirectTestAdapter()
    ledger = ModelCallLedger(tmp_path / ".sdd")

    with pytest.raises(ValueError, match="Invalid parameters"):
        adapter.invoke_recorded(
            ledger=ledger,
            capability_id="review.summarise",
            model="claude-opus-4",
            input_text="diff",
            parameters={"temperature": 5.0},
        )
    assert adapter.calls == []
    assert ledger.list_records() == []

    first = adapter.invoke_recorded(
        ledger=ledger,
        capability_id="review.summarise",
        model="claude-opus-4",
        input_text="diff",
        parameters={"temperature": 0.2, "max_tokens": 256},
        journal_entry_id="task-42",
    )
    assert first.status == "succeeded"
    assert first.adapter_id == "direct-test"
    assert first.model_version == "20261101"
    assert first.parameter_schema_version == "direct-test/2"
    assert first.output_text == "reply-1:claude-opus-4:diff"
    assert len(adapter.calls) == 1

    reused = adapter.invoke_recorded(
        ledger=ledger,
        capability_id="review.summarise",
        model="claude-opus-4",
        input_text="diff",
        parameters={"temperature": 0.2, "max_tokens": 256},
        reuse_identical=True,
    )
    assert reused.reused is True
    assert reused.reused_from == first.id
    assert len(adapter.calls) == 1


def test_spawner_records_model_call_and_cost_join(tmp_path: Path, make_task) -> None:
    adapter = _SpawnOnlyTestAdapter()
    spawner = AgentSpawner(adapter, tmp_path / "templates", tmp_path, use_worktrees=False, default_model="mock-model")
    task = make_task(role="backend")

    session = spawner.spawn_for_tasks([task])
    assert session.model_call_id != ""

    ledger = ModelCallLedger(tmp_path / ".sdd")
    running = ledger.get_record(session.model_call_id)
    assert running is not None
    assert running.status == "running"
    assert running.adapter_id == "spawn-only-test"
    assert running.model == "mock-model"
    assert running.parameter_schema_version == "1.0"
    assert running.journal_entry_id == task.id

    spawner.reap_completed_agent(session, skip_merge=True)
    reopened = ModelCallLedger(tmp_path / ".sdd")
    finished = reopened.get_record(session.model_call_id)
    assert finished is not None
    assert finished.status == "succeeded"
    assert "agent completed cleanly" in finished.output_text

    sidecar_path = _resolve_tokens_sidecar_path(tmp_path, session)
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar_path.write_text('{"ts": 1.0, "in": 1000, "out": 200}\n')

    with patch("bernstein.core.agents.agent_lifecycle.price_model_usage") as mock_price:
        mock_price.return_value = MagicMock(cost_usd=0.01, priced=True)
        cost_usd, tokens_in, tokens_out = _read_runner_cost_usd(tmp_path, session, task.id)

    assert cost_usd == pytest.approx(0.01)
    assert tokens_in == 1000
    assert tokens_out == 200
    mock_price.assert_called_once_with("mock-model", 1000, 200, model_call_id=session.model_call_id)


def test_spawner_marks_failed_model_call_when_spawn_raises(tmp_path: Path, make_task) -> None:
    adapter = _SpawnOnlyTestAdapter()
    with patch.object(adapter, "spawn", side_effect=RuntimeError("binary crashed")):
        spawner = AgentSpawner(
            adapter, tmp_path / "templates", tmp_path, use_worktrees=False, default_model="mock-model"
        )
        task = make_task(role="backend")
        with pytest.raises(RuntimeError, match="All spawn attempts failed"):
            spawner.spawn_for_tasks([task])

    records = ModelCallLedger(tmp_path / ".sdd").list_records()
    assert len(records) == 1
    assert records[0].status == "failed"
    assert "binary crashed" in records[0].error


def test_cli_model_call_invoke_replay_list_show_with_registered_adapter(tmp_path: Path) -> None:
    sdd_dir = tmp_path / ".sdd"
    sdd_dir.mkdir()
    (sdd_dir / "runtime").mkdir()
    adapter = _DirectTestAdapter()
    spawn_only = _SpawnOnlyTestAdapter()
    runner = CliRunner()

    register_adapter("direct-test", adapter)
    register_adapter("spawn-only-test", spawn_only)
    try:
        res_invoke = runner.invoke(
            cost_cmd,
            [
                "model-call",
                "invoke",
                "--sdd-dir",
                str(sdd_dir),
                "--capability-id",
                "review.summarise",
                "--adapter-id",
                "direct-test",
                "--model",
                "claude-opus-4",
                "--input",
                "hello",
                "--parameters",
                json.dumps({"temperature": 0.1}),
            ],
        )
        assert res_invoke.exit_code == 0, res_invoke.output
        invoked_payload = json.loads(res_invoke.output)
        record_id = invoked_payload["id"]
        assert invoked_payload["output_text"] == "reply-1:claude-opus-4:hello"
        assert invoked_payload["parameter_schema_version"] == "direct-test/2"
        assert invoked_payload["model_version"] == "20261101"

        res_reused = runner.invoke(
            cost_cmd,
            [
                "model-call",
                "invoke",
                "--sdd-dir",
                str(sdd_dir),
                "--capability-id",
                "review.summarise",
                "--adapter-id",
                "direct-test",
                "--model",
                "claude-opus-4",
                "--input",
                "hello",
                "--parameters",
                json.dumps({"temperature": 0.1}),
                "--reuse-identical",
            ],
        )
        assert res_reused.exit_code == 0, res_reused.output
        reused_payload = json.loads(res_reused.output)
        assert reused_payload["reused_from"] == record_id
        assert len(adapter.calls) == 1

        res_replay = runner.invoke(
            cost_cmd,
            [
                "model-call",
                "replay",
                "--sdd-dir",
                str(sdd_dir),
                "--record-id",
                record_id,
            ],
        )
        assert res_replay.exit_code == 0, res_replay.output
        replay_payload = json.loads(res_replay.output)
        assert replay_payload["replay_of"] == record_id
        assert replay_payload["output_text"] == "reply-2:claude-opus-4:hello"

        res_list = runner.invoke(cost_cmd, ["model-call", "list", "--sdd-dir", str(sdd_dir)])
        assert res_list.exit_code == 0
        assert len(json.loads(res_list.output)) == 3

        res_show = runner.invoke(
            cost_cmd,
            ["model-call", "show", "--sdd-dir", str(sdd_dir), "--record-id", record_id],
        )
        assert res_show.exit_code == 0
        assert json.loads(res_show.output)["id"] == record_id

        res_spawn_only = runner.invoke(
            cost_cmd,
            [
                "model-call",
                "invoke",
                "--sdd-dir",
                str(sdd_dir),
                "--capability-id",
                "review.summarise",
                "--adapter-id",
                "spawn-only-test",
                "--model",
                "gpt-5-mini",
                "--input",
                "hello",
            ],
        )
        assert res_spawn_only.exit_code == 1
        assert "Real adapter invocation requires a running agent session" in res_spawn_only.output
    finally:
        _ADAPTERS.pop("direct-test", None)
        _ADAPTERS.pop("spawn-only-test", None)


def _refresh_dead_session(tmp_path: Path, session: Any) -> None:
    from bernstein.core.agents.agent_lifecycle import refresh_agent_states

    spawner = MagicMock()
    spawner.check_alive.return_value = False
    orch = SimpleNamespace(
        _workdir=tmp_path,
        _agents={session.id: session},
        _spawner=spawner,
        _agent_failure_timestamps={},
        _rate_limit_tracker=None,
        _signal_mgr=MagicMock(),
        _crash_counts={},
        _spawn_failures={},
        _SPAWN_BACKOFF_MAX_S=60,
        _MAX_DEAD_AGENTS_KEPT=10,
        _processed_done_tasks={},
        _MAX_PROCESSED_DONE=10,
    )
    with ExitStack() as stack:
        for name in (
            "_capture_agent_crash",
            "_propagate_abort_to_children",
            "_release_file_ownership",
            "_release_task_to_session",
            "_preserve_runner_logs",
            "_write_retry_checkpoint",
            "_dispose_dead_worktree",
            "_reevaluate_pending_death_judgments",
            "_release_orphaned_file_locks",
        ):
            stack.enter_context(patch(f"bernstein.core.agents.agent_lifecycle.{name}"))
        refresh_agent_states(orch, {})


def test_refresh_clean_exit_preserves_successful_call(tmp_path: Path) -> None:
    from bernstein.core.models import AgentSession

    log = tmp_path / "agent.log"
    log.write_text("completed output\n")
    call = ModelCallLedger(tmp_path / ".sdd").begin_call(
        capability_id="agent.spawn",
        adapter_id="fake",
        model="fake",
    )
    session = AgentSession(
        id="clean-exit",
        role="backend",
        task_ids=[],
        model_config=ModelConfig("fake", "high"),
        exit_code=0,
        log_path=str(log),
        model_call_id=call.id,
    )
    _refresh_dead_session(tmp_path, session)
    assert session.status == "dead"
    final = ModelCallLedger(tmp_path / ".sdd").get_record(call.id)
    assert final is not None
    assert final.status == "succeeded"
    assert final.output_text == "completed output\n"


@pytest.mark.parametrize("cleanup_exit", [-15, 143])
def test_completed_call_survives_cleanup_termination(tmp_path: Path, make_task, cleanup_exit: int) -> None:
    spawner = AgentSpawner(
        _SpawnOnlyTestAdapter(),
        tmp_path / "templates",
        tmp_path,
        use_worktrees=False,
        default_model="fake",
    )
    session = spawner.spawn_for_tasks([make_task(role="backend")])
    proc = MagicMock()
    proc.poll.return_value = None
    proc.wait.return_value = cleanup_exit
    spawner._procs[session.id] = proc
    spawner.reap_completed_agent(session, skip_merge=True)
    assert session.exit_code == cleanup_exit
    proc.terminate.assert_called_once()
    record = ModelCallLedger(tmp_path / ".sdd").get_record(session.model_call_id)
    assert record is not None
    assert record.status == "succeeded"
    assert "agent completed cleanly" in record.output_text


@pytest.mark.parametrize("timed_out", [False, True])
def test_forced_stop_finalizes_without_worktree(tmp_path: Path, make_task, timed_out: bool) -> None:
    from bernstein.core.models import AbortReason

    adapter = _SpawnOnlyTestAdapter()
    spawner = AgentSpawner(
        adapter,
        tmp_path / "templates",
        tmp_path,
        use_worktrees=False,
        default_model="fake",
    )
    session = spawner.spawn_for_tasks([make_task(role="backend")])
    if timed_out:
        session.status = "dead"
        session.abort_reason = AbortReason.TIMEOUT
        session.abort_detail = "timeout budget exceeded"
        session.exit_code = 124
    with patch.object(adapter, "kill"):
        spawner.kill(session)
    _refresh_dead_session(tmp_path, session)
    record = ModelCallLedger(tmp_path / ".sdd").get_record(session.model_call_id)
    assert record is not None
    assert record.status == "failed"
    assert record.error == ("timeout budget exceeded" if timed_out else "local process kill requested by orchestrator")
    with patch.object(adapter, "kill"):
        spawner.kill(session)
    assert ModelCallLedger(tmp_path / ".sdd").get_record(session.model_call_id) == record


def test_spawn_records_task_scope_and_retry_multiplier(tmp_path: Path, make_task) -> None:
    from bernstein.core.models import Scope

    adapter = _SpawnOnlyTestAdapter()
    spawner = AgentSpawner(
        adapter,
        tmp_path / "templates",
        tmp_path,
        use_worktrees=False,
        default_model="fake",
    )
    tasks = [make_task(role="backend", scope=Scope.LARGE), make_task(role="backend", scope=Scope.SMALL)]
    tasks[0].metadata["budget_multiplier"] = 2.0
    tasks[1].metadata["budget_multiplier"] = 1.5
    with patch.object(adapter, "spawn", wraps=adapter.spawn) as spawn:
        session = spawner.spawn_for_tasks(tasks)
    record = ModelCallLedger(tmp_path / ".sdd").get_record(session.model_call_id)
    assert record is not None
    for name in ("task_scope", "budget_multiplier", "timeout_seconds"):
        assert record.parameters[name] == spawn.call_args.kwargs[name]
    assert record.parameters["task_scope"] == "large"
    assert record.parameters["budget_multiplier"] == 2.0
