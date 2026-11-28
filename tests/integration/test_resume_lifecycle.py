"""Integration tests for ``bernstein resume`` end-to-end.

These tests exercise the full flow:
* Write checkpoints across multiple "steps" simulating a real task run.
* Kill the orchestrator mid-step → verify ``bernstein resume`` picks up
  from the *next* step boundary, never the killed step.
* Adapter without a native ``resume()`` falls back to fresh + recovered
  scratchpad re-injection.
* Corrupt trace → actionable error.
* ``resume_count`` increments on each invocation.

No live network, no spawned subprocesses - we drive the public API
surface directly (CLI runner + the storage + adapter contract).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.adapters._contract import (
    RESUME_FALLBACK_FRESH,
    RESUME_NATIVE,
    resume_capability,
)
from bernstein.adapters.base import CLIAdapter
from bernstein.cli.commands.ledger_cmd import ledger_group
from bernstein.cli.commands.resume_cmd import (
    EXIT_CORRUPT,
    EXIT_NO_CHECKPOINT,
    EXIT_OK,
    prepare_resume,
    resume_cmd,
)
from bernstein.core.lifecycle.hooks import HookRegistry, LifecycleEvent
from bernstein.core.persistence.resume_prompt import build_resume_context
from bernstein.core.persistence.task_resume import (
    CHECKPOINT_FILENAME,
    ResumeDispatchStatus,
    TaskResumeCheckpoint,
    checkpoint_dir_for,
    load_checkpoint,
    load_resume_request,
    read_resume_requests,
    resume_request_acknowledged,
    save_checkpoint,
    scratchpad_sha256,
)
from bernstein.core.persistence.work_ledger import (
    KIND_RUN_OPEN,
    KIND_TASK_SCHEDULED,
    WorkLedger,
    run_ledger_dir,
)
from bernstein.core.run_service.supervisor import advance_run, resume_checkpoint_request


@pytest.fixture()
def project_root(tmp_path: Path) -> Path:
    """Standalone project root with the runtime tree pre-created."""
    (tmp_path / ".sdd" / "runtime").mkdir(parents=True)
    return tmp_path


@pytest.mark.parametrize("source", ["checkpoint", "ledger"])
def test_failed_signal_replacement_preserves_previous_request(
    source: str, project_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = "in-flight"
    if source == "checkpoint":
        save_checkpoint(project_root, TaskResumeCheckpoint(task_id=task_id, adapter="mock"))
        command, args = resume_cmd, [task_id]
    else:
        run_id = "recovery"
        ledger = WorkLedger.open(run_ledger_dir(project_root / ".sdd", run_id))
        ledger.append(kind=KIND_TASK_SCHEDULED, task_id=task_id)
        ledger.close()
        command, args = ledger_group, ["resume", run_id]
    args.extend(["--workdir", str(project_root), "--json"])
    runner = CliRunner()
    first = runner.invoke(command, args)
    assert first.exit_code == 0, first.output
    signal = Path(json.loads(first.stdout)["signal_paths"][0])
    previous = signal.read_bytes()
    replace = os.replace

    def interrupt_publish(staging: str | Path, destination: str | Path) -> None:
        if Path(destination) == signal:
            assert json.loads(Path(staging).read_bytes())["task_id"] == task_id
            raise OSError("interrupted before publishing the signal")
        replace(staging, destination)

    monkeypatch.setattr(os, "replace", interrupt_publish)
    second = runner.invoke(command, args)
    assert second.exit_code == 1, second.output
    assert "interrupted before publishing" in json.loads(second.stdout)["error"]
    assert signal.read_bytes() == previous


def _simulate_step_transitions(
    workdir: Path,
    task_id: str,
    *,
    completed_steps: list[str],
    scratchpad_text: str,
) -> Path:
    """Walk through N step completions, persisting after each transition.

    Mirrors what the orchestrator does on the live path: after every
    successful step it writes a fresh checkpoint with the bumped
    ``last_completed_step_id`` and the latest trace cursor.

    Returns the scratchpad path so the caller can re-use it for the
    fresh-fallback assertion below.
    """
    scratchpad = workdir / ".sdd" / "runtime" / "scratchpads" / f"{task_id}.md"
    scratchpad.parent.mkdir(parents=True, exist_ok=True)
    scratchpad.write_text(scratchpad_text, encoding="utf-8")

    cursor = 0
    for step_id in completed_steps:
        cursor += 256  # pretend the trace grew by 256B per step
        cp = TaskResumeCheckpoint(
            task_id=task_id,
            last_completed_step_id=step_id,
            trace_cursor=cursor,
            scratchpad_path=str(scratchpad),
            scratchpad_sha256=scratchpad_sha256(scratchpad),
            adapter="aider",  # fallback-fresh in the matrix
            adapter_session_id="adapter-sess-xyz",
            worktree_path=str(workdir / ".sdd" / "runtime" / "worktrees" / task_id),
        )
        save_checkpoint(workdir, cp)
    return scratchpad


def test_mid_step_kill_resumes_from_next_step_boundary(project_root: Path) -> None:
    """Kill mid-step → ``resume`` picks up at the next boundary, not from step 1."""
    _simulate_step_transitions(
        project_root,
        task_id="midkill",
        completed_steps=["step-1", "step-2", "step-3"],
        scratchpad_text="picked up halfway through step-4\n",
    )

    runner = CliRunner()
    result = runner.invoke(
        resume_cmd,
        ["midkill", "--workdir", str(project_root)],
    )
    assert result.exit_code == EXIT_OK, result.output

    # Persisted state: last completed step is the latest checkpoint, not
    # the one we crashed on.
    persisted = load_checkpoint(project_root, "midkill")
    assert persisted.last_completed_step_id == "step-3"
    assert persisted.trace_cursor == 768  # 3 * 256
    assert persisted.resume_count == 1

    # Signal payload tells the orchestrator the same thing.
    signal = project_root / ".sdd" / "runtime" / "resume" / "midkill.signal"
    payload = json.loads(signal.read_text(encoding="utf-8"))
    assert payload["resume_count"] == 1
    assert payload["adapter"] == "aider"


def test_corrupt_trace_exits_with_actionable_error(project_root: Path) -> None:
    bad_dir = checkpoint_dir_for(project_root, "corrupt")
    bad_dir.mkdir(parents=True)
    (bad_dir / CHECKPOINT_FILENAME).write_text(
        '{"task_id": "corrupt", "trace_cursor": -42}',
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        resume_cmd,
        ["corrupt", "--workdir", str(project_root)],
    )
    assert result.exit_code == EXIT_CORRUPT
    # The operator should be told what to do next.
    assert ".sdd/runtime/checkpoints" in result.output
    assert "fresh" in result.output


def test_no_checkpoint_exits_with_actionable_error(project_root: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        resume_cmd,
        ["never-seen", "--workdir", str(project_root)],
    )
    assert result.exit_code == EXIT_NO_CHECKPOINT
    assert "bernstein run" in result.output


def test_resume_count_increments_across_invocations(project_root: Path) -> None:
    _simulate_step_transitions(
        project_root,
        task_id="flaky",
        completed_steps=["a", "b"],
        scratchpad_text="",
    )
    runner = CliRunner()
    for expected in (1, 2, 3):
        result = runner.invoke(
            resume_cmd,
            ["flaky", "--workdir", str(project_root), "--dry-run"],
        )
        assert result.exit_code == EXIT_OK, result.output
        assert load_checkpoint(project_root, "flaky").resume_count == expected


def test_fallback_fresh_adapter_gets_scratchpad_reinjection(project_root: Path) -> None:
    """Adapters without ``resume()`` fall back to fresh + scratchpad block."""
    _simulate_step_transitions(
        project_root,
        task_id="fallback",
        completed_steps=["s1", "s2"],
        scratchpad_text="REMEMBER: we already wrote tests/foo.py\n",
    )

    plan = prepare_resume(project_root, "fallback")
    # The aider adapter declares fallback-fresh in the matrix.
    assert plan.capability == RESUME_FALLBACK_FRESH

    # The recovered scratchpad must surface in the prompt block so the
    # fresh agent has the continuity it needs.
    assert "REMEMBER: we already wrote tests/foo.py" in plan.resume_context

    # And the default CLIAdapter.resume() returns None - the orchestrator
    # uses that to decide "fall back to fresh".
    class _DefaultAdapter(CLIAdapter):
        def spawn(self, **_kwargs: object) -> object:  # type: ignore[override]
            raise AssertionError("not called in this test")

        def name(self) -> str:
            return "aider"

    adapter = _DefaultAdapter()
    assert adapter.resume("adapter-sess-xyz", {"prompt": plan.resume_context}) is None


def test_native_resume_adapter_can_return_reattachment(project_root: Path) -> None:
    """Adapters that override ``resume()`` return a SpawnResult.

    We don't actually spawn a process here - we just verify the override
    contract works without needing a real subprocess.
    """
    _simulate_step_transitions(
        project_root,
        task_id="native",
        completed_steps=["s1"],
        scratchpad_text="",
    )

    # Pretend the claude adapter overrides resume() to return a sentinel.
    sentinel = object()

    class _Reattaching(CLIAdapter):
        def spawn(self, **_kwargs: object) -> object:  # type: ignore[override]
            raise AssertionError("native resume avoids spawn")

        def name(self) -> str:
            return "claude"

        def resume(  # type: ignore[override]
            self,
            _session_id: str,
            _context: dict[str, object],
        ) -> object | None:
            return sentinel

    adapter = _Reattaching()
    result = adapter.resume("adapter-sess-xyz", {})
    assert result is sentinel
    # Matrix still says native for claude.
    assert resume_capability("claude") == RESUME_NATIVE


def test_task_resume_lifecycle_hook_payload_is_complete(project_root: Path) -> None:
    """``task.resume`` hook should carry task id, capability, and session id."""
    _simulate_step_transitions(
        project_root,
        task_id="hookcheck",
        completed_steps=["s1"],
        scratchpad_text="",
    )

    hooks = HookRegistry()
    captured: list[dict[str, object]] = []

    def _record(ctx: object) -> None:
        # LifecycleContext is frozen; pull what we care about.
        from bernstein.core.lifecycle.hooks import LifecycleContext

        assert isinstance(ctx, LifecycleContext)
        captured.append(
            {
                "event": str(ctx.event),
                "task": ctx.task,
                "session_id": ctx.session_id,
                "env": dict(ctx.env),
            }
        )

    hooks.register_callable(LifecycleEvent.TASK_RESUME, _record)
    plan = prepare_resume(project_root, "hookcheck", hooks=hooks)
    assert plan.checkpoint.resume_count == 1

    assert len(captured) == 1
    payload = captured[0]
    assert payload["task"] == "hookcheck"
    assert payload["session_id"] == "adapter-sess-xyz"
    env = payload["env"]
    assert isinstance(env, dict)
    assert env["BERNSTEIN_RESUME_COUNT"] == "1"
    # aider -> fallback-fresh per matrix
    assert env["BERNSTEIN_RESUME_CAPABILITY"] == RESUME_FALLBACK_FRESH


def test_resume_context_block_is_safe_to_prepend(project_root: Path) -> None:
    """``build_resume_context`` output must be a clean prefix string."""
    _simulate_step_transitions(
        project_root,
        task_id="prefix",
        completed_steps=["only-step"],
        scratchpad_text="some pad content",
    )
    cp = load_checkpoint(project_root, "prefix")
    block = build_resume_context(cp)
    # Banner first, blank line, then prompt resumes - verify shape.
    assert block.startswith("## Resume context")
    assert "only-step" in block
    assert "some pad content" in block


# ---------------------------------------------------------------------------
# Deterministic requests and real runtime acknowledgement (#7854)
# ---------------------------------------------------------------------------


def _resume_json(workdir: Path, task_id: str) -> dict[str, object]:
    result = CliRunner().invoke(resume_cmd, [task_id, "--workdir", str(workdir), "--json"])
    assert result.exit_code == EXIT_OK, result.output
    return json.loads(result.stdout)


def test_queued_then_worker_consumed_reports_real_acknowledgement(project_root: Path) -> None:
    _simulate_step_transitions(project_root, task_id="e2e", completed_steps=["step-1"], scratchpad_text="")
    queued = _resume_json(project_root, "e2e")
    request_id = str(queued["request_id"])
    assert queued["dispatch_status"] == ResumeDispatchStatus.QUEUED.value
    assert queued["runtime_acknowledged"] is False

    ran: list[str] = []
    consumed = resume_checkpoint_request(project_root, "e2e", lambda: ran.append("resumed"))
    assert consumed is not None
    assert consumed.request_id == request_id
    assert consumed.dispatch_status == ResumeDispatchStatus.RESUMED.value
    assert ran == ["resumed"]
    assert resume_request_acknowledged(project_root, "e2e", request_id) is True

    repeated = _resume_json(project_root, "e2e")
    assert repeated["dispatch_status"] == ResumeDispatchStatus.RESUMED.value
    assert repeated["runtime_acknowledged"] is True
    assert repeated["resume_count"] == 1
    assert load_checkpoint(project_root, "e2e").resume_count == 1


def test_newer_checkpoint_mints_a_distinct_request_id(project_root: Path) -> None:
    _simulate_step_transitions(project_root, task_id="newer", completed_steps=["step-1"], scratchpad_text="")
    first = _resume_json(project_root, "newer")
    first_id = str(first["request_id"])

    checkpoint = load_checkpoint(project_root, "newer")
    checkpoint.last_completed_step_id = "step-2"
    checkpoint.trace_cursor += 256
    save_checkpoint(project_root, checkpoint)
    second = _resume_json(project_root, "newer")

    assert second["request_id"] != first_id
    assert load_checkpoint(project_root, "newer").resume_count == 2
    records = read_resume_requests(project_root, "newer")
    assert {record.request_id for record in records} == {first_id, second["request_id"]}
    first_record = load_resume_request(project_root, "newer", first_id)
    assert first_record is not None
    assert first_record.dispatch_status == ResumeDispatchStatus.QUEUED.value


def test_supervisor_restart_recovers_a_claimed_request(project_root: Path) -> None:
    run_id = "recovery-run"
    ledger = WorkLedger.open(run_ledger_dir(project_root / ".sdd", run_id))
    ledger.append(kind=KIND_RUN_OPEN, payload={"run_id": run_id})
    ledger.append(kind=KIND_TASK_SCHEDULED, task_id="crash-task")
    ledger.close()

    queued = json.loads(
        CliRunner().invoke(ledger_group, ["resume", run_id, "--workdir", str(project_root), "--json"]).stdout
    )
    assert queued["runtime_acknowledged"] is False
    request_id = str(queued["resume_requests"][0]["request_id"])

    ran: list[str] = []
    advance_run(
        project_root,
        run_id,
        stop_after=1,
        stop_phase="started",
        task_runner=lambda task_id: ran.append(task_id),
    )
    assert ran == []
    interrupted = load_resume_request(project_root, "crash-task", request_id)
    assert interrupted is not None
    assert interrupted.dispatch_status == ResumeDispatchStatus.CLAIMED.value
    assert resume_request_acknowledged(project_root, "crash-task", request_id) is False

    advance_run(project_root, run_id, task_runner=lambda task_id: ran.append(task_id))
    assert ran == ["crash-task"]
    final = load_resume_request(project_root, "crash-task", request_id)
    assert final is not None
    assert final.dispatch_status == ResumeDispatchStatus.RESUMED.value
    assert resume_request_acknowledged(project_root, "crash-task", request_id) is True


def test_supervisor_stop_after_completed_acknowledges_finished_task(project_root: Path) -> None:
    run_id = "partial-complete-run"
    ledger = WorkLedger.open(run_ledger_dir(project_root / ".sdd", run_id))
    ledger.append(kind=KIND_RUN_OPEN, payload={"run_id": run_id})
    ledger.append(kind=KIND_TASK_SCHEDULED, task_id="t1")
    ledger.append(kind=KIND_TASK_SCHEDULED, task_id="t2")
    ledger.close()

    queued = json.loads(
        CliRunner().invoke(ledger_group, ["resume", run_id, "--workdir", str(project_root), "--json"]).stdout
    )
    requests_by_task = {str(item["task_id"]): str(item["request_id"]) for item in queued["resume_requests"]}

    ran: list[str] = []
    state = advance_run(
        project_root,
        run_id,
        stop_after=1,
        stop_phase="completed",
        task_runner=lambda task_id: ran.append(task_id),
    )
    assert ran == ["t1"]
    assert state.completed_tasks == ["t1"]

    t1_record = load_resume_request(project_root, "t1", requests_by_task["t1"])
    assert t1_record is not None
    assert t1_record.dispatch_status == ResumeDispatchStatus.RESUMED.value
    assert resume_request_acknowledged(project_root, "t1", requests_by_task["t1"]) is True

    t2_record = load_resume_request(project_root, "t2", requests_by_task["t2"])
    assert t2_record is not None
    assert t2_record.dispatch_status == ResumeDispatchStatus.QUEUED.value
    assert resume_request_acknowledged(project_root, "t2", requests_by_task["t2"]) is False


def test_supervisor_advance_run_does_not_claim_another_runs_request(project_root: Path) -> None:
    for rid in ("run-a", "run-b"):
        ledger = WorkLedger.open(run_ledger_dir(project_root / ".sdd", rid))
        ledger.append(kind=KIND_RUN_OPEN, payload={"run_id": rid})
        ledger.append(kind=KIND_TASK_SCHEDULED, task_id="shared-task")
        ledger.close()

    queued_a = json.loads(
        CliRunner().invoke(ledger_group, ["resume", "run-a", "--workdir", str(project_root), "--json"]).stdout
    )
    request_a = str(queued_a["resume_requests"][0]["request_id"])

    # Advancing run-b with the same task_id must leave run-a's resume request queued.
    advance_run(project_root, "run-b")
    after_b = load_resume_request(project_root, "shared-task", request_a)
    assert after_b is not None
    assert after_b.dispatch_status == ResumeDispatchStatus.QUEUED.value
    assert resume_request_acknowledged(project_root, "shared-task", request_a) is False

    # Advancing run-a consumes and acknowledges run-a's request.
    advance_run(project_root, "run-a")
    after_a = load_resume_request(project_root, "shared-task", request_a)
    assert after_a is not None
    assert after_a.dispatch_status == ResumeDispatchStatus.RESUMED.value
    assert resume_request_acknowledged(project_root, "shared-task", request_a) is True
