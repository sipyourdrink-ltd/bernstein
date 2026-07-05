"""Tests for the /complete auto-commit hook (defect 33).

The hook (``_run_auto_commit_pre_complete`` in
``bernstein.core.routes.task_crud``) auto-commits a worker's uncommitted
work BEFORE ``store.complete`` transitions the task to done, so a worker
that forgets to commit still has its work delivered.  Failures are
swallowed (fail-open) - see defect 33 spec.

These tests run foreground-only per the lessons.md rule 5 (background
pytest is a stall trap for delegated agents).
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from bernstein.core.routes.task_crud import (
    _is_auto_commit_denied,
    _is_salvage_branch,
    _run_auto_commit_pre_complete,
)
from bernstein.core.server import create_app
from bernstein.core.tasks.models import Task, TaskStatus

# ---------------------------------------------------------------------------
# Pure-function tests (deny list / salvage detection)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        ".sdd/runtime/pids/spawner.pid",
        ".sdd/metrics/foo.jsonl",
        ".sdd/traces/session.jsonl",
        "attestations/abc.sig",
        "auth/secrets.yaml",
        "bernstein.yaml",
        ".claude/mcp.json",
        ".env",
        ".env.local",
        ".env.production",
        "config/.env",
        # Regression: switching the ".env" glob from substring containment
        # to exact path/basename matching must not fail open for NESTED
        # dotenv variants -- "config/.env.local" has basename ".env.local",
        # which matches neither the exact ".env" glob nor the full-path
        # ".env.*" check, so the ".env.*" branch must consult the basename.
        "config/.env.local",
        "backend/.env.production",
    ],
)
def test_is_auto_commit_denied_true(path: str) -> None:
    assert _is_auto_commit_denied(path), f"expected {path!r} to be denied"


@pytest.mark.parametrize(
    "path",
    [
        "src/bernstein/core/routes/task_crud.py",
        "tests/unit/test_auto_commit_on_complete.py",
        "templates/roles/backend/system_prompt.md",
        "README.md",
        "docs/operations/runbook.md",
        # Regression: plain substring containment on ".env" previously
        # matched any path that merely contains the substring ".env"
        # anywhere, silently excluding unrelated legitimate files from
        # auto-commit.
        ".envrc",
        "config.envelope.json",
        "src/bernstein/core/envelope.py",
    ],
)
def test_is_auto_commit_denied_false(path: str) -> None:
    assert not _is_auto_commit_denied(path), f"expected {path!r} to be allowed"


@pytest.mark.parametrize(
    "branch",
    [
        "salvage/session-abc",
        "salvage/anything",
        "bernstein-salvage-2026",
    ],
)
def test_is_salvage_branch_true(branch: str) -> None:
    assert _is_salvage_branch(branch)


@pytest.mark.parametrize(
    "branch",
    [
        "agent/A-1234",
        "main",
        "fix/auto-commit-on-complete",
        None,
        "",
    ],
)
def test_is_salvage_branch_false(branch: str | None) -> None:
    assert not _is_salvage_branch(branch)


# ---------------------------------------------------------------------------
# Helper: build a fake Request with workdir set
# ---------------------------------------------------------------------------


class _FakeAppState:
    """Minimal stand-in for ``starlette.applications.Starlette.state``."""

    def __init__(self, workdir: Path) -> None:
        self.workdir = workdir


class _FakeApp:
    """Minimal stand-in for ``starlette.requests.Request.app``.

    The hook reads ``request.app.state.workdir`` - so ``app`` exposes
    ``state`` whose attribute is the workdir Path.
    """

    def __init__(self, workdir: Path) -> None:
        self.state = _FakeAppState(workdir)


class _FakeRequest:
    """Minimal stand-in for the bits of Request that the hook reads."""

    def __init__(self, workdir: Path) -> None:
        self.app = _FakeApp(workdir)


def _make_task(task_id: str, session_id: str | None) -> Task:
    return Task(
        id=task_id,
        title="Test task",
        description="",
        role="backend",
        status=TaskStatus.CLAIMED,
        claimed_by_session=session_id,
    )


def _init_git_repo(repo: Path, initial_commit_message: str = "init") -> str:
    """Initialise a git repo at *repo* and return HEAD sha."""
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", initial_commit_message], cwd=repo, check=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return sha


def _setup_workdir_with_worktree(workdir: Path, session_id: str) -> Path:
    """Build a workdir/<.sdd/worktrees/<session_id>/> git worktree.

    Returns the worktree path.  The worktree is on its own
    ``agent/<session_id>`` branch and is a fully working git checkout
    (separate HEAD but tracked by the same .git).
    """
    workdir.mkdir(parents=True, exist_ok=True)
    _init_git_repo(workdir)
    wt = workdir / ".sdd" / "worktrees" / session_id
    wt.parent.mkdir(parents=True, exist_ok=True)
    branch_name = f"agent/{session_id}"
    subprocess.run(
        [
            "git",
            "worktree",
            "add",
            "-b",
            branch_name,
            str(wt),
        ],
        cwd=workdir,
        check=True,
    )
    # Configure user.email/user.name inside the worktree too.
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=wt, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=wt, check=True)
    return wt


# ---------------------------------------------------------------------------
# (a) worker has uncommitted deliverable → auto-commit happens
# ---------------------------------------------------------------------------


def test_auto_commit_creates_commit_for_uncommitted_changes(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="bernstein.core.routes.task_crud")
    session_id = "A-1"
    task = _make_task("T-100", session_id)
    wt = _setup_workdir_with_worktree(tmp_path, session_id)

    # Worker leaves a staged-and-unstaged mixed state: a new file plus a
    # modification to README.md.
    (wt / "src" / "new_module.py").parent.mkdir(parents=True, exist_ok=True)
    (wt / "src" / "new_module.py").write_text("def hello():\n    return 'hi'\n", encoding="utf-8")
    (wt / "README.md").write_text("updated contents\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=wt, check=True)

    request = _FakeRequest(tmp_path)
    _run_auto_commit_pre_complete(request, task)  # type: ignore[arg-type]

    # /complete still succeeds (we never raised).  Verify the commit landed.
    log_out = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=wt,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "auto: T-100 pre-/complete" in log_out, f"expected auto-commit in log:\n{log_out}"
    # Status should now be clean.
    status_proc = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=wt,
        check=True,
        capture_output=True,
        text=True,
    )
    assert status_proc.stdout.strip() == "", f"expected clean status, got:\n{status_proc.stdout}"

    reason_logs = [
        r.message
        for r in caplog.records
        if "auto_commit_pre_complete" in r.message and "reason=uncommitted_changes_at_complete" in r.message
    ]
    assert reason_logs, f"expected uncommitted_changes log; got: {[r.message for r in caplog.records]}"


# ---------------------------------------------------------------------------
# (b) worker has already committed → no-op
# ---------------------------------------------------------------------------


def test_auto_commit_skips_when_already_committed(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="bernstein.core.routes.task_crud")
    session_id = "A-2"
    task = _make_task("T-200", session_id)
    wt = _setup_workdir_with_worktree(tmp_path, session_id)

    # Worker already committed with the task id in the message - this is
    # the success path from 88611aab's prompt contract.
    (wt / "delivered.txt").write_text("worker deliverable\n", encoding="utf-8")
    subprocess.run(["git", "add", "delivered.txt"], cwd=wt, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "T-200: shipped feature"],
        cwd=wt,
        check=True,
    )

    log_count_before = len(caplog.records)

    request = _FakeRequest(tmp_path)
    _run_auto_commit_pre_complete(request, task)  # type: ignore[arg-type]

    # No new commit was created (we expect init + the worker's commit).
    log_out = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=wt,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert log_out.count("\n") == 2, f"expected exactly 2 commits (init + worker), got:\n{log_out}"
    assert "auto: T-200 pre-/complete" not in log_out

    # Log records reflect the already-committed reason.
    new_records = caplog.records[log_count_before:]
    reasons = [r.message for r in new_records if "auto_commit_pre_complete" in r.message]
    assert any("reason=already_committed" in m for m in reasons), reasons


# ---------------------------------------------------------------------------
# (c) salvage branch → skipped
# ---------------------------------------------------------------------------


def test_auto_commit_skips_salvage_branch(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="bernstein.core.routes.task_crud")
    session_id = "A-3"
    task = _make_task("T-300", session_id)
    wt = _setup_workdir_with_worktree(tmp_path, session_id)

    # Switch the worktree onto a salvage branch with dirty state.
    subprocess.run(["git", "checkout", "-q", "-b", "salvage/legacy"], cwd=wt, check=True)
    (wt / "salvaged.txt").write_text("rescued state\n", encoding="utf-8")

    request = _FakeRequest(tmp_path)
    _run_auto_commit_pre_complete(request, task)  # type: ignore[arg-type]

    # No commit was created on the salvage branch.
    log_out = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=wt,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "auto: T-300 pre-/complete" not in log_out

    reasons = [
        r.message
        for r in caplog.records
        if "auto_commit_pre_complete" in r.message and "skipped_salvage_branch" in r.message
    ]
    assert reasons, f"expected salvage skip log; got: {[r.message for r in caplog.records]}"


# ---------------------------------------------------------------------------
# (d) only deny-listed uncommitted files → nothing_to_commit
# ---------------------------------------------------------------------------


def test_auto_commit_skips_when_only_deny_listed_changes_exist(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="bernstein.core.routes.task_crud")
    session_id = "A-4"
    task = _make_task("T-400", session_id)
    wt = _setup_workdir_with_worktree(tmp_path, session_id)

    # Worker accidentally left a .env file and a runtime artifact.
    (wt / ".env").write_text("SECRET=should-not-commit\n", encoding="utf-8")
    (wt / ".sdd" / "runtime").mkdir(parents=True, exist_ok=True)
    (wt / ".sdd" / "runtime" / "scratch.log").write_text("noise\n", encoding="utf-8")

    request = _FakeRequest(tmp_path)
    _run_auto_commit_pre_complete(request, task)  # type: ignore[arg-type]

    # No commit was created.
    log_out = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=wt,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "auto: T-400 pre-/complete" not in log_out

    reasons = [
        r.message
        for r in caplog.records
        if "auto_commit_pre_complete" in r.message and "reason=nothing_to_commit" in r.message
    ]
    assert reasons, f"expected nothing_to_commit log; got: {[r.message for r in caplog.records]}"


# ---------------------------------------------------------------------------
# (e) mix of allow + deny → only allowed files committed, deny excluded
# ---------------------------------------------------------------------------


def test_auto_commit_excludes_deny_listed_files_even_with_real_work(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="bernstein.core.routes.task_crud")
    session_id = "A-5"
    task = _make_task("T-500", session_id)
    wt = _setup_workdir_with_worktree(tmp_path, session_id)

    (wt / "real_work.py").write_text("print('real')\n", encoding="utf-8")
    (wt / ".env.local").write_text("SECRET=excluded\n", encoding="utf-8")
    (wt / "bernstein.yaml").write_text("config: leaked\n", encoding="utf-8")

    request = _FakeRequest(tmp_path)
    _run_auto_commit_pre_complete(request, task)  # type: ignore[arg-type]

    # Only the allowed file should have been staged and committed.
    log_out = subprocess.run(
        ["git", "show", "--name-only", "HEAD"],
        cwd=wt,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "real_work.py" in log_out, log_out
    assert ".env.local" not in log_out, log_out
    assert "bernstein.yaml" not in log_out, log_out


# ---------------------------------------------------------------------------
# (f) git error during commit (mocked) → /complete still succeeds
# ---------------------------------------------------------------------------


def test_auto_commit_swallows_git_errors(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    caplog.set_level(logging.INFO, logger="bernstein.core.routes.task_crud")
    session_id = "A-6"
    task = _make_task("T-600", session_id)
    wt = _setup_workdir_with_worktree(tmp_path, session_id)

    # Create a real dirty state so the hook tries to commit.
    (wt / "deliverable.py").write_text("print('hi')\n", encoding="utf-8")

    # Force git commit to fail by patching subprocess.run inside the hook.
    # The hook reads ``subprocess.run`` from the module-level import - we
    # patch ``subprocess.run`` itself so the hook sees the failure too.
    real_run = subprocess.run

    def failing_run(*args: Any, **kwargs: Any) -> Any:
        if (
            args
            and isinstance(args[0], list)
            and args[0][:1] == ["git"]
            and len(args[0]) >= 2
            and args[0][1:2] == ["commit"]
        ):
            return subprocess.CompletedProcess(
                args=args[0],
                returncode=128,
                stdout="",
                stderr="fatal: simulated commit failure",
            )
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", failing_run)

    request = _FakeRequest(tmp_path)
    # Must NOT raise.
    _run_auto_commit_pre_complete(request, task)  # type: ignore[arg-type]

    warn_records = [
        r for r in caplog.records if r.levelno == logging.WARNING and "auto_commit_pre_complete_failed" in r.message
    ]
    assert warn_records, f"expected warn log on simulated git error; got: {[r.message for r in caplog.records]}"


# ---------------------------------------------------------------------------
# (g) no session attached → log no-op, return without raising
# ---------------------------------------------------------------------------


def test_auto_commit_skips_when_no_session(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="bernstein.core.routes.task_crud")
    task = _make_task("T-700", None)

    request = _FakeRequest(tmp_path)
    _run_auto_commit_pre_complete(request, task)  # type: ignore[arg-type]

    log_lines = [r.message for r in caplog.records if "auto_commit_pre_complete" in r.message]
    assert any("reason=no_session" in m for m in log_lines), log_lines


# ---------------------------------------------------------------------------
# (h) /complete end-to-end via TestClient - works on a tiny task
#     (regression: existing /complete path stays green).
# ---------------------------------------------------------------------------


def test_complete_endpoint_still_responds_200(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="bernstein.core.routes.task_crud")
    app = create_app(jsonl_path=tmp_path / "tasks.jsonl")

    with TestClient(app) as client:
        create_resp = client.post(
            "/tasks",
            json={
                "title": "Trivial task",
                "description": "Smoke.",
                "role": "backend",
                "priority": 1,
                "scope": "small",
                "complexity": "low",
                "estimated_minutes": 5,
            },
        )
        assert create_resp.status_code == 201, create_resp.text
        task_id = create_resp.json()["id"]
        # Claim and complete - no worker session, so the hook should
        # no-op cleanly with reason=no_session.
        assert client.post(f"/tasks/{task_id}/claim").status_code == 200
        resp = client.post(
            f"/tasks/{task_id}/complete",
            json={"result_summary": "Done."},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "done"

    # The no_session log line must have been emitted.
    lines = [r.message for r in caplog.records if "auto_commit_pre_complete" in r.message]
    assert any("reason=no_session" in m for m in lines), lines
