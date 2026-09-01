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
# Conditional `when` gating (#4464)
# ---------------------------------------------------------------------------


def test_when_false_skips_node_without_failing_the_run(runner_workdir: Path) -> None:
    """A false `when` predicate skips its node but doesn't abort the DAG."""
    spec = _spec_from(
        """
name: conditional-skip
description: "A gated node whose predicate never passes"
version: "1.0.0"
nodes:
  - id: setup
    command: "true"
  - id: gated
    depends_on: [setup]
    command: "echo should-not-run > gated.txt"
    when: "false"
"""
    )
    runner = _build_runner(workdir=runner_workdir)
    execution = runner.run(spec)

    by_id = {n.node_id: n for n in execution.nodes}
    assert by_id["setup"].status == NodeStatus.SUCCESS
    assert by_id["gated"].status == NodeStatus.SKIPPED
    assert by_id["gated"].condition_skipped is True
    assert execution.succeeded is True
    assert not (runner_workdir / "gated.txt").exists()


def test_when_true_runs_the_node_normally(runner_workdir: Path) -> None:
    """A passing `when` predicate behaves like an ungated node."""
    spec = _spec_from(
        """
name: conditional-run
description: "A gated node whose predicate always passes"
version: "1.0.0"
nodes:
  - id: gated
    command: "echo ran > gated.txt"
    when: "true"
"""
    )
    runner = _build_runner(workdir=runner_workdir)
    execution = runner.run(spec)

    gated = execution.nodes[0]
    assert gated.status == NodeStatus.SUCCESS
    assert gated.condition_skipped is False
    assert (runner_workdir / "gated.txt").read_text().strip() == "ran"


def test_downstream_of_a_condition_skipped_node_still_runs(runner_workdir: Path) -> None:
    """A node depending on a condition-skipped node is not itself blocked.

    This is what distinguishes `when: false` from a failed dependency: the
    node was intentionally not needed, not aborted, so anything downstream
    that only needed the DAG to *reach* this point still proceeds.
    """
    spec = _spec_from(
        """
name: skip-does-not-cascade
description: "review -> gated revise -> verify, revise's `when` is false"
version: "1.0.0"
nodes:
  - id: review
    command: "true"
  - id: revise
    depends_on: [review]
    command: "echo revised > revise.txt"
    when: "false"
  - id: verify
    depends_on: [review, revise]
    command: "echo verified > verify.txt"
"""
    )
    runner = _build_runner(workdir=runner_workdir)
    execution = runner.run(spec)

    by_id = {n.node_id: n for n in execution.nodes}
    assert by_id["review"].status == NodeStatus.SUCCESS
    assert by_id["revise"].status == NodeStatus.SKIPPED
    assert by_id["revise"].condition_skipped is True
    assert by_id["verify"].status == NodeStatus.SUCCESS
    assert execution.succeeded is True
    assert (runner_workdir / "verify.txt").read_text().strip() == "verified"


def test_failure_skip_still_blocks_downstream_unlike_condition_skip(runner_workdir: Path) -> None:
    """A cascade skip (from a *failed* dependency) still blocks children.

    Only a `when`-gated skip is "intentionally not needed"; a node skipped
    because its own dependency failed must keep blocking the DAG exactly
    as it did before `when` existed.
    """
    spec = _spec_from(
        """
name: failure-still-cascades
description: "bad fails; never depends on bad and must stay skipped"
version: "1.0.0"
nodes:
  - id: bad
    command: "exit 9"
  - id: never
    depends_on: [bad]
    command: "echo nope > never.txt"
"""
    )
    runner = _build_runner(workdir=runner_workdir)
    execution = runner.run(spec)

    by_id = {n.node_id: n for n in execution.nodes}
    assert by_id["never"].status == NodeStatus.SKIPPED
    assert by_id["never"].condition_skipped is False
    assert execution.succeeded is False


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


# ---------------------------------------------------------------------------
# State persistence and resume functionality
# ---------------------------------------------------------------------------


def test_state_persistence_functions_work_correctly(tmp_path: Path) -> None:
    """Test that all state persistence functions work as expected."""
    from bernstein.core.workflows.workflow_runner import (
        SPEC_SNAPSHOT_FILE,
        _run_state_dir,
        _validated_run_id,
        load_node_state,
        load_spec_snapshot,
        record_node_state,
        record_run_complete,
        record_spec_snapshot,
        run_complete_marker_exists,
        spec_digest,
    )

    workdir = tmp_path / "workdir"
    workdir.mkdir()
    sdd_dir = workdir / ".sdd"
    sdd_dir.mkdir()

    # Create a simple workflow spec
    spec = _spec_from(
        """
name: test-workflow
description: "Test workflow for state persistence"
version: "1.0.0"
nodes:
  - id: node1
    command: "echo hello > output1.txt"
  - id: node2
    depends_on: [node1]
    command: "echo world > output2.txt"
"""
    )

    run_id = "test-run-123"

    # Test _validated_run_id accepts valid IDs
    assert _validated_run_id("test-run-123") == "test-run-123"
    assert _validated_run_id("run.ID-with_underscores") == "run.ID-with_underscores"

    # Test _validated_run_id rejects invalid IDs
    for invalid_id in [".", "..", "test/run", "test\\run", "", "a" * 129]:
        try:
            _validated_run_id(invalid_id)
            raise AssertionError(f"Expected WorkflowRunError for invalid run_id: {invalid_id}")
        except Exception:
            pass  # Expected

    # Test _run_state_dir returns correct path
    state_dir = _run_state_dir(sdd_dir, run_id)
    expected_dir = sdd_dir / "runs" / run_id
    assert state_dir == expected_dir

    # Test spec_digest produces consistent results
    digest1 = spec_digest(spec)
    digest2 = spec_digest(spec)
    assert digest1 == digest2
    assert len(digest1) == 64  # SHA-256 hex length

    # Test record_spec_snapshot and load_spec_snapshot
    recorded_digest = record_spec_snapshot(sdd_dir, run_id, spec, manifest_source="test.yaml")
    assert recorded_digest == digest1  # Should return the digest

    snapshot = load_spec_snapshot(sdd_dir, run_id)
    assert snapshot is not None
    assert snapshot["spec_name"] == spec.name
    assert snapshot["spec_version"] == spec.version
    assert snapshot["spec_digest"] == digest1
    assert snapshot["node_ids"] == ["node1", "node2"]
    assert snapshot["source"] == "test.yaml"
    assert snapshot["version"] == 1  # STATE_VERSION

    # Test record_node_state and load_node_state
    node1_exec = type(
        "NodeExecution",
        (),
        {
            "node_id": "node1",
            "status": NodeStatus.SUCCESS,
            "iterations": 1,
            "exit_code": 0,
            "stdout": "hello\n",
            "stderr": "",
            "session_id": "session-123",
            "error": "",
            "wall_time_seconds": 0.5,
            "condition_skipped": False,
        },
    )()

    record_node_state(sdd_dir, run_id, node1_exec, digest1)

    loaded_node1 = load_node_state(sdd_dir, run_id, "node1")
    assert loaded_node1 is not None
    assert loaded_node1["node_id"] == "node1"
    assert loaded_node1["status"] == "success"
    assert loaded_node1["iterations"] == 1
    assert loaded_node1["exit_code"] == 0
    assert loaded_node1["stdout"] == "hello\n"
    assert loaded_node1["stderr"] == ""
    assert loaded_node1["session_id"] == "session-123"
    assert loaded_node1["error"] == ""
    assert loaded_node1["wall_time_seconds"] == 0.5
    assert loaded_node1["condition_skipped"] is False
    assert loaded_node1["version"] == 1
    assert loaded_node1["spec_digest"] == digest1

    # Test loading non-existent node returns None
    assert load_node_state(sdd_dir, run_id, "nonexistent") is None

    # Test record_run_complete and run_complete_marker_exists
    record_run_complete(sdd_dir, run_id, succeeded=True)
    completion = run_complete_marker_exists(sdd_dir, run_id)
    assert completion is not None
    assert completion["succeeded"] is True
    assert "completed_at_epoch" in completion
    assert isinstance(completion["completed_at_epoch"], float)

    # Test loading non-existent completion returns None
    assert run_complete_marker_exists(sdd_dir, "nonexistent-run") is None

    # Test that corrupted JSON is handled gracefully
    corrupted_file = _run_state_dir(sdd_dir, run_id) / "node1.node.json"
    corrupted_file.write_text("{ invalid json", encoding="utf-8")
    assert load_node_state(sdd_dir, run_id, "node1") is None

    corrupted_file = _run_state_dir(sdd_dir, run_id) / SPEC_SNAPSHOT_FILE
    corrupted_file.write_text("{ invalid json", encoding="utf-8")
    assert load_spec_snapshot(sdd_dir, run_id) is None

    corrupted_file = _run_state_dir(sdd_dir, run_id) / "run_complete.json"
    corrupted_file.write_text("{ invalid json", encoding="utf-8")
    assert run_complete_marker_exists(sdd_dir, run_id) is None


def test_workflow_resume_works_correctly(tmp_path: Path) -> None:
    """Test that workflow resume correctly resumes from completed nodes."""

    workdir = tmp_path / "workdir"
    workdir.mkdir()
    sdd_dir = workdir / ".sdd"
    sdd_dir.mkdir()

    # Create a workflow spec that will fail on node 2
    spec = _spec_from(
        """
name: resumable-workflow
description: "Workflow that can be resumed"
version: "1.0.0"
nodes:
  - id: setup
    command: "mkdir -p output && echo setup > output/setup.txt"
  - id: fail-node
    depends_on: [setup]
    command: "exit 1"
  - id: verify
    depends_on: [fail-node]
    command: "echo verified > output/verify.txt"
"""
    )

    runner = WorkflowRunner(workdir=workdir)

    # Run the workflow initially - it will fail at fail-node
    run_id = "resume-test-123"
    execution1 = runner.run(spec, run_id=run_id)

    assert execution1.succeeded is False
    nodes_by_id = {n.node_id: n for n in execution1.nodes}
    assert nodes_by_id["setup"].status == NodeStatus.SUCCESS
    assert nodes_by_id["fail-node"].status == NodeStatus.FAILED
    assert nodes_by_id["verify"].status == NodeStatus.SKIPPED

    # Verify setup output was created
    assert (workdir / "output" / "setup.txt").read_text().strip() == "setup"
    # verify.txt should not exist since verify was skipped
    assert not (workdir / "output" / "verify.txt").exists()

    # Simulate a kill by removing the run-complete marker; a real runner
    # process would have been SIGKILL'd before writing it.
    # Note: the runner stores state under workdir/runs/, not workdir/.sdd/runs/
    run_complete_file = workdir / "runs" / run_id / "run_complete.json"
    run_complete_file.unlink(missing_ok=True)

    # Resume with the SAME spec - fail-node runs again and fails again
    execution2 = runner.resume(spec, goal="", run_id=run_id)
    assert execution2.succeeded is False

    # Completed nodes (setup) are loaded from state, not re-executed;
    # fail-node ran again and failed; verify was skipped again
    nodes_by_id = {n.node_id: n for n in execution2.nodes}
    assert nodes_by_id["setup"].status == NodeStatus.SUCCESS
    assert nodes_by_id["fail-node"].status == NodeStatus.FAILED
    assert nodes_by_id["verify"].status == NodeStatus.SKIPPED

    # Test resuming an already completed run fails
    try:
        runner.resume(spec, goal="", run_id=run_id)
        raise AssertionError("Expected WorkflowRunError for already completed run")
    except Exception as e:
        assert "already completed" in str(e).lower()

    # Test resume with modified spec fails with digest mismatch
    modified_spec = _spec_from(
        """
name: resumable-workflow
description: "Workflow that can be resumed - MODIFIED"
version: "1.0.0"
nodes:
  - id: setup
    command: "mkdir -p output && echo setup-modified > output/setup.txt"
  - id: fail-node
    depends_on: [setup]
    command: "echo fixed > output/fixed.txt"
  - id: verify
    depends_on: [fail-node]
    command: "echo verified > output/verify.txt"
"""
    )

    # Use a new run_id for this test to avoid the "already completed" error
    run_id2 = "resume-test-456"
    # First run with original spec
    runner.run(spec, run_id=run_id2)
    # Then try to resume with modified spec - should fail
    try:
        runner.resume(modified_spec, goal="", run_id=run_id2)
        raise AssertionError("Expected WorkflowRunError due to spec digest mismatch")
    except Exception as e:
        assert "spec digest mismatch" in str(e).lower()

    # Test resume with non-existent run_id fails
    try:
        runner.resume(spec, goal="", run_id="nonexistent-run")
        raise AssertionError("Expected WorkflowRunError for nonexistent run")
    except Exception as e:
        assert "no workflow run state" in str(e).lower()
