"""Focused tests for Issue #8077: openai_agents token extraction and reconciliation."""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import subprocess
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.models import AgentSession, Complexity, ModelConfig, Scope, Task, TaskStatus

from bernstein.adapters.openai_agents import OpenAIAgentsAdapter
from bernstein.adapters.openai_agents_runner import (
    EXIT_OK,
    RunnerManifest,
    _extract_usage_breakdown,
    _extract_usage_tokens,
    run,
)
from bernstein.core.agents.spawner_core import AgentSpawner
from bernstein.core.cost.cost import MODEL_COSTS_PER_1M_TOKENS, read_tokens_sidecar_totals
from bernstein.core.cost.cost_tracker import CostTracker, estimate_cost
from bernstein.core.cost.model_prices import price_model_usage
from bernstein.core.observability import metric_collector as metric_collector_mod
from bernstein.core.orchestration.orchestrator import Orchestrator
from bernstein.core.tasks.task_lifecycle import _record_completion_metrics

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


class _StubRunHooks:
    """Minimal base class matching the SDK's ``sdk.RunHooks`` subclass contract."""


def _usage(
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cached_tokens: int = 0,
    reasoning_tokens: int = 0,
    tool_calls: int = 0,
) -> Any:
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        input_tokens_details=SimpleNamespace(cached_tokens=cached_tokens),
        output_tokens_details=SimpleNamespace(reasoning_tokens=reasoning_tokens),
        tool_calls=tool_calls,
    )


def _fake_sdk(runner: Any) -> Any:
    return SimpleNamespace(
        Agent=MagicMock(return_value=MagicMock()),
        Runner=runner,
        RunHooks=_StubRunHooks,
        ModelSettings=MagicMock(),
    )


def _manifest(tmp_path: Path, session_id: str, model: str = "gpt-5-mini") -> RunnerManifest:
    return RunnerManifest(
        session_id=session_id,
        prompt="solve task",
        workdir=str(tmp_path),
        model=model,
        heartbeat_dir=str(tmp_path / ".sdd" / "runtime" / "heartbeats"),
    )


def _orch_for_session(tmp_path: Path, session: AgentSession, *, cost_tracker: Any = None) -> Any:
    return SimpleNamespace(
        _processed_done_tasks=collections.OrderedDict(),
        _executor=MagicMock(),
        _agents={session.id: session},
        _find_session_for_task=lambda task_id: session if task_id in session.task_ids else None,
        _spawner=MagicMock(),
        _record_provider_health=MagicMock(),
        _approval_gate=None,
        _post_bulletin=MagicMock(),
        _notify=MagicMock(),
        _cost_tracker=cost_tracker if cost_tracker is not None else MagicMock(),
        _cost_cap_killed_agents=set(),
        _evolution=None,
        _response_cache=MagicMock(),
        _client=MagicMock(),
        _config=SimpleNamespace(
            server_url="http://server",
            cross_model_verify=None,
            pr_labels=[],
            budget_usd=0.0,
            max_cost_per_agent=0.0,
        ),
        _workdir=tmp_path,
        _quality_gate_config=None,
        _wal_writer=None,
        _bandit_router=None,
    )


@pytest.fixture
def isolated_collector(tmp_path: Path) -> Iterator[metric_collector_mod.MetricsCollector]:
    """Provide an isolated global MetricsCollector rooted under ``tmp_path``."""
    previous = metric_collector_mod._default_collector
    metric_collector_mod._default_collector = None
    collector = metric_collector_mod.get_collector(tmp_path / ".sdd" / "metrics")
    try:
        yield collector
    finally:
        metric_collector_mod._default_collector = previous


class TestUsageExtractionShapes:
    """Verify result.usage -> context_wrapper.usage -> raw_responses[].usage extraction."""

    def test_extracts_from_result_usage_and_context_wrapper_and_raw_responses(self) -> None:
        top = SimpleNamespace(
            usage=_usage(input_tokens=1_500, output_tokens=300, cached_tokens=400, reasoning_tokens=90, tool_calls=2),
            raw_responses=[],
        )
        assert _extract_usage_tokens(top) == (1_500, 300, 2)
        assert _extract_usage_breakdown(top) == _extract_usage_breakdown(
            SimpleNamespace(usage=None, context_wrapper=SimpleNamespace(usage=top.usage), raw_responses=[])
        )

        raw = SimpleNamespace(
            usage=None,
            context_wrapper=None,
            raw_responses=[
                SimpleNamespace(
                    usage=_usage(input_tokens=800, output_tokens=100, cached_tokens=200, reasoning_tokens=30)
                ),
                SimpleNamespace(
                    usage={
                        "prompt_tokens": 1_200,
                        "completion_tokens": 250,
                        "prompt_tokens_details": {"cached_tokens": 300},
                        "completion_tokens_details": {"reasoning_tokens": 70},
                    }
                ),
            ],
        )
        breakdown = _extract_usage_breakdown(raw)
        assert (
            breakdown.input_tokens,
            breakdown.output_tokens,
            breakdown.cached_tokens,
            breakdown.reasoning_tokens,
        ) == (
            2_000,
            350,
            500,
            100,
        )


class TestRunnerStubAndLifecycleReconciliation:
    """Stubbed Runner.run_sync -> .tokens sidecar -> collector.task_metrics."""

    @pytest.mark.parametrize("source_mode", ["context_wrapper", "raw_responses"])
    def test_run_sync_stub_reconciles_tokens_into_collector_task_metrics(
        self,
        tmp_path: Path,
        make_task: Any,
        isolated_collector: metric_collector_mod.MetricsCollector,
        capsys: pytest.CaptureFixture[str],
        caplog: pytest.LogCaptureFixture,
        source_mode: str,
    ) -> None:
        session_id = f"oa-{source_mode}"
        manifest = _manifest(tmp_path, session_id, model="MiniMax-M3")
        stub_runner = MagicMock()
        if source_mode == "context_wrapper":
            stub_runner.run_sync.return_value = SimpleNamespace(
                final_output="done",
                context_wrapper=SimpleNamespace(
                    usage=_usage(
                        input_tokens=10_000,
                        output_tokens=2_000,
                        cached_tokens=4_000,
                        reasoning_tokens=500,
                        tool_calls=1,
                    )
                ),
                raw_responses=[],
            )
            exp_in, exp_out, exp_cached, exp_reasoning = 10_000, 2_000, 4_000, 500
        else:
            stub_runner.run_sync.return_value = SimpleNamespace(
                final_output="done",
                raw_responses=[
                    SimpleNamespace(
                        usage=_usage(input_tokens=3_000, output_tokens=400, cached_tokens=500, reasoning_tokens=80)
                    ),
                    SimpleNamespace(
                        usage=_usage(input_tokens=2_000, output_tokens=350, cached_tokens=700, reasoning_tokens=70)
                    ),
                ],
            )
            exp_in, exp_out, exp_cached, exp_reasoning = 5_000, 750, 1_200, 150

        with patch.dict("sys.modules", {"agents": _fake_sdk(stub_runner)}):
            assert run(manifest) == EXIT_OK

        events = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
        usage_event = next(e for e in events if e["type"] == "usage")
        assert usage_event["input_tokens"] == exp_in
        assert usage_event["output_tokens"] == exp_out
        assert usage_event["cached_tokens"] == exp_cached
        assert usage_event["reasoning_tokens"] == exp_reasoning

        rates = MODEL_COSTS_PER_1M_TOKENS["minimax-m3"]
        expected_cost = (exp_in / 1_000_000.0) * rates.get("input", 0.0) + (exp_out / 1_000_000.0) * rates.get(
            "output", 0.0
        )
        assert usage_event["cost_usd"] == pytest.approx(expected_cost)
        assert read_tokens_sidecar_totals(tmp_path / ".sdd" / "runtime" / f"{session_id}.tokens") == (exp_in, exp_out)

        task = make_task(id=f"T-{source_mode}", title="oa task", description="d", status=TaskStatus.DONE)
        session = AgentSession(
            id=session_id,
            role="backend",
            task_ids=[task.id],
            status="working",
            exit_code=0,
            provider="openai_agents",
            model_config=ModelConfig("MiniMax-M3", "high"),
        )
        isolated_collector.start_task(task.id, role="backend", model="MiniMax-M3", provider="openai_agents")

        with caplog.at_level(logging.INFO):
            task_m, cost_usd = _record_completion_metrics(
                _orch_for_session(tmp_path, session),
                task,
                session,
                janitor_passed=True,
                qg_result=None,
                completion_data=None,
                agent_just_reaped=True,
            )

        assert cost_usd == pytest.approx(expected_cost)
        assert task_m.tokens_prompt == exp_in
        assert task_m.tokens_completion == exp_out
        assert task_m.tokens_used == exp_in + exp_out
        assert any("source=tokens_sidecar" in rec.message for rec in caplog.records)


class TestIncrementalHooksAndRecovery:
    """Per-turn RunHooks flush sidecar early without double-counting."""

    def test_on_llm_end_and_final_emit_deduplicate_and_preserve_delta(
        self,
        tmp_path: Path,
        make_task: Any,
        isolated_collector: metric_collector_mod.MetricsCollector,
    ) -> None:
        manifest = _manifest(tmp_path, "oa-hooks-exact")
        turn1 = SimpleNamespace(usage=_usage(input_tokens=1_200, output_tokens=200), output=[])
        turn2 = SimpleNamespace(usage=_usage(input_tokens=1_800, output_tokens=400), output=[])

        class _HookRunner:
            @staticmethod
            def run_sync(agent: Any, prompt: str, **kwargs: Any) -> Any:
                del prompt
                hooks = kwargs["hooks"]
                asyncio.run(hooks.on_llm_end(None, agent, turn1))
                asyncio.run(hooks.on_llm_end(None, agent, turn2))
                return SimpleNamespace(
                    final_output="done",
                    context_wrapper=SimpleNamespace(usage=_usage(input_tokens=3_000, output_tokens=600)),
                    raw_responses=[turn1, turn2],
                )

        with patch.dict("sys.modules", {"agents": _fake_sdk(_HookRunner)}):
            assert run(manifest) == EXIT_OK

        sidecar = tmp_path / ".sdd" / "runtime" / "oa-hooks-exact.tokens"
        assert read_tokens_sidecar_totals(sidecar) == (3_000, 600)
        assert len(sidecar.read_text(encoding="utf-8").strip().splitlines()) == 2

        manifest_delta = _manifest(tmp_path, "oa-hooks-delta")

        class _PartialHookRunner:
            @staticmethod
            def run_sync(agent: Any, prompt: str, **kwargs: Any) -> Any:
                del prompt
                asyncio.run(kwargs["hooks"].on_llm_end(None, agent, turn1))
                return SimpleNamespace(
                    final_output="done",
                    context_wrapper=SimpleNamespace(usage=_usage(input_tokens=1_500, output_tokens=350)),
                    raw_responses=[],
                )

        with patch.dict("sys.modules", {"agents": _fake_sdk(_PartialHookRunner)}):
            assert run(manifest_delta) == EXIT_OK

        delta_sidecar = tmp_path / ".sdd" / "runtime" / "oa-hooks-delta.tokens"
        assert read_tokens_sidecar_totals(delta_sidecar) == (1_500, 350)
        assert len(delta_sidecar.read_text(encoding="utf-8").strip().splitlines()) == 2

        task = make_task(id="T-hooks", title="hooks task", description="d", status=TaskStatus.DONE)
        session = AgentSession(
            id="oa-hooks-exact",
            role="backend",
            task_ids=[task.id],
            status="working",
            exit_code=0,
            provider="openai_agents",
            model_config=ModelConfig("gpt-5-mini", "high"),
        )
        isolated_collector.start_task(task.id, role="backend", model="gpt-5-mini", provider="openai_agents")
        task_m, _ = _record_completion_metrics(
            _orch_for_session(tmp_path, session),
            task,
            session,
            janitor_passed=True,
            qg_result=None,
            completion_data=None,
            agent_just_reaped=True,
        )
        assert (task_m.tokens_prompt, task_m.tokens_completion, task_m.tokens_used) == (3_000, 600, 3_600)

    def test_early_termination_after_on_llm_end_retains_turn_tokens(
        self,
        tmp_path: Path,
        make_task: Any,
        isolated_collector: metric_collector_mod.MetricsCollector,
    ) -> None:
        session_id = "oa-early-1"
        manifest = _manifest(tmp_path, session_id)
        turn1 = SimpleNamespace(usage=_usage(input_tokens=2_500, output_tokens=300), output=[])

        class _EarlyStopSignal(BaseException):
            pass

        class _EarlyExitRunner:
            @staticmethod
            def run_sync(agent: Any, prompt: str, **kwargs: Any) -> Any:
                del prompt
                asyncio.run(kwargs["hooks"].on_llm_end(None, agent, turn1))
                raise _EarlyStopSignal("simulated terminate")

        with patch.dict("sys.modules", {"agents": _fake_sdk(_EarlyExitRunner)}), pytest.raises(_EarlyStopSignal):
            run(manifest)

        task = make_task(id="T-oa-early", title="oa early", description="d", status=TaskStatus.DONE)
        task.assigned_agent = session_id
        isolated_collector.start_agent(session_id, role="backend", model="gpt-5-mini", provider="openai_agents")
        isolated_collector.start_task(task.id, role="backend", model="gpt-5-mini", provider="openai_agents")
        task_m, cost_usd = _record_completion_metrics(
            _orch_for_session(tmp_path, AgentSession(id="other", role="backend", task_ids=["other"])),
            task,
            None,
            janitor_passed=True,
            qg_result=None,
            completion_data=None,
            agent_just_reaped=False,
        )
        assert cost_usd > 0.0
        assert (task_m.tokens_prompt, task_m.tokens_completion, task_m.tokens_used) == (2_500, 300, 2_800)


class TestResumeAndLogFallback:
    """Resume spawns forward heartbeat_dir and logs fall back when sidecar is missing."""

    def test_spawn_for_resume_forwards_heartbeat_dir_for_openai_agents(self, tmp_path: Path) -> None:
        spawner = AgentSpawner(
            adapter=OpenAIAgentsAdapter(),
            templates_dir=tmp_path / "templates",
            workdir=tmp_path,
            default_model="gpt-5-mini",
        )
        task = Task(
            id="T-resume-oa",
            title="Resume openai_agents task",
            description="Continue task work",
            role="backend",
            scope=Scope.SMALL,
            complexity=Complexity.LOW,
            status=TaskStatus.OPEN,
        )
        worktree_path = tmp_path / ".sdd" / "worktrees" / "prev-oa"
        worktree_path.mkdir(parents=True)
        fake_popen = MagicMock(spec=subprocess.Popen, pid=12345)
        fake_popen.poll.return_value = None

        with patch("bernstein.adapters.openai_agents.subprocess.Popen", return_value=fake_popen):
            session = spawner.spawn_for_resume([task], worktree_path=worktree_path, changed_files=[])

        manifest_data = json.loads(
            (worktree_path / ".sdd" / "runtime" / f"{session.id}.manifest.json").read_text(encoding="utf-8")
        )
        assert manifest_data["heartbeat_dir"] == str(tmp_path / ".sdd" / "runtime" / "heartbeats")
        assert manifest_data["instrumentation_root"] == str(tmp_path)

    def test_log_fallback_recovers_runner_usage_event_into_task_metrics(
        self,
        tmp_path: Path,
        make_task: Any,
        isolated_collector: metric_collector_mod.MetricsCollector,
    ) -> None:
        session_id = "oa-log-only"
        log_path = tmp_path / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            "\n".join(
                [
                    json.dumps({"type": "start", "session_id": session_id, "model": "gpt-5-mini"}),
                    json.dumps({"type": "usage", "input_tokens": 4_200, "output_tokens": 600, "model": "gpt-5-mini"}),
                ]
            ),
            encoding="utf-8",
        )

        task = make_task(id="T-oa-log", title="oa log", description="d", status=TaskStatus.DONE)
        session = AgentSession(
            id=session_id,
            role="backend",
            task_ids=[task.id],
            status="working",
            exit_code=0,
            provider="openai_agents",
            log_path=str(log_path),
            model_config=ModelConfig("gpt-5-mini", "high"),
        )
        isolated_collector.start_task(task.id, role="backend", model="gpt-5-mini", provider="openai_agents")

        task_m, cost_usd = _record_completion_metrics(
            _orch_for_session(tmp_path, session),
            task,
            session,
            janitor_passed=True,
            qg_result=None,
            completion_data=None,
            agent_just_reaped=True,
        )
        assert cost_usd > 0.0
        assert (task_m.tokens_prompt, task_m.tokens_completion, task_m.tokens_used) == (4_200, 600, 4_800)

    def test_spawn_for_resume_skips_mcp_config_when_not_heartbeat_consumer(self, tmp_path: Path) -> None:
        adapter = MagicMock()
        adapter.name.return_value = "generic"
        adapter.consumes_heartbeat_dir = False
        adapter.spawn.return_value = MagicMock(
            pid=99, log_path=tmp_path / "out.log", abort_reason=None, abort_detail=""
        )
        spawner = AgentSpawner(
            adapter=adapter,
            templates_dir=tmp_path / "templates",
            workdir=tmp_path,
            default_model="sonnet",
            mcp_config={"extra": "val"},
        )
        task = Task(
            id="T-resume-other",
            title="Resume non-heartbeat task",
            description="Continue",
            role="backend",
            scope=Scope.SMALL,
            complexity=Complexity.LOW,
            status=TaskStatus.OPEN,
        )
        worktree_path = tmp_path / ".sdd" / "worktrees" / "prev-other"
        worktree_path.mkdir(parents=True)

        spawner.spawn_for_resume([task], worktree_path=worktree_path, changed_files=[])
        assert "mcp_config" not in adapter.spawn.call_args.kwargs


class TestRealCostTrackerLiveAndCompletion:
    """Real CostTracker (no mocks) across per-turn live ticks and task completion."""

    def test_estimate_cost_longest_key_and_none_tier(self) -> None:
        # gpt-5-mini ($0.5/$2.5 per 1M) must not hit gpt-5 ($2.5/$15 per 1M).
        assert estimate_cost("gpt-5-mini", 1_000_000, 1_000_000) == pytest.approx(3.0)
        # MiniMax-M3 sets cache_write=None; must not raise TypeError.
        assert estimate_cost("MiniMax-M3", 1_000_000, 1_000_000, cache_write_tokens=0) == pytest.approx(3.0)

    @pytest.mark.parametrize("model_name", ["gpt-5-mini", "MiniMax-M3"])
    def test_live_ticks_and_completion_match_with_real_cost_tracker(
        self,
        tmp_path: Path,
        make_task: Any,
        isolated_collector: metric_collector_mod.MetricsCollector,
        model_name: str,
    ) -> None:
        session_id = f"oa-live-{model_name}"
        manifest = _manifest(tmp_path, session_id, model=model_name)
        task = make_task(id=f"T-live-{model_name}", title="live cost task", description="d", status=TaskStatus.DONE)
        session = AgentSession(
            id=session_id,
            role="backend",
            task_ids=[task.id],
            status="working",
            exit_code=None,
            provider="openai_agents",
            model_config=ModelConfig(model_name, "high"),
        )
        tracker = CostTracker(run_id=f"run-{model_name}", budget_usd=10.0)
        orch = _orch_for_session(tmp_path, session, cost_tracker=tracker)

        turn1 = SimpleNamespace(usage=_usage(input_tokens=1_000, output_tokens=200), output=[])
        turn2 = SimpleNamespace(usage=_usage(input_tokens=2_000, output_tokens=400), output=[])
        live_snapshots: list[float] = []

        class _LiveTickRunner:
            @staticmethod
            def run_sync(agent: Any, prompt: str, **kwargs: Any) -> Any:
                del prompt
                hooks = kwargs["hooks"]
                asyncio.run(hooks.on_llm_end(None, agent, turn1))
                Orchestrator._record_live_costs(orch)
                live_snapshots.append(tracker.status().spent_usd)

                asyncio.run(hooks.on_llm_end(None, agent, turn2))
                Orchestrator._record_live_costs(orch)
                live_snapshots.append(tracker.status().spent_usd)

                return SimpleNamespace(
                    final_output="done",
                    context_wrapper=SimpleNamespace(usage=_usage(input_tokens=3_000, output_tokens=600)),
                    raw_responses=[turn1, turn2],
                )

        with patch.dict("sys.modules", {"agents": _fake_sdk(_LiveTickRunner)}):
            assert run(manifest) == EXIT_OK

        expected_turn1 = price_model_usage(model_name, 1_000, 200).cost_usd
        expected_total = price_model_usage(model_name, 3_000, 600).cost_usd
        assert live_snapshots[0] == pytest.approx(expected_turn1)
        assert live_snapshots[1] == pytest.approx(expected_total)

        session.exit_code = 0
        isolated_collector.start_task(task.id, role="backend", model=model_name, provider="openai_agents")
        task_m, completion_cost = _record_completion_metrics(
            orch,
            task,
            session,
            janitor_passed=True,
            qg_result=None,
            completion_data=None,
            agent_just_reaped=True,
        )
        assert completion_cost == pytest.approx(expected_total)
        assert tracker.status().spent_usd == pytest.approx(completion_cost)
        assert (task_m.tokens_prompt, task_m.tokens_completion, task_m.tokens_used) == (3_000, 600, 3_600)
