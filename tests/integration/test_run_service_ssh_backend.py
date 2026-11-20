"""AC4 end-to-end: a full goal runs on the ssh backend with worktree isolation.

Issue #2352's fourth acceptance criterion is that a full goal executes end to
end on the ssh backend with worktree isolation preserved. These tests drive the
*real* ssh sandbox backend and the *real* detached-run advance loop; only the
external ssh hop is a faithful in-process double, so the base64 file transfer,
the ``cd``/``env`` command composition, and the remote ``git worktree``
lifecycle all run for real against genuine local git worktrees.

They prove, per the AC and the deferred-scope note:

* a goal's every task executes off-host and completes (end to end);
* each task runs in its own isolated worktree on its own branch (isolation);
* a supervisor killed mid-run resumes on the ssh backend from the ledger tip
  with zero lost completed tasks (zero lost across the ssh boundary);
* injected credentials flow through the vault only and never reach the ledger
  or the receipts; and
* the audit chain, work ledger, and continuity boundaries verify offline.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import subprocess
from contextlib import contextmanager
from multiprocessing.connection import Connection
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.run_service_cmd import EXIT_NO_RUN, run_service_group
from bernstein.core.persistence.work_ledger import LedgerReader, WorkLedger, run_ledger_dir
from bernstein.core.run_service import (
    RunService,
    SSHBackendSpec,
    SSHTaskRunner,
    advance_run,
    run_goal_on_ssh,
    serve_run,
    verify_run,
    write_ssh_spec,
)
from bernstein.core.run_service.ssh_runner import SSHRunnerError
from bernstein.core.sandbox.ssh_backend import SSHSandboxBackend
from bernstein.core.security.audit_chain import EVENT_RUN_CLOSURE, EVENT_RUN_SSH_TASK, AuditChainStore
from bernstein.core.security.run_closure import RunClosureError, RunClosureOutcome, close_run, project_run_closure
from tests.support.ssh_fake import InProcessSSHTransport
from tests.support.vault_fake import FakeVault, make_git_repo

_SECRET = "sk-supersecret-e2e-must-not-leak"

# A task command that only passes on a real, freshly-checked-out, isolated
# worktree: the base checkout must be present (``seed.txt``) and no sibling
# task's crosstalk file may have leaked in.
_ISOLATION_PROBE = ["sh", "-c", "test -f seed.txt && test ! -e crosstalk.txt && echo mine > crosstalk.txt"]


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    root = tmp_path / "proj"
    root.mkdir()
    return root


@pytest.fixture
def remote_repo(tmp_path: Path) -> Path:
    return make_git_repo(tmp_path / "remote_repo")


@pytest.fixture
def remote_root(tmp_path: Path) -> Path:
    root = tmp_path / "remote_root"
    root.mkdir()
    return root


def _spec(remote_repo: Path, remote_root: Path, *, secret: bool = False) -> SSHBackendSpec:
    return SSHBackendSpec(
        host="builder.invalid",
        remote_root=str(remote_root),
        repo_src=str(remote_repo),
        base_branch="main",
        vault_env=(("GITHUB_TOKEN", "github"),) if secret else (),
    )


def _fake_backend(remote_root: Path) -> SSHSandboxBackend:
    return SSHSandboxBackend(host="builder.invalid", path=str(remote_root), transport=InProcessSSHTransport())


def _ssh_events(project: Path, run_id: str) -> list:
    chain = AuditChainStore(project / ".sdd" / "audit")
    return [e for e in chain.query(event_type=EVENT_RUN_SSH_TASK) if e.details.get("run_id") == run_id]


def _branches(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", "bernstein/*"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def test_full_goal_executes_end_to_end_on_ssh_backend_with_worktree_isolation(
    project: Path, remote_repo: Path, remote_root: Path
) -> None:
    """AC4: a full goal runs to completion off-host, one isolated worktree per task."""
    tasks = [f"t{i}" for i in range(4)]
    spec = _spec(remote_repo, remote_root, secret=True)
    state = run_goal_on_ssh(
        project,
        "multi-task goal",
        tasks,
        spec,
        run_id="run-e2e",
        backend=_fake_backend(remote_root),
        vault=FakeVault({"github": _SECRET}),
        command_for=lambda _tid: _ISOLATION_PROBE,
    )

    # End to end: every task of the goal completed and the run closed.
    assert set(state.completed_tasks) == set(tasks)
    assert state.in_flight_tasks == []
    assert state.run_closed

    events = _ssh_events(project, "run-e2e")
    assert {e.details["task_id"] for e in events} == set(tasks)
    # Isolation: a distinct remote worktree per task, and every probe passed
    # (fresh checkout, no crosstalk from a sibling task).
    assert len({e.details["worktree"] for e in events}) == len(tasks)
    assert all(e.details["exit_code"] == 0 for e in events)
    # Isolation: a distinct per-task branch (git-enforced), surviving cleanup.
    branches = _branches(remote_repo)
    for task in tasks:
        assert f"bernstein/run-e2e/{task}" in branches

    # Secret flowed through the vault only; it never touched disk.
    for path in (project / ".sdd").glob("**/*"):
        if path.is_file():
            assert _SECRET.encode() not in path.read_bytes(), f"secret leaked into {path}"

    # The audit chain, ledger, and continuity boundaries verify offline.
    assert verify_run(project, "run-e2e").ok


def test_ssh_backend_kill_mid_run_then_restart_loses_zero_completed(
    project: Path, remote_repo: Path, remote_root: Path
) -> None:
    """AC4 + deferred note: resume on the ssh backend across a mid-run kill, zero lost."""
    tasks = [f"t{i}" for i in range(5)]
    spec = _spec(remote_repo, remote_root)
    svc = RunService(project)
    handle = svc.submit("goal", tasks, run_id="run-chaos")
    run_id = handle.run_id

    # First supervisor generation runs two tasks on ssh, then is "killed".
    gen1 = SSHTaskRunner(spec, project, run_id, backend=_fake_backend(remote_root))
    advance_run(project, run_id, task_runner=gen1, stop_after=2)
    completed_before = set(RunService(project).project(run_id).completed_tasks)
    assert len(completed_before) == 2
    assert {e.details["task_id"] for e in _ssh_events(project, run_id)} == completed_before

    # A fresh generation (new backend, new control master) resumes from the tip.
    svc.daemon_restart(run_id)
    gen2 = SSHTaskRunner(spec, project, run_id, backend=_fake_backend(remote_root))
    advance_run(project, run_id, task_runner=gen2)
    svc.complete(run_id)

    final = RunService(project).project(run_id)
    assert set(final.completed_tasks) == set(tasks)
    # Zero lost: everything completed before the kill is still completed.
    assert completed_before <= set(final.completed_tasks)

    # Exactly one ssh receipt per task -- nothing re-executed, nothing lost.
    events = _ssh_events(project, run_id)
    assert sorted(e.details["task_id"] for e in events) == sorted(tasks)
    assert len({e.details["worktree"] for e in events}) == len(tasks)
    assert verify_run(project, run_id).ok


@pytest.mark.parametrize("exit_code", [0, 23])
def test_remote_exit_controls_task_outcome(project: Path, remote_repo: Path, remote_root: Path, exit_code: int) -> None:
    svc = RunService(project)
    run_id = svc.submit("exit status", ["t0"], run_id="run-exit").run_id
    runner = SSHTaskRunner(
        _spec(remote_repo, remote_root),
        project,
        run_id,
        backend=_fake_backend(remote_root),
        command_for=lambda _: ["sh", "-c", f"exit {exit_code}"],
    )
    if exit_code:
        with pytest.raises(SSHRunnerError, match="23"):
            advance_run(project, run_id, task_runner=runner)
    else:
        advance_run(project, run_id, task_runner=runner)

    state = svc.project(run_id)
    assert state.completed_tasks == ([] if exit_code else ["t0"])
    assert state.failed_tasks == (["t0"] if exit_code else [])
    assert state.in_flight_tasks == []
    entries = list(LedgerReader(run_ledger_dir(project / ".sdd", run_id)).entries())
    terminal = [e for e in entries if e.kind in {"task.completed", "task.failed"}]
    assert len(terminal) == 1
    assert terminal[0].kind == ("task.failed" if exit_code else "task.completed")
    if exit_code:
        assert terminal[0].payload["exit_code"] == exit_code
    events = _ssh_events(project, run_id)
    assert len(events) == 1
    assert events[0].details["exit_code"] == exit_code
    assert verify_run(project, run_id).ok


def test_serve_run_closes_failed_remote_task_without_running_next_task(
    project: Path, remote_repo: Path, remote_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _fake_backend(remote_root)
    monkeypatch.setattr("bernstein.core.run_service.ssh_runner.build_ssh_backend", lambda spec: backend)
    monkeypatch.setattr(SSHTaskRunner, "_default_command", lambda self, task_id: ["sh", "-c", "exit 23"])
    svc = RunService(project)
    run_id = svc.submit("exit status", ["t0", "t1"], run_id="run-failed").run_id
    write_ssh_spec(project, run_id, _spec(remote_repo, remote_root))

    with pytest.raises(SSHRunnerError, match="23"):
        serve_run(project, run_id)

    state = svc.project(run_id)
    assert state.run_closed
    assert state.failed_tasks == ["t0"]
    assert state.completed_tasks == []
    assert state.scheduled_tasks == ["t1"]
    assert state.in_flight_tasks == []
    assert [event.details["task_id"] for event in _ssh_events(project, run_id)] == ["t0"]
    entries = list(LedgerReader(run_ledger_dir(project / ".sdd", run_id)).entries())
    assert not any(entry.kind == "task.completed" for entry in entries)
    assert verify_run(project, run_id).ok


def test_restart_preserves_remote_failure_after_failure_append(
    project: Path, remote_repo: Path, remote_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bernstein.core.run_service import supervisor

    class SimulatedCrash(BaseException):
        pass

    backend = _fake_backend(remote_root)
    monkeypatch.setattr("bernstein.core.run_service.ssh_runner.build_ssh_backend", lambda spec: backend)
    monkeypatch.setattr(SSHTaskRunner, "_default_command", lambda self, task_id: ["sh", "-c", "exit 23"])
    svc = RunService(project)
    run_id = svc.submit("exit status", ["t0", "t1"], run_id="run-failure-crash").run_id
    write_ssh_spec(project, run_id, _spec(remote_repo, remote_root))
    original_heartbeat = supervisor._write_heartbeat

    def crash_after_failure(*args, **kwargs):
        if kwargs["phase"] == "failed":
            raise SimulatedCrash
        original_heartbeat(*args, **kwargs)

    monkeypatch.setattr(supervisor, "_write_heartbeat", crash_after_failure)
    with pytest.raises(SimulatedCrash):
        serve_run(project, run_id)
    assert not svc.project(run_id).run_closed

    monkeypatch.setattr(supervisor, "_write_heartbeat", original_heartbeat)
    assert advance_run(project, run_id).scheduled_tasks == ["t1"]
    state = serve_run(project, run_id)
    assert state.run_closed
    assert state.failed_tasks == ["t0"]
    assert state.completed_tasks == []
    assert state.scheduled_tasks == ["t1"]
    assert [event.details["task_id"] for event in _ssh_events(project, run_id)] == ["t0"]
    closure = project_run_closure(AuditChainStore(project / ".sdd" / "audit"), run_id)
    assert closure.outcome is RunClosureOutcome.FAILED
    assert verify_run(project, run_id).ok


@pytest.mark.parametrize("prior_outcome", [None, "cancelled"])
def test_serve_finishes_the_frontier_seen_by_advance(
    project: Path, remote_repo: Path, remote_root: Path, monkeypatch: pytest.MonkeyPatch, prior_outcome: str | None
) -> None:
    from bernstein.core.run_service import supervisor

    svc = RunService(project)
    run_id = svc.submit("overlapping supervisors", ["a", "b"]).run_id
    runner = SSHTaskRunner(
        _spec(remote_repo, remote_root),
        project,
        run_id,
        backend=_fake_backend(remote_root),
        command_for=lambda _: ["sh", "-c", "exit 37"],
    )

    def other_generation_then_advance(*args, **kwargs):
        # Another generation owns the first task between serve_run's two reads.
        with pytest.raises(SSHRunnerError, match="37"):
            advance_run(project, run_id, task_runner=runner)
        if prior_outcome:
            svc.close(run_id, prior_outcome)
        return advance_run(*args, **kwargs)

    monkeypatch.setattr(supervisor, "advance_run", other_generation_then_advance)
    state = serve_run(project, run_id)
    assert state.run_closed and state.failed_tasks == ["a"] and state.scheduled_tasks == ["b"]
    assert [(e.details["task_id"], e.details["exit_code"]) for e in _ssh_events(project, run_id)] == [("a", 37)]
    chain = AuditChainStore(project / ".sdd" / "audit")
    assert project_run_closure(chain, run_id).outcome == (prior_outcome or "failed")


def test_reopen_restores_one_failed_result(
    project: Path, remote_repo: Path, remote_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bernstein.core.run_service import service

    svc = RunService(project)
    run_id = svc.submit("interrupted failure", ["a", "b"]).run_id
    runner = SSHTaskRunner(
        _spec(remote_repo, remote_root),
        project,
        run_id,
        backend=_fake_backend(remote_root),
        command_for=lambda _: ["sh", "-c", "exit 37"],
    )
    with pytest.raises(SSHRunnerError, match="37"):
        advance_run(project, run_id, task_runner=runner)

    def interrupt(**kwargs):
        raise InterruptedError("before terminal receipt")

    chain = AuditChainStore(project / ".sdd" / "audit")
    with monkeypatch.context() as patch:
        patch.setattr(service, "close_run", interrupt)
        with pytest.raises(InterruptedError):
            svc.close(run_id, "failed")
    state = svc.project(run_id)
    assert state.run_closed and state.failed_tasks == ["a"] and state.scheduled_tasks == ["b"]
    assert project_run_closure(chain, run_id).outcome is None

    assert serve_run(project, run_id) == state
    assert project_run_closure(chain, run_id).outcome is RunClosureOutcome.FAILED
    events = chain.query(include_archived=True)
    assert serve_run(project, run_id) == state
    assert chain.query(include_archived=True) == events
    assert [(e.details["task_id"], e.details["exit_code"]) for e in _ssh_events(project, run_id)] == [("a", 37)]
    assert verify_run(project, run_id).ok


def test_public_ssh_goal_closes_before_returning_nonzero_exit(
    project: Path, remote_repo: Path, remote_root: Path
) -> None:
    run_id = "public-ssh-failure"
    with pytest.raises(SSHRunnerError) as error:
        run_goal_on_ssh(
            project,
            "nonzero SSH exit",
            ["a", "b"],
            _spec(remote_repo, remote_root),
            run_id=run_id,
            backend=_fake_backend(remote_root),
            command_for=lambda _: ["sh", "-c", "exit 37"],
        )
    assert error.value.exit_code == 37
    state = RunService(project).project(run_id)
    assert state.run_closed and state.failed_tasks == ["a"] and state.scheduled_tasks == ["b"]
    assert [(e.details["task_id"], e.details["exit_code"]) for e in _ssh_events(project, run_id)] == [("a", 37)]
    chain = AuditChainStore(project / ".sdd" / "audit")
    assert project_run_closure(chain, run_id).outcome is RunClosureOutcome.FAILED


def _pause_failed_supervisor(project: Path, run_id: str, ready: Connection, proceed: Connection) -> None:
    open_ledger = WorkLedger.open

    def pause_at_writer(directory: Path) -> WorkLedger:
        # The other supervisor observes the same failed, open run before this append.
        ready.send("before journal append")
        if not proceed.poll(20):
            raise TimeoutError("second supervisor did not reach finalization")
        proceed.recv()
        return open_ledger(directory)

    with patch.object(WorkLedger, "open", side_effect=pause_at_writer):
        serve_run(project, run_id)


def _crash_after_failed_marker(project: Path, run_id: str) -> None:
    with patch("bernstein.core.run_service.service.close_run", side_effect=lambda **_: os._exit(73)):
        RunService(project).close(run_id, "failed")


def test_two_processes_finish_same_failed_frontier(
    project: Path, remote_repo: Path, remote_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = RunService(project)
    run_id = svc.submit("overlap", ["a", "b"]).run_id
    runner = SSHTaskRunner(
        _spec(remote_repo, remote_root),
        project,
        run_id,
        backend=_fake_backend(remote_root),
        command_for=lambda _: ["sh", "-c", "exit 37"],
    )
    with pytest.raises(SSHRunnerError, match="37"):
        advance_run(project, run_id, task_runner=runner)

    ctx = multiprocessing.get_context("spawn")
    with_ready, signal_ready = ctx.Pipe(duplex=False)
    allow_proceed, signal_proceed = ctx.Pipe(duplex=False)
    child = ctx.Process(target=_pause_failed_supervisor, args=(project, run_id, signal_ready, allow_proceed))
    child.start()
    try:
        assert with_ready.poll(20) and with_ready.recv() == "before journal append"
        take_transaction = AuditChainStore.chain_transaction
        announced = False

        @contextmanager
        def join_transaction(chain):
            nonlocal announced
            if not announced:
                announced = True
                signal_proceed.send("both generations are finalizing")
            with take_transaction(chain):
                yield

        with monkeypatch.context() as mp:
            mp.setattr(AuditChainStore, "chain_transaction", join_transaction)
            final = serve_run(project, run_id)
        child.join(20)
        assert child.exitcode == 0
    finally:
        if child.is_alive():
            child.terminate()
            child.join(10)
        for pipe in (with_ready, signal_ready, allow_proceed, signal_proceed):
            pipe.close()
    assert final.failed_tasks == ["a"] and final.scheduled_tasks == ["b"]
    assert serve_run(project, run_id) == final
    entries = LedgerReader(run_ledger_dir(project / ".sdd", run_id)).entries()
    assert [e.payload["outcome"] for e in entries if e.kind == "run.closed"] == ["failed"]
    chain = AuditChainStore(project / ".sdd" / "audit")
    final_events = chain.query(event_type=EVENT_RUN_CLOSURE, resource_id=run_id)
    assert [event.details["outcome"] for event in final_events] == ["failed"]
    assert [(e.details["task_id"], e.details["exit_code"]) for e in _ssh_events(project, run_id)] == [("a", 37)]
    assert verify_run(project, run_id).ok


def test_public_ssh_goal_finishes_durable_outcome_after_process_exit(
    project: Path, remote_repo: Path, remote_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    complete = RunService.complete

    def already_closing(svc: RunService, run_id: str):
        child = multiprocessing.get_context("spawn").Process(target=_crash_after_failed_marker, args=(project, run_id))
        child.start()
        try:
            child.join(20)
            assert child.exitcode == 73
        finally:
            if child.is_alive():
                child.terminate()
                child.join(10)
        return complete(svc, run_id)

    monkeypatch.setattr(RunService, "complete", already_closing)
    run_id = "stale-success"
    state = run_goal_on_ssh(
        project,
        "successful task",
        ["a"],
        _spec(remote_repo, remote_root),
        run_id=run_id,
        backend=_fake_backend(remote_root),
        command_for=lambda _: ["sh", "-c", "exit 0"],
    )
    assert state.run_closed and state.completed_tasks == ["a"]
    chain = AuditChainStore(project / ".sdd" / "audit")
    assert project_run_closure(chain, run_id).outcome is RunClosureOutcome.FAILED
    assert all(event.details["transition"] != "completed" for event in RunService(project).lifecycle_events(run_id))
    assert verify_run(project, run_id).ok


@pytest.mark.parametrize("journal_outcome", ["failed", "completed"])
def test_mismatched_historical_receipt_is_read_only(project: Path, journal_outcome: str) -> None:
    svc = RunService(project)
    run_id = svc.submit("historical mismatch", ["a"]).run_id
    with (
        patch("bernstein.core.run_service.service.close_run", side_effect=InterruptedError),
        pytest.raises(InterruptedError),
    ):
        svc.close(run_id, journal_outcome)
    chain = AuditChainStore(project / ".sdd" / "audit")
    link = LedgerReader(run_ledger_dir(project / ".sdd", run_id)).verify()
    # Model the inconsistent receipt left by an old finalizer after the journal committed.
    close_run(
        chain=chain,
        run_id=run_id,
        outcome=RunClosureOutcome.CANCELLED,
        actor="run_service",
        work_ledger_head=link.head_hash,
        work_ledger_entry_count=link.entries,
    )
    original = chain.query(include_archived=True)
    finalizers = [lambda: svc.close(run_id, journal_outcome), lambda: svc.complete(run_id)]
    if journal_outcome == "failed":
        finalizers.append(lambda: serve_run(project, run_id))
    for finalize in finalizers:
        with pytest.raises(RunClosureError, match="conflicting"):
            finalize()
        assert chain.query(include_archived=True) == original
        assert svc.project(run_id).head_hash == link.head_hash


@pytest.mark.parametrize(
    ("first", "second"), [("failed", "cancelled"), ("completed", "failed"), ("cancelled", "abandoned")]
)
def test_explicit_conflicting_close_preserves_first_journal_outcome(project: Path, first: str, second: str) -> None:
    svc = RunService(project)
    run_id = svc.submit("competing terminal requests", ["a"]).run_id
    with (
        patch("bernstein.core.run_service.service.close_run", side_effect=InterruptedError),
        pytest.raises(InterruptedError),
    ):
        svc.close(run_id, first)
    with pytest.raises(RunClosureError, match="conflicting"):
        svc.close(run_id, second)
    chain = AuditChainStore(project / ".sdd" / "audit")
    assert project_run_closure(chain, run_id).outcome == first
    original = chain.query(include_archived=True)
    assert svc.close(run_id, first).details["outcome"] == first
    with pytest.raises(RunClosureError, match="conflicting"):
        svc.close(run_id, second)
    assert chain.query(include_archived=True) == original


def test_ssh_backend_kill_after_started_before_completed_resumes(
    project: Path, remote_repo: Path, remote_root: Path
) -> None:
    """A task killed after `started` but before its ssh execution runs once on resume."""
    tasks = ["t0", "t1"]
    spec = _spec(remote_repo, remote_root)
    svc = RunService(project)
    svc.submit("goal", tasks, run_id="run-mid")

    gen1 = SSHTaskRunner(spec, project, "run-mid", backend=_fake_backend(remote_root))
    advance_run(project, "run-mid", task_runner=gen1, stop_after=1, stop_phase="started")
    mid = RunService(project).project("run-mid")
    assert mid.in_flight_tasks == ["t0"]
    # The kill landed before t0's ssh command, so no receipt exists yet.
    assert _ssh_events(project, "run-mid") == []

    svc.daemon_restart("run-mid")
    gen2 = SSHTaskRunner(spec, project, "run-mid", backend=_fake_backend(remote_root))
    advance_run(project, "run-mid", task_runner=gen2)
    svc.complete("run-mid")

    final = RunService(project).project("run-mid")
    assert set(final.completed_tasks) == set(tasks)
    events = _ssh_events(project, "run-mid")
    assert sorted(e.details["task_id"] for e in events) == sorted(tasks)
    assert verify_run(project, "run-mid").ok


def test_serve_run_executes_goal_on_ssh_backend_via_sidecar(
    project: Path, remote_repo: Path, remote_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The daemon path (serve_run) runs the goal on ssh when a sidecar is present."""
    backend = _fake_backend(remote_root)
    monkeypatch.setattr(
        "bernstein.core.run_service.ssh_runner.build_ssh_backend",
        lambda spec, transport=None: backend,
    )
    spec = _spec(remote_repo, remote_root)
    svc = RunService(project)
    svc.submit("goal", ["t0", "t1"], run_id="run-serve")
    write_ssh_spec(project, "run-serve", spec)

    state = serve_run(project, "run-serve")

    assert set(state.completed_tasks) == {"t0", "t1"}
    assert state.run_closed
    assert {e.details["task_id"] for e in _ssh_events(project, "run-serve")} == {"t0", "t1"}
    assert verify_run(project, "run-serve").ok


def test_cli_submit_backend_ssh_runs_goal_on_ssh(
    project: Path, remote_repo: Path, remote_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``run-service submit --backend ssh --foreground`` runs the goal off-host."""
    backend = _fake_backend(remote_root)
    monkeypatch.setattr(
        "bernstein.core.run_service.ssh_runner.build_ssh_backend",
        lambda spec, transport=None: backend,
    )
    result = CliRunner().invoke(
        run_service_group,
        [
            "submit",
            "a real goal",
            "--task",
            "t0",
            "--task",
            "t1",
            "--workdir",
            str(project),
            "--foreground",
            "--backend",
            "ssh",
            "--ssh-host",
            "builder.invalid",
            "--ssh-path",
            str(remote_root),
            "--ssh-repo",
            str(remote_repo),
            "--json",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["backend"] == "ssh"
    run_id = payload["run_id"]
    assert {e.details["task_id"] for e in _ssh_events(project, run_id)} == {"t0", "t1"}
    assert verify_run(project, run_id).ok


def test_cli_submit_backend_ssh_requires_host_and_path(project: Path) -> None:
    result = CliRunner().invoke(
        run_service_group,
        ["submit", "goal", "--task", "t0", "--workdir", str(project), "--foreground", "--backend", "ssh"],
    )
    assert result.exit_code == EXIT_NO_RUN
    assert "requires --ssh-host and --ssh-path" in result.output
