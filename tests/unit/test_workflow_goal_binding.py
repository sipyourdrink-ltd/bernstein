"""The first workflow goal belongs to its run ID, including across resume entry points."""

from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.workflow_cmd import workflow_group
from bernstein.core import workflows
from bernstein.core.workflows import (
    NodeExecution,
    NodeStatus,
    WorkflowRunError,
    WorkflowRunner,
    load_workflow_spec_from_text,
)
from bernstein.core.workflows.workflow_runner import load_spec_snapshot, record_node_state, record_spec_snapshot

_GOAL = " original e\u0301 🪴 "
_MANIFEST = """name: sample
description: Synthetic checkpoint
version: "1.0.0"
nodes:
  - {id: first, agent: backend, prompt: first}
  - {id: next, agent: backend, prompt: 'continue {goal}', depends_on: [first]}
"""


def _files(root: Path, rid: str) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in (root / "runs" / rid).iterdir() if p.is_file()}


def _runner(root: Path):
    emitted = []
    spawner = Mock()
    spawner.spawn_for_tasks.side_effect = [SimpleNamespace(id="first-session"), KeyboardInterrupt()]
    runner = WorkflowRunner(workdir=root, spawner=spawner, audit_emitter=lambda *event: emitted.append(event))
    return runner, spawner, emitted


@pytest.mark.parametrize("entry", ["resume", "run"])
@pytest.mark.parametrize("changed", ["", _GOAL.strip(), _GOAL.replace("e\u0301", "é"), "entirely different"])
def test_interrupted_run_refuses_changed_goal_before_effects(tmp_path: Path, entry: str, changed: str) -> None:
    runner, spawner, events = _runner(tmp_path)
    spec = load_workflow_spec_from_text(_MANIFEST)
    with pytest.raises(KeyboardInterrupt):
        runner.run(spec, goal=_GOAL, run_id="partial")
    before = _files(tmp_path, "partial")
    events.clear()
    spawner.reset_mock()
    spawner.spawn_for_tasks.side_effect = lambda _tasks: SimpleNamespace(id="unexpected")
    with pytest.raises(WorkflowRunError, match="goal") as error:
        getattr(runner, entry)(spec, goal=changed, run_id="partial")
    assert changed == "" or changed not in str(error.value)
    assert _GOAL not in str(error.value)
    assert not any(e[0] in {"workflow.start", "workflow.resume", "workflow.node_start"} for e in events)
    spawner.spawn_for_tasks.assert_not_called()
    assert _files(tmp_path, "partial") == before

    spawner.spawn_for_tasks.side_effect = [SimpleNamespace(id="next-session")]
    final = getattr(runner, entry)(spec, goal=_GOAL, run_id="partial")
    assert final.succeeded
    assert [n.session_id for n in final.nodes] == ["first-session", "next-session"]
    assert spawner.spawn_for_tasks.call_args.args[0][0].description == "continue " + _GOAL
    assert any(e[2].get("goal") == _GOAL for e in events)
    assert _files(tmp_path, "partial")["first.node.json"] == before["first.node.json"]
    assert load_spec_snapshot(tmp_path, "partial")["goal_digest"] == hashlib.sha256(_GOAL.encode()).hexdigest()
    initial = json.loads(before["spec_snapshot.json"])
    assert set(initial) == {"version", "spec_name", "spec_version", "spec_digest", "node_ids", "source", "goal_digest"}
    assert _GOAL not in initial.values()


@pytest.mark.parametrize("bad_binding", [None, 0, "", "invalid-goal-text", "A" * 64, "0" * 63])
@pytest.mark.parametrize("entry", ["resume", "run"])
def test_invalid_present_binding_rejects_without_legacy_exemption(tmp_path: Path, bad_binding, entry: str) -> None:
    spec = load_workflow_spec_from_text(_MANIFEST.replace("continue {goal}", "independent"))
    record_spec_snapshot(tmp_path, "old", spec)
    path = tmp_path / "runs/old/spec_snapshot.json"
    payload = json.loads(path.read_bytes())
    payload["goal_digest"] = bad_binding
    path.write_text(json.dumps(payload), encoding="utf-8")
    before = _files(tmp_path, "old")
    runner, spawner, _ = _runner(tmp_path)
    spawner.spawn_for_tasks.side_effect = lambda _tasks: SimpleNamespace(id="unexpected")
    with pytest.raises(WorkflowRunError, match="goal") as error:
        getattr(runner, entry)(spec, run_id="old")
    assert "invalid-goal-text" not in str(error.value)
    assert _files(tmp_path, "old") == before
    spawner.spawn_for_tasks.assert_not_called()


@pytest.mark.parametrize(
    ("state", "dependency", "refuse"),
    [
        ("failed", "", True),
        ("failed", ", depends_on: [first]", False),
        ("condition-skipped", ", depends_on: [first]", True),
        ("blocked", ", depends_on: [first]", False),
        ("done-goal", ", depends_on: [first]", False),
        ("pending", ", depends_on: [first]", True),
    ],
)
@pytest.mark.parametrize("entry", ["resume", "run"])
def test_legacy_goal_reachability(tmp_path: Path, state: str, dependency: str, refuse: bool, entry: str) -> None:
    node = "agent: backend, prompt: 'original {goal}'" if state == "done-goal" else "command: synthetic-first"
    later = "static" if state == "done-goal" else "needs {goal}"
    text = _MANIFEST.split("nodes:")[0] + (
        f"nodes:\n  - {{id: first, {node}}}\n"
        f"  - {{id: next, agent: backend, prompt: '{later}', when: dynamic{dependency}}}\n"
    )
    spec = load_workflow_spec_from_text(text)
    digest = record_spec_snapshot(tmp_path, "legacy", spec)
    if state != "pending":
        status = {"failed": NodeStatus.FAILED, "done-goal": NodeStatus.SUCCESS}.get(state, NodeStatus.SKIPPED)
        record_node_state(
            tmp_path, "legacy", NodeExecution("first", status, condition_skipped=state == "condition-skipped"), digest
        )
    runner, spawner, events = _runner(tmp_path)
    runner._loop_predicate_passes = Mock(return_value=True)
    spawner.spawn_for_tasks.side_effect = [SimpleNamespace(id="static")]
    before = _files(tmp_path, "legacy")
    if refuse:
        with pytest.raises(WorkflowRunError, match="new run"):
            getattr(runner, entry)(spec, run_id="legacy", goal=_GOAL)
        assert _files(tmp_path, "legacy") == before
        runner._loop_predicate_passes.assert_not_called()
        spawner.spawn_for_tasks.assert_not_called()
        assert not any(e[0] in {"workflow.start", "workflow.resume"} for e in events)
    else:
        getattr(runner, entry)(spec, run_id="legacy", goal=_GOAL)
        assert spawner.spawn_for_tasks.call_count == int(state == "done-goal")
        assert _files(tmp_path, "legacy")["spec_snapshot.json"] == before["spec_snapshot.json"]


@pytest.mark.parametrize(("initial", "resumed"), [({}, {"goal": ""}), ({"goal": ""}, {})])
def test_bound_empty_goal_and_goal_independent_manifest(tmp_path: Path, initial: dict, resumed: dict) -> None:
    spec = load_workflow_spec_from_text(_MANIFEST.replace("continue {goal}", "static"))
    runner, spawner, _ = _runner(tmp_path)
    with pytest.raises(KeyboardInterrupt):
        runner.run(spec, run_id="empty", **initial)
    before = _files(tmp_path, "empty")
    with pytest.raises(WorkflowRunError, match="goal"):
        runner.resume(spec, run_id="empty", goal="new")
    assert _files(tmp_path, "empty") == before
    spawner.spawn_for_tasks.side_effect = [SimpleNamespace(id="resumed")]
    assert runner.resume(spec, run_id="empty", **resumed).succeeded


@pytest.mark.parametrize("entry", ["resume", "run"])
def test_original_manifest_and_finished_run_protections(tmp_path: Path, entry: str) -> None:
    spec = load_workflow_spec_from_text(_MANIFEST)
    changed = load_workflow_spec_from_text(_MANIFEST.replace("continue {goal}", "other"))
    runner, spawner, events = _runner(tmp_path)
    with pytest.raises(KeyboardInterrupt):
        runner.run(spec, run_id="same", goal=_GOAL)
    before = _files(tmp_path, "same")
    # An older helper caller must also preserve a bound snapshot.
    record_spec_snapshot(tmp_path, "same", spec, "other-source")
    events.clear()
    with pytest.raises(WorkflowRunError, match="spec digest mismatch"):
        getattr(runner, entry)(changed, run_id="same", goal=_GOAL)
    assert not events
    assert _files(tmp_path, "same") == before
    spawner.spawn_for_tasks.side_effect = [SimpleNamespace(id="last")]
    assert runner.resume(spec, run_id="same", goal=_GOAL).succeeded
    finished = _files(tmp_path, "same")
    events.clear()
    with pytest.raises(WorkflowRunError, match="already completed"):
        getattr(runner, entry)(spec, run_id="same", goal="unrecorded")
    assert not any("goal" in event[2] for event in events)
    assert _files(tmp_path, "same") == finished


def test_cli_run_and_resume_preserve_original_goal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner, spawner, events = _runner(tmp_path)
    monkeypatch.setattr(workflows, "WorkflowRunner", lambda **_: runner)
    manifest = tmp_path / "sample.yaml"
    manifest.write_text(_MANIFEST, encoding="utf-8")
    cli = CliRunner()
    start = cli.invoke(workflow_group, ["run", str(manifest), "-g", _GOAL])
    assert start.exit_code == 1
    rid = next((tmp_path / "runs").iterdir()).name
    before = _files(tmp_path, rid)
    events.clear()
    for extra in ([], ["-g", "different-input"]):
        rejected = cli.invoke(workflow_group, ["resume", rid, "-m", str(manifest), *extra])
        assert rejected.exit_code == 1 and "goal" in rejected.output.lower()
        assert "different-input" not in rejected.output and _GOAL not in rejected.output
        assert _files(tmp_path, rid) == before
    assert not any(e[0] == "workflow.resume" for e in events)
    spawner.spawn_for_tasks.side_effect = [SimpleNamespace(id="next")]
    result = cli.invoke(workflow_group, ["resume", rid, "-m", str(manifest), "-g", _GOAL])
    assert result.exit_code == 0, result.output
    assert spawner.spawn_for_tasks.call_args.args[0][0].description == "continue " + _GOAL
    finished = cli.invoke(workflow_group, ["resume", rid, "-g", "different-input"])
    assert finished.exit_code == 0 and "already completed" in finished.output


def test_different_goal_initializers_publish_one_winner(tmp_path: Path) -> None:
    spec = load_workflow_spec_from_text(_MANIFEST)
    entrants = threading.Barrier(2)
    finished = threading.Event()
    dispatches = []

    def start(goal):
        def dispatch(tasks):
            dispatches.append(goal)
            assert finished.wait(5)
            raise KeyboardInterrupt()

        spawner = Mock()
        spawner.spawn_for_tasks.side_effect = dispatch
        entrants.wait(5)
        try:
            WorkflowRunner(workdir=tmp_path, spawner=spawner).run(spec, run_id="race", goal=goal)
        except WorkflowRunError:
            finished.set()
            return "rejected", goal
        except KeyboardInterrupt:
            return "winner", goal
        raise AssertionError("expected interruption or goal rejection")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = dict(pool.map(start, ["alpha-test", "beta-test"]))
    assert set(results) == {"winner", "rejected"}
    assert dispatches == [results["winner"]]
    assert load_spec_snapshot(tmp_path, "race")["goal_digest"] == hashlib.sha256(results["winner"].encode()).hexdigest()
