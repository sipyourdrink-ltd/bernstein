"""Exercise workflow resume across a stopped CLI process, using local files as observable effects."""

from __future__ import annotations

import multiprocessing
import os
from functools import partial
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from bernstein.core.spawner import AgentSpawner
from click.testing import CliRunner

from bernstein.cli.commands.workflow_cmd import workflow_group
from bernstein.core.persistence.workspace import grant_workspace_trust
from bernstein.core.workflows import WorkflowRunner
from bernstein.core.workflows.workflow_runner import load_node_state
from tests.support.workflow_completion import _known_contract_gap, _ProposedContractGap

_STOPPED_AT_BOUNDARY = 73
_GOAL = "original-input"


def _run_until_event(workdir: Path, event: str, node: str) -> None:
    """Stop the CLI process at the requested boundary after its shell child has exited."""
    os.chdir(workdir)

    def interrupt(kind: str, resource: str, details: dict[str, object]) -> None:
        if kind == event and resource == node:
            (workdir / "interrupted-run").write_text(str(details["run_id"]), encoding="utf-8")
            os._exit(_STOPPED_AT_BOUNDARY)

    with patch("bernstein.core.workflows.WorkflowRunner", partial(WorkflowRunner, audit_emitter=interrupt)):
        workflow_group.main(["run", "flow.yaml", "--goal", _GOAL])


def _interrupt_run(workdir: Path, nodes: str, event: str, node: str) -> str:
    grant_workspace_trust(workdir)
    (workdir / "flow.yaml").write_text(
        'name: recovery\nversion: "1.0.0"\ndescription: Test interrupted recovery\nnodes:\n' + nodes, encoding="utf-8"
    )
    child = multiprocessing.get_context("spawn").Process(target=_run_until_event, args=(workdir, event, node))
    child.start()
    try:
        child.join(timeout=60)
        assert child.exitcode == _STOPPED_AT_BOUNDARY, f"did not reach {node} at {event}: exit {child.exitcode}"
    finally:
        if child.is_alive():
            child.kill()
            child.join(timeout=10)
        child.close()
    return (workdir / "interrupted-run").read_text(encoding="utf-8")


@pytest.mark.xfail(
    strict=True, raises=_ProposedContractGap, reason="Interrupted loop loses its iteration count (#7855)"
)
def test_resumed_command_loop_keeps_original_iteration_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    run_id = _interrupt_run(
        tmp_path,
        """  - id: tick
    command: "echo tick>>effects.txt"
    loop:
      until: "exit 1"
      max_iterations: 2
""",
        "workflow.loop_continue",
        "tick",
    )
    effects = tmp_path / "effects.txt"
    assert effects.read_text().splitlines() == ["tick"]
    resumed = CliRunner().invoke(workflow_group, ["resume", run_id, "-m", "flow.yaml", "-g", _GOAL])
    assert resumed.exit_code == 1 and isinstance(resumed.exception, SystemExit), resumed.output
    terminal = load_node_state(tmp_path, run_id, "tick")
    assert terminal is not None and terminal["status"] == "failed"
    assert "exhaust" in terminal["error"]
    writes = effects.read_text().splitlines()
    assert writes in (["tick", "tick"], ["tick", "tick", "tick"]), writes
    with _known_contract_gap():
        assert writes == ["tick", "tick"]


@pytest.mark.parametrize(
    "resume_goal",
    [
        pytest.param(_GOAL, id="same-input-control"),
        pytest.param(
            "replacement-input",
            id="changed-input",
            marks=pytest.mark.xfail(
                strict=True, raises=_ProposedContractGap, reason="Original goal is unbound (#7856)"
            ),
        ),
    ],
)
def test_resumed_prompt_uses_original_goal_or_refuses_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resume_goal: str
) -> None:
    monkeypatch.chdir(tmp_path)
    run_id = _interrupt_run(
        tmp_path,
        """  - id: setup
    command: "echo setup>>effects.txt"
  - id: consume
    depends_on: [setup]
    agent: backend
    prompt: "Continue {goal}"
""",
        "workflow.node_start",
        "consume",
    )
    setup = load_node_state(tmp_path, run_id, "setup")
    assert setup is not None and setup["status"] == "success"
    assert load_node_state(tmp_path, run_id, "consume") is None
    spawner = Mock(spec=AgentSpawner)
    spawner.spawn_for_tasks.side_effect = RuntimeError("fixture stopped at dispatch")
    with patch("bernstein.core.workflows.WorkflowRunner", partial(WorkflowRunner, spawner=spawner)):
        resumed = CliRunner().invoke(workflow_group, ["resume", run_id, "-m", "flow.yaml", "-g", resume_goal])
    assert resumed.exit_code == 1 and isinstance(resumed.exception, SystemExit), resumed.output
    assert (tmp_path / "effects.txt").read_text().splitlines() == ["setup"]

    if resume_goal != _GOAL and not spawner.spawn_for_tasks.called:
        reason = str(resumed.exception.__cause__).lower()
        assert "goal" in reason or "input" in reason, resumed.output
        return
    spawner.spawn_for_tasks.assert_called_once()
    (task,) = spawner.spawn_for_tasks.call_args.args[0]
    assert task.description in {f"Continue {_GOAL}", f"Continue {resume_goal}"}
    with _known_contract_gap():
        assert task.description == f"Continue {_GOAL}"
