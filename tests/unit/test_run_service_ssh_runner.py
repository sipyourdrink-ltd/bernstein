"""Unit tests for the detached-run ssh task runner (issue #2352, AC4).

The runner executes each task of a detached run on the ssh sandbox backend in
its own isolated remote worktree, injecting credentials resolved from the
credential vault (never the ledger or the receipts), and records a signed
``run.ssh_task`` receipt binding the worktree. These tests pin: vault-only
secret resolution, the no-leak property (a resolved secret never reaches the
ledger or the audit chain on disk), the isolation-marker digest in the receipt,
and the non-secret spec sidecar round-trip.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bernstein.core.run_service.service import RunService
from bernstein.core.run_service.ssh_runner import (
    SSHBackendSpec,
    SSHTaskRunner,
    build_ssh_backend,
    read_ssh_spec,
    write_ssh_spec,
)
from bernstein.core.sandbox.ssh_backend import SSHSandboxBackend
from bernstein.core.security.audit_chain import EVENT_RUN_SSH_TASK, AuditChainStore
from tests.support.ssh_fake import InProcessSSHTransport
from tests.support.vault_fake import FakeVault, make_git_repo

_SECRET = "sk-supersecret-do-not-leak-42"


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    root = tmp_path / "proj"
    root.mkdir()
    return root


@pytest.fixture
def remote_repo(tmp_path: Path) -> Path:
    return make_git_repo(tmp_path / "remote_repo")


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


# ---------------------------------------------------------------------------
# Spec sidecar: non-secret only
# ---------------------------------------------------------------------------


def test_spec_public_dict_is_secret_free_and_round_trips(remote_repo: Path, tmp_path: Path) -> None:
    spec = _spec(remote_repo, tmp_path / "root", secret=True)
    public = spec.to_public_dict()
    # Only env-var names and provider ids -- never a secret value.
    assert "github" in str(public["vault_env"])
    assert _SECRET not in str(public)
    assert SSHBackendSpec.from_public_dict(public) == spec


def test_from_public_dict_accepts_legacy_secret_env_key(remote_repo: Path, tmp_path: Path) -> None:
    # A sidecar written before the field rename used the ``secret_env`` key for
    # the same non-secret (env-name, provider-id) pairs; a resume must still load it.
    spec = _spec(remote_repo, tmp_path / "root", secret=True)
    legacy = spec.to_public_dict()
    legacy["secret_env"] = legacy.pop("vault_env")
    assert SSHBackendSpec.from_public_dict(legacy) == spec


def test_spec_sidecar_round_trips_on_disk(project: Path, remote_repo: Path, tmp_path: Path) -> None:
    spec = _spec(remote_repo, tmp_path / "root", secret=True)
    RunService(project).submit("goal", ["t0"], run_id="run-x")
    write_ssh_spec(project, "run-x", spec)
    loaded = read_ssh_spec(project, "run-x")
    assert loaded == spec
    # Nothing on disk carries the secret (only the provider id).
    blob = (project / ".sdd").glob("**/*")
    for path in blob:
        if path.is_file():
            assert _SECRET.encode() not in path.read_bytes()


def test_read_ssh_spec_absent_returns_none(project: Path) -> None:
    RunService(project).submit("goal", ["t0"], run_id="run-x")
    assert read_ssh_spec(project, "run-x") is None


# ---------------------------------------------------------------------------
# Vault-only secret resolution
# ---------------------------------------------------------------------------


def test_resolve_secret_env_reads_vault_only(project: Path, remote_repo: Path, tmp_path: Path) -> None:
    spec = _spec(remote_repo, tmp_path / "root", secret=True)
    runner = SSHTaskRunner(
        spec, project, "run-x", backend=_fake_backend(tmp_path / "root"), vault=FakeVault({"github": _SECRET})
    )
    env = runner.resolve_secret_env()
    assert env == {"GITHUB_TOKEN": _SECRET}


def test_resolve_secret_env_missing_provider_raises(project: Path, remote_repo: Path, tmp_path: Path) -> None:
    spec = _spec(remote_repo, tmp_path / "root", secret=True)
    runner = SSHTaskRunner(spec, project, "run-x", backend=_fake_backend(tmp_path / "root"), vault=FakeVault({}))
    with pytest.raises(Exception, match="github"):
        runner.resolve_secret_env()


def test_resolve_secret_env_never_falls_back_to_environment(
    project: Path, remote_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Even with the legacy env-var set, an empty vault must not resolve it.
    monkeypatch.setenv("GITHUB_TOKEN", "env-fallback-value")
    spec = _spec(remote_repo, tmp_path / "root", secret=True)
    runner = SSHTaskRunner(spec, project, "run-x", backend=_fake_backend(tmp_path / "root"), vault=FakeVault({}))
    with pytest.raises(Exception, match="github"):
        runner.resolve_secret_env()


# ---------------------------------------------------------------------------
# Task execution: receipt binds the isolated worktree
# ---------------------------------------------------------------------------


def test_run_task_records_ssh_receipt_binding_worktree(project: Path, remote_repo: Path, tmp_path: Path) -> None:
    spec = _spec(remote_repo, tmp_path / "root")
    RunService(project).submit("goal", ["t0"], run_id="run-x")
    runner = SSHTaskRunner(spec, project, "run-x", backend=_fake_backend(tmp_path / "root"))
    receipt = runner.run_task("t0")

    assert receipt.exit_code == 0
    assert receipt.worktree.endswith("run-x--t0") or "t0" in receipt.worktree
    assert receipt.worktree_digest.startswith("sha256:")

    chain = AuditChainStore(project / ".sdd" / "audit")
    events = [e for e in chain.query(event_type=EVENT_RUN_SSH_TASK) if e.details.get("task_id") == "t0"]
    assert len(events) == 1
    details = events[0].details
    assert details["run_id"] == "run-x"
    assert details["backend"] == "ssh"
    assert details["host"] == "builder.invalid"
    assert details["worktree"] == receipt.worktree
    assert details["worktree_digest"] == receipt.worktree_digest


def test_run_task_worktrees_are_isolated_per_task(project: Path, remote_repo: Path, tmp_path: Path) -> None:
    spec = _spec(remote_repo, tmp_path / "root")
    RunService(project).submit("goal", ["t0", "t1"], run_id="run-x")
    backend = _fake_backend(tmp_path / "root")
    runner = SSHTaskRunner(spec, project, "run-x", backend=backend)
    r0 = runner.run_task("t0")
    r1 = runner.run_task("t1")
    assert r0.worktree != r1.worktree
    # Each task got its own branch (git enforces per-tree isolation); branches
    # survive the per-task worktree cleanup.
    branches = subprocess.run(
        ["git", "-C", str(remote_repo), "branch", "--list", "bernstein/*"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "bernstein/run-x/t0" in branches
    assert "bernstein/run-x/t1" in branches


def test_run_task_is_idempotent_under_reexecution(project: Path, remote_repo: Path, tmp_path: Path) -> None:
    """A task re-run after a mid-execution kill re-provisions without collision."""
    spec = _spec(remote_repo, tmp_path / "root")
    RunService(project).submit("goal", ["t0"], run_id="run-x")
    runner = SSHTaskRunner(spec, project, "run-x", backend=_fake_backend(tmp_path / "root"))
    first = runner.run_task("t0")
    # The worktree/branch from the first attempt persist; a resume re-runs t0.
    second = runner.run_task("t0")
    assert first.exit_code == 0
    assert second.exit_code == 0
    assert second.worktree == first.worktree


def test_secret_never_reaches_ledger_or_audit_chain_on_disk(project: Path, remote_repo: Path, tmp_path: Path) -> None:
    """A resolved credential is injected into the remote env, never persisted."""
    spec = _spec(remote_repo, tmp_path / "root", secret=True)
    RunService(project).submit("goal", ["t0"], run_id="run-x")
    runner = SSHTaskRunner(
        spec,
        project,
        "run-x",
        backend=_fake_backend(tmp_path / "root"),
        vault=FakeVault({"github": _SECRET}),
    )
    runner.run_task("t0")

    # Scan every byte under .sdd (ledger segments + audit chain): no secret.
    for path in (project / ".sdd").glob("**/*"):
        if path.is_file():
            assert _SECRET.encode() not in path.read_bytes(), f"secret leaked into {path}"


def test_build_ssh_backend_defaults_to_subprocess_transport(remote_repo: Path, tmp_path: Path) -> None:
    spec = _spec(remote_repo, tmp_path / "root")
    backend = build_ssh_backend(spec)
    assert isinstance(backend, SSHSandboxBackend)
    assert backend.host == "builder.invalid"
