"""Command-loop interruptions use counted local fakes for every external effect."""

import json
import subprocess
from collections import Counter
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.workflow_cmd import workflow_group
from bernstein.core.workflows import workflow_runner as wr
from bernstein.core.workflows.workflow_spec import WorkflowSpec


class Interrupted(Exception):
    pass


def manifest(*, before=False, sibling=False, cap=2, when=True):
    loop = {"id": "loop", "command": "body", "loop": {"until": "until", "max_iterations": cap}}
    if when:
        loop["when"] = "outer"
    nodes = [loop]
    if before:
        loop["depends_on"] = ["prior"]
        nodes.insert(0, {"id": "prior", "command": "prior"})
    if sibling:
        nodes.append({"id": "early", "command": "sibling", "when": "sibling-condition"})
    return WorkflowSpec.model_validate({"name": "checks", "description": "Local counters", "nodes": nodes})


def calls(monkeypatch, *, stop_at=None, interrupted=None, outcome="exhausted"):
    counts = Counter()

    def execute(_runner, node, *, goal, run_id, iteration):
        label = node.command
        counts[label] += 1
        if interrupted == label:
            raise Interrupted()
        failed = outcome == "failed" and label == "body"
        return wr.NodeExecution(
            node_id=node.id,
            status=wr.NodeStatus.FAILED if failed else wr.NodeStatus.SUCCESS,
            exit_code=7 if failed else 0,
            stdout=f"{label} {iteration}",
            error="exit 7" if failed else "",
        )

    def predicate(_runner, label):
        counts[label] += 1
        if label == interrupted:
            raise Interrupted()
        if label == "until":
            if outcome == "timeout":
                raise subprocess.TimeoutExpired("fixture", 30)
            return counts[label] == stop_at
        return outcome != "skip"

    monkeypatch.setattr(wr.WorkflowRunner, "_execute_once", execute)
    monkeypatch.setattr(wr.WorkflowRunner, "_loop_predicate_passes", predicate)
    return counts


def halt_on(expected):
    def emit(event, *_):
        if event == expected:
            raise Interrupted()

    return emit


@pytest.mark.parametrize("method", ["run", "resume", "cli"])
def test_interrupted_loop_resumes_next_iteration_under_original_limit(tmp_path, monkeypatch, method):
    counts = calls(monkeypatch)
    spec = manifest()
    with pytest.raises(Interrupted):
        wr.WorkflowRunner(workdir=tmp_path, audit_emitter=halt_on("workflow.loop_continue")).run(spec, run_id="count")
    assert counts == {"body": 1, "until": 1, "outer": 1}
    if method == "cli":
        monkeypatch.chdir(tmp_path)
        source = tmp_path / "manifest.yaml"
        source.write_text(spec.model_dump_json(exclude_none=True))
        result = CliRunner().invoke(workflow_group, ["resume", "count", "--manifest", str(source)])
        assert result.exit_code == 1 and "exhausted" in result.output
    else:
        result = getattr(wr.WorkflowRunner(workdir=tmp_path), method)(spec, run_id="count")
        assert not result.succeeded and result.nodes[0].iterations == 2
    assert counts == {"body": 2, "until": 2, "outer": 1}
    assert wr.load_node_state(tmp_path, "count", "loop")["stdout"] == "body 2"
    with pytest.raises(wr.WorkflowRunError, match="already completed"):
        wr.WorkflowRunner(workdir=tmp_path).resume(spec, run_id="count")


@pytest.mark.parametrize("outcome", ["stop", "exhausted", "failed", "timeout", "skip"])
def test_terminal_decision_survives_loss_of_final_node_write(tmp_path, monkeypatch, outcome):
    spec = manifest(cap=1)
    counts = calls(monkeypatch, stop_at=1 if outcome == "stop" else None, outcome=outcome)
    runner = wr.WorkflowRunner(workdir=tmp_path)
    with monkeypatch.context() as m:
        m.setattr(wr, "record_node_state", lambda *a, **kw: (_ for _ in ()).throw(Interrupted()))
        with pytest.raises(Interrupted):
            runner.run(spec, run_id="terminal")
    before = dict(counts)
    result = runner.resume(spec, run_id="terminal")
    assert counts == before
    row = result.nodes[0]
    assert result.succeeded == (outcome in {"stop", "skip"})
    assert row.iterations == (0 if outcome == "skip" else 1)
    assert row.condition_skipped == (outcome == "skip")
    if outcome == "failed":
        assert row.exit_code == 7 and row.stdout == "body 1"
    if outcome == "timeout":
        assert "timed out" in row.error and row.exit_code is None


@pytest.mark.parametrize("effect", ["outer", "body", "until"])
def test_unknown_effect_refuses_before_any_sibling_or_rewrite(tmp_path, monkeypatch, effect):
    counts = calls(monkeypatch, interrupted=effect)
    spec = manifest(sibling=True)
    with pytest.raises(Interrupted):
        wr.WorkflowRunner(workdir=tmp_path, max_parallel=1).run(spec, run_id="partial")
    before = dict(counts)
    folder = tmp_path / "runs" / "partial"
    files = {p.name: p.read_bytes() for p in folder.iterdir()}
    for method in ["resume", "run"]:
        with pytest.raises(wr.WorkflowRunError, match="uncertain"):
            getattr(wr.WorkflowRunner(workdir=tmp_path), method)(spec, run_id="partial")
        assert counts == before and files == {p.name: p.read_bytes() for p in folder.iterdir()}


@pytest.mark.parametrize("method", ["run", "resume"])
@pytest.mark.parametrize("before", [False, True])
def test_legacy_snapshot_does_not_prove_loop_unentered(tmp_path, monkeypatch, method, before):
    counts = calls(monkeypatch, stop_at=1)
    spec = manifest(before=before, sibling=True)
    wr.record_spec_snapshot(tmp_path, "legacy", spec)
    prior = checkpoint_bytes(tmp_path, "legacy")
    with pytest.raises(wr.WorkflowRunError, match="uncertain legacy"):
        getattr(wr.WorkflowRunner(workdir=tmp_path), method)(spec, run_id="legacy")
    assert not counts and checkpoint_bytes(tmp_path, "legacy") == prior


def test_existing_run_checks_original_manifest_before_effects(tmp_path, monkeypatch):
    counts = calls(monkeypatch)
    with pytest.raises(Interrupted):
        wr.WorkflowRunner(workdir=tmp_path, audit_emitter=halt_on("workflow.loop_continue")).run(
            manifest(), run_id="drift"
        )
    original = wr.load_spec_snapshot(tmp_path, "drift")
    with pytest.raises(wr.WorkflowRunError, match="digest mismatch"):
        wr.WorkflowRunner(workdir=tmp_path).run(manifest(cap=3), run_id="drift")
    assert original == wr.load_spec_snapshot(tmp_path, "drift") and counts["body"] == 1


def _process_entry(path, method, entered, release, results):
    from unittest.mock import patch

    def command(self, node, **kw):
        with (path / "effects").open("a") as file:
            file.write("body\n")
        entered.set()
        if not release.wait(10):
            raise AssertionError("fixture release timed out")
        return wr.NodeExecution(node.id, wr.NodeStatus.SUCCESS, exit_code=0)

    def predicate(self, text):
        with (path / "effects").open("a") as file:
            file.write(text + "\n")
        return True

    try:
        with (
            patch.object(wr.WorkflowRunner, "_execute_once", command),
            patch.object(wr.WorkflowRunner, "_loop_predicate_passes", predicate),
        ):
            result = getattr(wr.WorkflowRunner(workdir=path), method)(manifest(when=False), run_id="contended")
        results.put((method, result.succeeded))
    except wr.WorkflowRunError as err:
        results.put((method, str(err)))


@pytest.mark.parametrize("first,second", [("run", "resume"), ("resume", "run")])
def test_process_exclusion_before_a_second_caller_can_dispatch(tmp_path, first, second):
    import multiprocessing

    runner = wr.WorkflowRunner(workdir=tmp_path, audit_emitter=halt_on("workflow.node_start"))
    with pytest.raises(Interrupted):
        runner.run(manifest(when=False), run_id="contended")
    ctx = multiprocessing.get_context("spawn")
    entered, release, results = ctx.Event(), ctx.Event(), ctx.Queue()
    a = ctx.Process(target=_process_entry, args=(tmp_path, first, entered, release, results))
    b = ctx.Process(target=_process_entry, args=(tmp_path, second, entered, release, results))
    a.start()
    try:
        assert entered.wait(8)
        b.start()
        losing = results.get(timeout=8)
        assert losing[0] == second and "active caller" in losing[1]
        assert (tmp_path / "effects").read_text() == "body\n"
    finally:
        release.set()
        a.join(8)
        if b.pid is not None:
            b.join(8)
    assert a.exitcode == b.exitcode == 0 and results.get(timeout=2) == (first, True)
    assert (tmp_path / "effects").read_text() == "body\nuntil\n"


def test_completed_command_can_resume_at_its_unstarted_stop_predicate(tmp_path, monkeypatch):
    counts = calls(monkeypatch, stop_at=1)
    runner = wr.WorkflowRunner(workdir=tmp_path)
    write = wr._CommandLoopState.write

    def interrupt_before_until(self, phase, iteration, result=None):
        if phase == "checking until":
            raise Interrupted()
        write(self, phase, iteration, result)

    with monkeypatch.context() as m:
        m.setattr(wr._CommandLoopState, "write", interrupt_before_until)
        with pytest.raises(Interrupted):
            runner.run(manifest(), run_id="body-finished")
    assert counts == {"outer": 1, "body": 1}
    assert runner.resume(manifest(), run_id="body-finished").succeeded
    assert counts == {"outer": 1, "body": 1, "until": 1}


def test_legacy_skippable_earlier_node_is_not_proof_of_unentered_next_layer(tmp_path, monkeypatch):
    spec = manifest(before=True)
    payload = spec.model_dump()
    payload["nodes"][0]["when"] = "optional"
    spec = WorkflowSpec.model_validate(payload)
    wr.record_spec_snapshot(tmp_path, "optional", spec)
    counts = calls(monkeypatch)
    with pytest.raises(wr.WorkflowRunError, match="uncertain legacy"):
        wr.WorkflowRunner(workdir=tmp_path).run(spec, run_id="optional")
    assert not counts


def test_completed_node_is_authoritative_and_uncertainty_preserves_every_checkpoint(tmp_path, monkeypatch):
    spec = manifest(sibling=True)
    counts = calls(monkeypatch)
    wr.record_spec_snapshot(tmp_path, "historic", spec)
    wr.record_node_state(
        tmp_path, "historic", wr.NodeExecution("loop", wr.NodeStatus.SUCCESS, iterations=1), wr.spec_digest(spec)
    )
    (tmp_path / "runs" / "historic" / "loop.loop.json").write_text("{unreadable")
    result = wr.WorkflowRunner(workdir=tmp_path).resume(spec, run_id="historic")
    assert result.succeeded and counts == {"sibling-condition": 1, "sibling": 1}


def checkpoint_bytes(root, run_id):
    return {p.name: p.read_bytes() for p in (root / "runs" / run_id).iterdir()}


@pytest.mark.parametrize("method", ["run", "resume"])
def test_restore_without_loop_record_refuses_before_sibling_effects(tmp_path, monkeypatch, method):
    spec = manifest(before=True, sibling=True)
    counts = calls(monkeypatch)
    with pytest.raises(Interrupted):
        wr.WorkflowRunner(workdir=tmp_path, audit_emitter=halt_on("workflow.loop_continue")).run(
            spec, run_id="missing-loop"
        )
    (tmp_path / "runs" / "missing-loop" / "loop.loop.json").unlink()
    before = checkpoint_bytes(tmp_path, "missing-loop")
    assert {"command_loops.json", "spec_snapshot.json", "prior.node.json"} <= before.keys()
    effects = dict(counts)
    with pytest.raises(wr.WorkflowRunError, match="uncertain command loop loop"):
        getattr(wr.WorkflowRunner(workdir=tmp_path), method)(spec, run_id="missing-loop")
    assert counts == effects and checkpoint_bytes(tmp_path, "missing-loop") == before


@pytest.mark.parametrize("method", ["run", "resume"])
def test_blank_command_agent_loop_is_not_checkpointed_as_command(tmp_path, monkeypatch, method):
    spec = WorkflowSpec.model_validate(
        {
            "name": "agent-loop",
            "description": "Test with a local fake session",
            "nodes": [
                {
                    "id": "review",
                    "agent": "local",
                    "prompt": "Compare",
                    "command": "  ",
                    "loop": {"until": "stop", "max_iterations": 2},
                }
            ],
        }
    )
    assert spec.nodes[0].kind == "agent"
    wr.record_spec_snapshot(tmp_path, "agent", spec)
    tasks = []

    def spawn(batch):
        tasks.extend(batch)
        return SimpleNamespace(id="synthetic-session")

    def stop(_runner, predicate):
        assert predicate == "stop"
        return len(tasks) == 2

    monkeypatch.setattr(wr.WorkflowRunner, "_loop_predicate_passes", stop)
    runner = wr.WorkflowRunner(workdir=tmp_path, spawner=SimpleNamespace(spawn_for_tasks=spawn))
    done = getattr(runner, method)(spec, run_id="agent")
    assert done.succeeded and done.nodes[0].iterations == 2
    assert [task.role for task in tasks] == ["local", "local"]
    assert done.nodes[0].session_id == "synthetic-session"
    assert set(checkpoint_bytes(tmp_path, "agent")) == {"spec_snapshot.json", "review.node.json", "run_complete.json"}


@pytest.mark.parametrize("method", ["run", "resume"])
@pytest.mark.parametrize("predicate", ["outer", "until"])
def test_definite_predicate_refusal_preserves_previous_known_progress(tmp_path, monkeypatch, method, predicate):
    spec = manifest(cap=1, when=predicate == "outer")
    counts = Counter()
    refuse = True

    def check(_runner, resource_id, kind):
        assert (resource_id, kind) == ("predicate", "predicate")
        if refuse and (predicate == "outer" or counts["body"]):
            raise wr.UntrustedWorkspaceError(tmp_path)

    def command(_runner, node, **kwargs):
        counts["body"] += 1
        return wr.NodeExecution(node.id, wr.NodeStatus.SUCCESS, exit_code=0, stdout="completed body")

    class LocalPredicate:
        returncode = 0

        def __init__(self, label, **kwargs):
            counts[label] += 1

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            pass

        def communicate(self, *, timeout):
            return b"", b""

    monkeypatch.setattr(wr.WorkflowRunner, "_require_trusted_workspace", check)
    runner = wr.WorkflowRunner(workdir=tmp_path)
    with monkeypatch.context() as fixture:
        fixture.setattr(wr.WorkflowRunner, "_execute_once", command)
        fixture.setattr(wr.subprocess, "Popen", LocalPredicate)
        with pytest.raises(wr.UntrustedWorkspaceError):
            runner.run(spec, run_id="refused")
        assert counts == ({"body": 1} if predicate == "until" else {})
        progress = json.loads(checkpoint_bytes(tmp_path, "refused")["loop.loop.json"])
        assert progress["phase"] == ("command finished" if predicate == "until" else "new")
        if predicate == "until":
            assert progress["result"]["stdout"] == "completed body"
        refuse = False
        done = getattr(runner, method)(spec, run_id="refused")
    assert counts == ({"body": 1, "until": 1, "outer": 1} if predicate == "outer" else {"body": 1, "until": 1})
    assert done.succeeded and done.nodes[0].stdout == "completed body" and done.nodes[0].iterations == 1


@pytest.mark.parametrize("published", [False, True])
@pytest.mark.parametrize("method", ["run", "resume"])
def test_interrupted_initialization_recovers_only_before_effects(tmp_path, monkeypatch, method, published):
    spec = manifest(before=True, sibling=True)
    counts = calls(monkeypatch, stop_at=1)
    emit = halt_on("workflow.spec_snapshot_recorded") if published else None
    writer = wr.record_spec_snapshot

    def interrupted_snapshot(*args, **kwargs):
        raise Interrupted()

    runner = wr.WorkflowRunner(workdir=tmp_path, audit_emitter=emit)
    with monkeypatch.context() as fixture:
        if not published:
            fixture.setattr(wr, "record_spec_snapshot", interrupted_snapshot)
        with pytest.raises(Interrupted):
            runner.run(spec, run_id="initializing")
    assert not counts
    prior = checkpoint_bytes(tmp_path, "initializing")
    assert set(prior) == ({"command_loops.json", "spec_snapshot.json"} if published else {"command_loops.json"})
    assert json.loads(prior["command_loops.json"])["phase"] == "initializing"
    assert wr.record_spec_snapshot is writer
    if not published and method == "resume":
        with pytest.raises(wr.WorkflowRunError, match="no workflow run state"):
            runner.resume(spec, run_id="initializing")
        assert checkpoint_bytes(tmp_path, "initializing") == prior
        assert not counts
        return
    done = getattr(wr.WorkflowRunner(workdir=tmp_path), method)(spec, run_id="initializing")
    assert done.succeeded
    final = checkpoint_bytes(tmp_path, "initializing")
    assert json.loads(final["command_loops.json"])["phase"] == "active"
    assert counts == {"prior": 1, "sibling-condition": 1, "sibling": 1, "outer": 1, "body": 1, "until": 1}


def test_inline_resume_records_snapshot_only_on_publish(tmp_path, monkeypatch):
    spec = manifest()
    counts = calls(monkeypatch, stop_at=2)
    events = []

    def cut(event, resource, details):
        events.append((event, resource, details))
        if event == "workflow.loop_continue":
            raise Interrupted()

    with pytest.raises(Interrupted):
        wr.WorkflowRunner(workdir=tmp_path, audit_emitter=cut).run(spec, run_id="audit")
    snapshot = tmp_path / "runs" / "audit" / "spec_snapshot.json"
    original, original_stat = snapshot.read_bytes(), snapshot.stat()
    assert [event for event, *_ in events].count("workflow.spec_snapshot_recorded") == 1
    assert (
        "workflow.spec_snapshot_recorded",
        spec.name,
        {
            "run_id": "audit",
            "spec_digest": wr.spec_digest(spec),
        },
    ) in events
    events.clear()
    done = wr.WorkflowRunner(workdir=tmp_path, audit_emitter=lambda *row: events.append(row)).run(spec, run_id="audit")
    assert done.succeeded and counts == {"outer": 1, "body": 2, "until": 2}
    assert [event for event, *_ in events] == [
        "workflow.start",
        "workflow.node_start",
        "workflow.node_finish",
        "workflow.finish",
    ]
    final_stat = snapshot.stat()
    assert snapshot.read_bytes() == original
    assert (final_stat.st_ino, final_stat.st_mtime_ns) == (original_stat.st_ino, original_stat.st_mtime_ns)
