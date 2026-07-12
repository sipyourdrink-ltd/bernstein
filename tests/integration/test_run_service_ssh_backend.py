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
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.run_service_cmd import EXIT_NO_RUN, run_service_group
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
from bernstein.core.sandbox.ssh_backend import SSHSandboxBackend
from bernstein.core.security.audit_chain import EVENT_RUN_SSH_TASK, AuditChainStore
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
