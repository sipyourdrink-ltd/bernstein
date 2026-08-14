"""Integration tests for the WorkflowRunner.

Covers linear / fan-out / loop / fresh-context / interactive node
behaviour, plus end-to-end agent dispatch through a real
:class:`AgentSpawner` backed by the fake-CLI fixture.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from bernstein.core.models import ModelConfig
from bernstein.core.spawner import AgentSpawner

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult
from bernstein.core.workflows import (
    NodeStatus,
    WorkflowExecution,
    WorkflowRunner,
    WorkflowSpec,
    load_workflow_spec_from_text,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def runner_workdir(tmp_path: Path) -> Path:
    """Provide a clean working directory for command nodes."""
    workdir = tmp_path / "run"
    workdir.mkdir()
    return workdir


@pytest.fixture
def captured_audit() -> tuple[list[tuple[str, str, dict[str, Any]]], Callable[..., None]]:
    """Provide an in-memory audit emitter for assertions."""
    log: list[tuple[str, str, dict[str, Any]]] = []

    def _emit(event_type: str, resource_id: str, details: dict[str, Any]) -> None:
        log.append((event_type, resource_id, details.copy()))

    return log, _emit


def _build_runner(
    *,
    workdir: Path,
    audit: Callable[..., None] | None = None,
    spawner: AgentSpawner | None = None,
) -> WorkflowRunner:
    """Construct a runner with the supplied workdir/audit."""
    return WorkflowRunner(spawner=spawner, workdir=workdir, audit_emitter=audit)


def _spec_from(text: str) -> WorkflowSpec:
    """Load a manifest from inline YAML text."""
    return load_workflow_spec_from_text(text)


# ---------------------------------------------------------------------------
# Linear command-only DAG
# ---------------------------------------------------------------------------


def test_linear_command_dag_runs_each_node_once(runner_workdir: Path) -> None:
    """A simple linear DAG of command nodes runs in order, all green."""
    spec = _spec_from(
        """
name: linear-cmds
description: "Three echo steps"
version: "1.0.0"
nodes:
  - id: first
    command: "echo first > stage1.txt"
  - id: second
    depends_on: [first]
    command: "echo second > stage2.txt"
  - id: third
    depends_on: [second]
    command: "echo third > stage3.txt"
"""
    )
    runner = _build_runner(workdir=runner_workdir)
    execution = runner.run(spec)

    assert execution.succeeded is True
    assert [n.node_id for n in execution.nodes] == ["first", "second", "third"]
    assert all(n.iterations == 1 for n in execution.nodes)
    assert (runner_workdir / "stage1.txt").read_text().strip() == "first"
    assert (runner_workdir / "stage3.txt").read_text().strip() == "third"


def test_failing_command_fails_run_and_skips_downstream(runner_workdir: Path) -> None:
    """A failing node aborts the DAG; downstream nodes are SKIPPED."""
    spec = _spec_from(
        """
name: fail-fast
description: "Middle step exits non-zero"
version: "1.0.0"
nodes:
  - id: ok
    command: "true"
  - id: bad
    depends_on: [ok]
    command: "exit 7"
  - id: never
    depends_on: [bad]
    command: "echo nope > shouldnt-exist.txt"
"""
    )
    runner = _build_runner(workdir=runner_workdir)
    execution = runner.run(spec)

    by_id = {n.node_id: n for n in execution.nodes}
    assert execution.succeeded is False
    assert by_id["ok"].status == NodeStatus.SUCCESS
    assert by_id["bad"].status == NodeStatus.FAILED
    assert by_id["bad"].exit_code == 7
    assert by_id["never"].status == NodeStatus.SKIPPED
    assert not (runner_workdir / "shouldnt-exist.txt").exists()


# ---------------------------------------------------------------------------
# Fan-out parallel
# ---------------------------------------------------------------------------


def test_fan_out_runs_leaves_in_parallel(runner_workdir: Path) -> None:
    """Parallel leaves both run and produce their outputs."""
    spec = _spec_from(
        """
name: fan-out
description: "Two leaves off one root"
version: "1.0.0"
nodes:
  - id: root
    command: "echo root > root.txt"
  - id: leaf-a
    depends_on: [root]
    command: "echo a > a.txt"
  - id: leaf-b
    depends_on: [root]
    command: "echo b > b.txt"
"""
    )
    runner = _build_runner(workdir=runner_workdir)
    execution = runner.run(spec)
    assert execution.succeeded is True
    assert (runner_workdir / "a.txt").read_text().strip() == "a"
    assert (runner_workdir / "b.txt").read_text().strip() == "b"


# ---------------------------------------------------------------------------
# Loop until predicate passes
# ---------------------------------------------------------------------------


def test_loop_until_predicate_passes(runner_workdir: Path) -> None:
    """A loop fires repeatedly until the predicate exits 0."""
    counter = runner_workdir / "counter.txt"
    counter.write_text("0\n", encoding="utf-8")

    spec = _spec_from(
        f"""
name: loop-pass
description: "Increment until counter >= 3"
version: "1.0.0"
nodes:
  - id: tick
    command: "n=$(cat {counter}); echo $((n+1)) > {counter}"
    loop:
      until: "test $(cat {counter}) -ge 3"
      max_iterations: 10
"""
    )
    runner = _build_runner(workdir=runner_workdir)
    execution = runner.run(spec)

    tick = execution.nodes[0]
    assert tick.status == NodeStatus.SUCCESS
    assert tick.iterations >= 3
    assert int(counter.read_text().strip()) >= 3


def test_loop_max_iterations_exhausted_fails(runner_workdir: Path) -> None:
    """A loop whose predicate never passes fails after max_iterations."""
    spec = _spec_from(
        """
name: loop-exhaust
description: "Predicate never passes"
version: "1.0.0"
nodes:
  - id: spin
    command: "true"
    loop:
      until: "false"
      max_iterations: 3
"""
    )
    runner = _build_runner(workdir=runner_workdir)
    execution = runner.run(spec)
    spin = execution.nodes[0]
    assert spin.status == NodeStatus.FAILED
    assert spin.iterations == 3
    assert "exhausted" in spin.error


# ---------------------------------------------------------------------------
# Fresh context iteration
# ---------------------------------------------------------------------------


def test_fresh_context_uses_distinct_task_ids_per_iteration(
    runner_workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`fresh_context: true` mints a fresh task id per loop iteration."""
    captured: list[str] = []

    class StubSpawner:
        """Stub that captures task ids and pretends every spawn is fine."""

        def spawn_for_tasks(self, tasks: list[Any]) -> Any:
            captured.append(tasks[0].id)
            session = MagicMock()
            session.id = f"sess-{len(captured)}"
            return session

    flag = runner_workdir / "loop-done.txt"
    spec = _spec_from(
        f"""
name: fresh-loop
description: "Fresh context every iteration"
version: "1.0.0"
nodes:
  - id: think
    agent: backend
    prompt: "Think about goal: {{goal}}"
    fresh_context: true
    loop:
      until: "test -f {flag}"
      max_iterations: 4
  - id: stop
    depends_on: [think]
    command: "echo done"
"""
    )

    # Make the predicate flip to passing on the second iteration.
    iterations = {"count": 0}
    real_runner = WorkflowRunner(spawner=None, workdir=runner_workdir)

    original_predicate = real_runner._loop_predicate_passes

    def _flip(predicate: str) -> bool:
        iterations["count"] += 1
        if iterations["count"] >= 2:
            flag.write_text("ok", encoding="utf-8")
        return original_predicate(predicate)

    monkeypatch.setattr(real_runner, "_loop_predicate_passes", _flip)
    real_runner._spawner = StubSpawner()  # type: ignore[assignment]
    execution = real_runner.run(spec, goal="JWT auth")

    assert execution.succeeded is True
    think = execution.nodes[0]
    assert think.iterations == 2
    # Each iteration receives a fresh task id ("@iter1", "@iter2", ...).
    assert any("@iter1" in tid for tid in captured)
    assert any("@iter2" in tid for tid in captured)


def test_agent_node_forwards_routing_hints_to_task(runner_workdir: Path) -> None:
    """Workflow routing hints reach the Task handed to the spawner."""
    captured: list[Any] = []

    class StubSpawner:
        def spawn_for_tasks(self, tasks: list[Any]) -> Any:
            captured.append(tasks[0])
            session = MagicMock()
            session.id = "sess-routed"
            return session

    spec = _spec_from(
        """
name: routed-agent
description: "Route one workflow node"
version: "1.0.0"
nodes:
  - id: review
    agent: reviewer
    prompt: "Review {goal}"
    cli: pi
    model: provider/model-name
    effort: high
"""
    )
    execution = WorkflowRunner(spawner=StubSpawner(), workdir=runner_workdir).run(spec, goal="the patch")

    assert execution.succeeded is True
    assert len(captured) == 1
    assert captured[0].cli == "pi"
    assert captured[0].model == "provider/model-name"
    assert captured[0].effort == "high"


# ---------------------------------------------------------------------------
# Interactive stub for #1110
# ---------------------------------------------------------------------------


def test_interactive_node_rejected_at_load_time(runner_workdir: Path) -> None:
    """`interactive: true` fails fast at load time with #1110 reference."""
    from bernstein.core.workflows.workflow_spec import WorkflowSpecError

    with pytest.raises(WorkflowSpecError, match="#1110"):
        _spec_from(
            """
name: needs-approval
description: "Has a human gate"
version: "1.0.0"
nodes:
  - id: gate
    command: "true"
    interactive: true
"""
        )


def test_interactive_node_defence_in_depth_in_runner(runner_workdir: Path) -> None:
    """The runner keeps a defence-in-depth `NotImplementedError` for out-of-band loaders.

    A caller that bypasses :func:`load_workflow_spec_from_text` (e.g. via
    ``model_construct``) must still trip the runner's stub instead of running
    an unsupported node.
    """
    from bernstein.core.workflows.workflow_spec import WorkflowNode

    # ``model_construct`` skips validators, simulating an out-of-band loader.
    gate_node = WorkflowNode.model_construct(
        id="gate",
        depends_on=[],
        command="true",
        agent=None,
        prompt=None,
        loop=None,
        fresh_context=False,
        interactive=True,
        timeout_seconds=1800,
    )
    spec = WorkflowSpec.model_construct(
        name="needs-approval",
        description="Has a human gate",
        version="1.0.0",
        nodes=[gate_node],
    )
    runner = _build_runner(workdir=runner_workdir)
    with pytest.raises(NotImplementedError, match="#1110"):
        runner.run(spec)


# ---------------------------------------------------------------------------
# Real AgentSpawner dispatch via fake-CLI fixture
# ---------------------------------------------------------------------------


class _RecordingMockAdapter(CLIAdapter):
    """Minimal adapter that records every spawn call.

    Returns a fake :class:`SpawnResult` and exits immediately.  Lets the
    integration test confirm that an agent-typed workflow node travels
    through ``AgentSpawner.spawn_for_tasks`` end-to-end without bringing
    up the full subprocess pipeline.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def spawn(
        self,
        *,
        prompt: str,
        workdir: Path,
        model_config: ModelConfig,
        session_id: str,
        mcp_config: dict[str, Any] | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        task_scope: str = "medium",
        budget_multiplier: float = 1.0,
        system_addendum: str = "",
    ) -> SpawnResult:
        log_path = workdir / "agent.log"
        log_path.write_text("ok", encoding="utf-8")
        self.calls.append({"prompt": prompt, "session_id": session_id})
        return SpawnResult(pid=0, log_path=log_path)

    def name(self) -> str:
        return "recording-mock"

    def is_alive(self, pid: int) -> bool:
        return False

    def is_rate_limited(self) -> bool:
        return False

    def kill(self, pid: int) -> None:
        return None


def test_agent_node_dispatches_through_real_spawner(tmp_path: Path) -> None:
    """An agent-typed node reaches `AgentSpawner.spawn_for_tasks`."""
    workdir = tmp_path / "proj"
    workdir.mkdir()
    templates_dir = workdir / "templates" / "roles" / "backend"
    templates_dir.mkdir(parents=True)
    (templates_dir / "system_prompt.md").write_text("You are a backend specialist.")

    adapter = _RecordingMockAdapter()
    spawner = AgentSpawner(
        adapter=adapter,
        templates_dir=workdir / "templates" / "roles",
        workdir=workdir,
        use_worktrees=False,
        default_model="mock-model",
    )

    spec = _spec_from(
        """
name: agent-real
description: "One agent node, dispatched via real spawner"
version: "1.0.0"
nodes:
  - id: code-it
    agent: backend
    prompt: "Implement {goal}"
"""
    )
    runner = WorkflowRunner(spawner=spawner, workdir=workdir)
    execution = runner.run(spec, goal="JWT auth")

    assert execution.succeeded is True
    code_it = execution.nodes[0]
    assert code_it.status == NodeStatus.SUCCESS
    assert code_it.session_id  # populated by spawn_for_tasks
    assert adapter.calls, "expected the adapter to be invoked"
    rendered = adapter.calls[0]["prompt"]
    assert "JWT auth" in rendered, "goal substitution must happen before the spawn"


def test_agent_node_without_spawner_fails_node(runner_workdir: Path) -> None:
    """An agent-typed node with no spawner produces a FAILED node."""
    spec = _spec_from(
        """
name: orphan-agent
description: "Agent without a spawner"
version: "1.0.0"
nodes:
  - id: lonely
    agent: backend
    prompt: "Do work for {goal}"
"""
    )
    runner = _build_runner(workdir=runner_workdir, spawner=None)
    execution = runner.run(spec, goal="testing")
    assert execution.succeeded is False
    assert execution.nodes[0].status == NodeStatus.FAILED
    assert "AgentSpawner" in execution.nodes[0].error


# ---------------------------------------------------------------------------
# Audit emit
# ---------------------------------------------------------------------------


def test_audit_emits_start_finish_and_per_node_events(
    runner_workdir: Path,
    captured_audit: tuple[list[tuple[str, str, dict[str, Any]]], Callable[..., None]],
) -> None:
    """The runner emits a start, per-node, and finish event sequence."""
    log, emitter = captured_audit
    spec = _spec_from(
        """
name: audited
description: "One simple node"
version: "1.0.0"
nodes:
  - id: only
    command: "true"
"""
    )
    runner = _build_runner(workdir=runner_workdir, audit=emitter)
    runner.run(spec)

    types = [event for event, _, _ in log]
    assert "workflow.start" in types
    assert "workflow.node_start" in types
    assert "workflow.node_finish" in types
    assert "workflow.finish" in types


# ---------------------------------------------------------------------------
# Malformed YAML / missing template / loop predicate exhaustion
# ---------------------------------------------------------------------------


def test_malformed_yaml_raises_at_load_time(tmp_path: Path) -> None:
    """Malformed YAML raises before the runner ever sees it."""
    from bernstein.core.workflows import WorkflowSpecError, load_workflow_spec

    bad = tmp_path / "broken.yaml"
    bad.write_text("name: [\n", encoding="utf-8")
    with pytest.raises(WorkflowSpecError):
        load_workflow_spec(bad)


def test_resolve_missing_template_raises_clearly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Asking for an unknown workflow name raises with a useful message."""
    from bernstein.core.workflows import WorkflowSpecError
    from bernstein.core.workflows.workflow_spec import resolve_workflow

    monkeypatch.setenv("HOME", str(tmp_path / "no-home"))
    with pytest.raises(WorkflowSpecError, match="not found"):
        resolve_workflow("does-not-exist-anywhere", workdir=tmp_path / "no-proj")


def test_command_timeout_marks_node_failed(runner_workdir: Path) -> None:
    """A command that exceeds its timeout is FAILED with exit_code=None."""
    spec = _spec_from(
        """
name: slow-cmd
description: "Sleep longer than allowed"
version: "1.0.0"
nodes:
  - id: snooze
    command: "sleep 5"
    timeout_seconds: 1
"""
    )
    runner = _build_runner(workdir=runner_workdir)
    execution = runner.run(spec)
    snooze = execution.nodes[0]
    assert snooze.status == NodeStatus.FAILED
    assert snooze.exit_code is None
    assert "timed out" in snooze.error


def test_succeeded_flag_consistent_with_node_statuses(runner_workdir: Path) -> None:
    """`execution.succeeded` is True iff every node SUCCEEDED."""
    spec = _spec_from(
        """
name: split-outcome
description: "One pass, one fail"
version: "1.0.0"
nodes:
  - id: pass
    command: "true"
  - id: fail
    depends_on: [pass]
    command: "exit 1"
"""
    )
    runner = _build_runner(workdir=runner_workdir)
    execution: WorkflowExecution = runner.run(spec)
    assert execution.succeeded is False
    assert any(n.status == NodeStatus.FAILED for n in execution.nodes)
