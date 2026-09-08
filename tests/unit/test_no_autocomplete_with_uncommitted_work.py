"""An agent that wrote its deliverable and never committed is not "no changes".

Measured 2026-09-03: `orphan_auto_complete ... empty diff (exit code 0)` fired
on a worktree that still held the files the agent had written, which a
`git add -A` salvage then captured as a patch. Neither read that reaches the
auto-completion sees a file on disk: "empty diff" is scraped from the agent's
log and "no commits" is committed state.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from bernstein.core.models import AgentSession, ModelConfig

from bernstein.core.agents.agent_lifecycle import _uncommitted_work_paths


def _run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "wt"
    repo.mkdir()
    _run(["git", "init", "-b", "main"], repo)
    _run(["git", "config", "user.email", "t@e.com"], repo)
    _run(["git", "config", "user.name", "T"], repo)
    _run(["git", "config", "commit.gpgsign", "false"], repo)
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _run(["git", "add", "-A"], repo)
    _run(["git", "commit", "-m", "seed"], repo)
    return repo


def test_a_clean_worktree_reports_nothing(tmp_path: Path) -> None:
    assert _uncommitted_work_paths(_repo(tmp_path)) == []


def test_untracked_deliverable_is_reported(tmp_path: Path) -> None:
    """The measured shape: files written, never added, never committed."""
    repo = _repo(tmp_path)
    (repo / "adapter.go").write_text("package p\n", encoding="utf-8")
    (repo / "registry.go").write_text("package p\n", encoding="utf-8")
    assert len(_uncommitted_work_paths(repo)) == 2


def test_tracked_edits_are_reported(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "README.md").write_text("edited\n", encoding="utf-8")
    assert _uncommitted_work_paths(repo) != []


@pytest.mark.parametrize("directory", ["auth", "attestations"])
def test_tracked_edits_in_target_repo_runtime_named_directories_are_reported(tmp_path: Path, directory: str) -> None:
    repo = _repo(tmp_path)
    path = repo / directory / "service.py"
    path.parent.mkdir()
    path.write_text("before\n", encoding="utf-8")
    _run(["git", "add", str(path.relative_to(repo))], repo)
    _run(["git", "commit", "-m", f"add {directory} service"], repo)
    path.write_text("after\n", encoding="utf-8")
    assert _uncommitted_work_paths(repo) == [f"{directory}/service.py"]


def test_untracked_file_in_target_repo_auth_directory_is_reported(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    path = repo / "auth" / "service.py"
    path.parent.mkdir()
    path.write_text("new\n", encoding="utf-8")
    assert _uncommitted_work_paths(repo) == ["auth/service.py"]


def test_staged_add_in_target_repo_auth_directory_is_reported(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    path = repo / "auth" / "service.py"
    path.parent.mkdir()
    path.write_text("new\n", encoding="utf-8")
    _run(["git", "add", "auth/service.py"], repo)
    assert _uncommitted_work_paths(repo) == ["auth/service.py"]


def test_gitignored_files_are_not_work(tmp_path: Path) -> None:
    """git status --porcelain already honours .gitignore; build junk is not a deliverable."""
    repo = _repo(tmp_path)
    (repo / ".gitignore").write_text("junk/\n", encoding="utf-8")
    _run(["git", "add", "-A"], repo)
    _run(["git", "commit", "-m", "ignore"], repo)
    (repo / "junk").mkdir()
    (repo / "junk" / "out.o").write_text("x", encoding="utf-8")
    assert _uncommitted_work_paths(repo) == []


def test_a_missing_or_broken_path_never_claims_dirty(tmp_path: Path) -> None:
    """The guard only suppresses auto-completion; a broken git call must not fail a task."""
    assert _uncommitted_work_paths(None) == []
    assert _uncommitted_work_paths(tmp_path / "does-not-exist") == []
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    assert _uncommitted_work_paths(plain) == []


def test_sdd_runtime_files_are_ignored_when_tracked_or_untracked(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    runtime = repo / ".sdd" / "runtime"
    runtime.mkdir(parents=True)
    tracked = runtime / "tracked.log"
    tracked.write_text("before\n", encoding="utf-8")
    _run(["git", "add", ".sdd/runtime/tracked.log"], repo)
    _run(["git", "commit", "-m", "add runtime file"], repo)
    tracked.write_text("after\n", encoding="utf-8")
    (runtime / "untracked.log").write_text("new\n", encoding="utf-8")
    assert _uncommitted_work_paths(repo) == []


@pytest.mark.parametrize("path", ["CLAUDE.md", ".claude/settings.local.json"])
@pytest.mark.parametrize("status", ["untracked", "staged", "tracked"])
def test_exact_orchestrator_files_are_ignored_regardless_of_git_status(tmp_path: Path, path: str, status: str) -> None:
    repo = _repo(tmp_path)
    artifact = repo / path
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("before\n", encoding="utf-8")
    if status == "staged":
        _run(["git", "add", "-f", path], repo)
    elif status == "tracked":
        _run(["git", "add", "-f", path], repo)
        _run(["git", "commit", "-m", f"add {artifact.name}"], repo)
        artifact.write_text("after\n", encoding="utf-8")
    assert _uncommitted_work_paths(repo) == []


def test_rename_with_spaced_paths_reports_destination_once(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    source = repo / "old name.py"
    source.write_text("before\n", encoding="utf-8")
    _run(["git", "add", "old name.py"], repo)
    _run(["git", "commit", "-m", "add spaced path"], repo)
    _run(["git", "mv", "old name.py", "new name.py"], repo)
    assert _uncommitted_work_paths(repo) == ["new name.py"]


def test_untracked_path_containing_rename_arrow_is_not_misparsed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    path = repo / "foo -> auth" / "x.py"
    path.parent.mkdir()
    path.write_text("new\n", encoding="utf-8")
    assert _uncommitted_work_paths(repo) == ["foo -> auth/x.py"]


def test_a_new_directory_counts_every_file_in_it(tmp_path: Path) -> None:
    """``--porcelain`` alone collapses a new directory to one entry."""
    repo = _repo(tmp_path)
    (repo / "pkg").mkdir()
    for i in range(5):
        (repo / "pkg" / f"f{i}.go").write_text("package p\n", encoding="utf-8")
    assert len(_uncommitted_work_paths(repo)) == 5


def test_a_skill_the_agent_was_tasked_to_author_is_work(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / ".claude" / "skills").mkdir(parents=True)
    (repo / ".claude" / "skills" / "SKILL.md").write_text("# s\n", encoding="utf-8")
    assert _uncommitted_work_paths(repo) == [".claude/skills/SKILL.md"]


def test_the_guard_vetoes_the_clean_exit_auto_completion(
    tmp_path: Path,
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    """End to end: a clean exit over a dirty worktree is failed, never completed.

    On main the same input is auto-completed as "no changes needed".
    """
    import bernstein.core.agents.agent_lifecycle as al

    repo = _repo(tmp_path)
    (repo / "adapter.go").write_text("package p\n", encoding="utf-8")

    monkeypatch.setattr(al, "collect_completion_data", lambda workdir, session: {"files_modified": []})
    completed: list[str] = []
    monkeypatch.setattr(al, "complete_task", lambda client, base, task_id, summary: completed.append(task_id))
    failed: list[tuple[str, str]] = []
    monkeypatch.setattr(al, "retry_or_fail_task", lambda task_id, reason, **kw: failed.append((task_id, reason)))

    orch = SimpleNamespace(
        _workdir=tmp_path,
        _client=MagicMock(),
        _spawner=SimpleNamespace(get_worktree_path=lambda sid: repo),
        _config=SimpleNamespace(max_task_retries=3),
        _retried_task_ids=set(),
    )
    session = AgentSession(
        id="A-1",
        role="backend",
        provider="claude",
        model_config=ModelConfig("sonnet", "high"),
        task_ids=["T-1"],
        exit_code=0,
    )
    session.spawn_ts = time.time() - 600.0
    session.tokens_used = 2048

    success, error_type = al._handle_orphan_no_signals(orch, MagicMock(), "T-1", session, "http://srv", time.time())

    assert success is False
    assert error_type == "clean_exit_uncommitted_work"
    assert completed == []
    assert failed and failed[0][0] == "T-1"
    assert "never committed" in failed[0][1]
