from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from bernstein.core.evidence.run_artifacts import (
    ArtifactPayload,
    post_run_artifact,
)
from bernstein.core.git.remote_diff_ingest import RemoteDiffApplyError, ingest_remote_diff, latest_verified_diff


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / "README.md").write_text("initial content\n")
    subprocess.run(["git", "init", "-q"], cwd=repo_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo_path, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-q", "--allow-empty", "-m", "init"],
        cwd=repo_path,
        check=True,
    )
    return repo_path


@pytest.fixture
def sdd(repo: Path) -> Path:
    sdd_path = repo / ".sdd"
    sdd_path.mkdir()
    return sdd_path


_KEY = b"k" * 32

DIFF = "--- /dev/null\n+++ b/planned/x/test_x.py\n@@ -0,0 +1,2 @@\n+def test_ok():\n+    assert True\n"


def _post_diff_artifact(sdd_dir: Path, task_id: str, diff_content: str = DIFF, version: int = 1) -> None:
    # Wrap the diff_content in a dict under 'body' for ArtifactPayload.report
    payload = ArtifactPayload.report(body=diff_content)
    # Post the artifact for the specified version.
    # We call post_run_artifact once, and it increments the version internally
    # so if we want to post version N, we call it N times.
    for _ in range(version):
        post_run_artifact(sdd_dir=sdd_dir, task_id=task_id, key="diff", payload=payload, actor="gha-x", hmac_key=_KEY)


def test_ingest_creates_branch(repo: Path, sdd: Path) -> None:
    _post_diff_artifact(sdd, "task-abc123", version=1)

    initial_head = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, check=True, text=True, capture_output=True
    ).stdout.strip()

    branch = ingest_remote_diff(repo, sdd, "task-abc123", hmac_key=_KEY)
    assert branch == "agent/remote-task-abc123-v1"

    # Check content of the new file
    file_content = subprocess.run(
        ["git", "show", f"{branch}:planned/x/test_x.py"], cwd=repo, check=True, text=True, capture_output=True
    ).stdout
    assert "def test_ok():" in file_content
    assert "    assert True" in file_content

    # Check HEAD is unchanged
    current_head = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, check=True, text=True, capture_output=True
    ).stdout.strip()
    assert current_head == initial_head

    # No leftover worktree entries
    assert not (repo / ".sdd" / "runtime" / "remote-ingest").exists()


def test_no_artifact_returns_none(repo: Path, sdd: Path) -> None:
    branch = ingest_remote_diff(repo, sdd, "task-no-artifact", hmac_key=_KEY)
    assert branch is None


def test_tampered_blob_returns_none(repo: Path, sdd: Path) -> None:
    _post_diff_artifact(sdd, "task-abc123", version=1)

    # Tamper with the blob
    evidence_dir = sdd / "evidence"
    tampered = False
    for f in (p for p in evidence_dir.rglob("*") if p.is_file()):
        # Read the content, modify it, and write it back.
        # This assumes the diff artifact payload is JSON and contains a 'body' key.
        content_bytes = f.read_bytes()
        try:
            content_dict = json.loads(content_bytes.decode("utf-8"))
            if "body" in content_dict and "test_x.py" in content_dict["body"]:
                content_dict["body"] = content_dict["body"].replace("def test_ok():", "def test_nok():")
                f.write_bytes(json.dumps(content_dict).encode("utf-8"))
                tampered = True
                break
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue  # Not a JSON blob or not decodable, skip

    assert tampered, "Failed to tamper with the diff artifact blob"

    # Verify that latest_verified_diff now returns None due to tampering
    assert latest_verified_diff(sdd, "task-abc123", hmac_key=_KEY) is None

    branch = ingest_remote_diff(repo, sdd, "task-abc123", hmac_key=_KEY)
    assert branch is None


def test_conflicting_diff_raises(repo: Path, sdd: Path) -> None:
    # Modify README.md in the repo to simulate a conflict
    (repo / "README.md").write_text("changed content\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-q", "-m", "modify readme"],
        cwd=repo,
        check=True,
    )

    # Post a diff that tries to modify README.md with a conflicting context line
    conflicting_diff = (
        "--- a/README.md\n"
        "+++ b/README.md\n"
        "@@ -1,1 +1,1 @@\n"
        "-initial content\n"  # This line no longer exists
        "+changed by diff\n"
    )
    _post_diff_artifact(sdd_dir=sdd, task_id="task-conflict", diff_content=conflicting_diff, version=1)

    with pytest.raises(RemoteDiffApplyError) as exc_info:
        ingest_remote_diff(repo, sdd, "task-conflict", hmac_key=_KEY)

    assert exc_info.value.task_id == "task-conflict"
    # The branch name should be agent/remote-task-conflict-v1 because _post_diff_artifact posts version 1
    assert "could not materialise remote diff on agent/remote-task-conflict-v1" in str(exc_info.value)
    assert "patch failed" in exc_info.value.detail

    # No leftover worktree entries
    assert not (repo / ".sdd" / "runtime" / "remote-ingest").exists()


# --- artifact-mode ingest -------------------------------------------------


def test_ingest_remote_artifact_writes_output_path(repo: Path, sdd: Path) -> None:
    from types import SimpleNamespace

    from bernstein.core.evidence.run_artifacts import ArtifactPayload, post_run_artifact
    from bernstein.core.git.remote_diff_ingest import ingest_remote_artifact
    from bernstein.core.tasks.artifacts import ArtifactKind, ArtifactSpec

    task = SimpleNamespace(
        id="task-report1", artifact_spec=ArtifactSpec(kind=ArtifactKind.REPORT, output_path="out/report.md")
    )
    post_run_artifact(
        sdd_dir=sdd,
        task_id=task.id,
        key="artifact",
        payload=ArtifactPayload.report(body="# Verdict\n\naccept\n"),
        actor="gha-x",
        hmac_key=_KEY,
    )
    branch = ingest_remote_artifact(repo, sdd, task, hmac_key=_KEY)
    assert branch == "agent/remote-task-report1-v1"
    shown = subprocess.run(
        ["git", "show", f"{branch}:out/report.md"], cwd=repo, check=True, text=True, capture_output=True
    ).stdout
    assert shown.startswith("# Verdict")
    assert not (repo / ".sdd" / "runtime" / "remote-ingest").exists()


def test_ingest_remote_artifact_without_post_returns_none(repo: Path, sdd: Path) -> None:
    from types import SimpleNamespace

    from bernstein.core.git.remote_diff_ingest import ingest_remote_artifact
    from bernstein.core.tasks.artifacts import ArtifactKind, ArtifactSpec

    task = SimpleNamespace(
        id="task-report2", artifact_spec=ArtifactSpec(kind=ArtifactKind.REPORT, output_path="out/r.md")
    )
    assert ingest_remote_artifact(repo, sdd, task, hmac_key=_KEY) is None


# --- janitor hook ---------------------------------------------------------


def test_alive_exit_pass_routes_remote_completion_through_preview(monkeypatch, tmp_path: Path) -> None:
    from types import SimpleNamespace

    from bernstein.core.models import CompletionSignal, Task, TaskType

    from bernstein.core.git import remote_diff_ingest
    from bernstein.core.tasks import task_lifecycle
    from bernstein.core.tasks.task_lifecycle import (
        _enqueue_alive_exit_janitor_pass,
        _verify_against_merge_preview,
    )

    submitted: list[tuple] = []
    executor = SimpleNamespace(submit=lambda fn, *a, **k: submitted.append((fn, a)) or SimpleNamespace())
    orch = SimpleNamespace(
        _executor=executor,
        _processed_done_tasks={},
        _config=SimpleNamespace(server_url="http://127.0.0.1:8052"),
        _task_to_session={},
        _workdir=tmp_path,
        _find_session_for_task=lambda _tid: None,
    )
    task = Task(
        id="remote-task-1",
        title="t",
        description="d",
        role="qa",
        task_type=TaskType.STANDARD,
        status=task_lifecycle.TaskStatus.DONE,
    )
    task.completion_signals = [CompletionSignal(type="path_exists", value="planned/x/test_x.py")]
    monkeypatch.setattr(remote_diff_ingest, "ingest_remote_diff", lambda *a, **k: "agent/remote-remote-task-1-v1")
    monkeypatch.setattr("bernstein.core.security.audit.load_or_create_audit_key", lambda: _KEY)

    future = _enqueue_alive_exit_janitor_pass(orch, task, reason="alive_exit_tick")

    assert future is not None
    fn, args = submitted[0]
    assert fn is _verify_against_merge_preview
    # (verify_fn, task, workdir, branch, session_id, preview_setup, ...)
    assert args[3] == "agent/remote-remote-task-1-v1"
    assert args[4] == "remote-remote-task-1"


def test_alive_exit_pass_without_artifact_grades_run_checkout(monkeypatch, tmp_path: Path, caplog) -> None:
    import logging
    from types import SimpleNamespace

    from bernstein.core.models import CompletionSignal, Task, TaskType

    from bernstein.core.git import remote_diff_ingest
    from bernstein.core.tasks import task_lifecycle
    from bernstein.core.tasks.task_lifecycle import _enqueue_alive_exit_janitor_pass, _verify_against_merge_preview

    submitted: list[tuple] = []
    executor = SimpleNamespace(submit=lambda fn, *a, **k: submitted.append((fn, a)) or SimpleNamespace())
    orch = SimpleNamespace(
        _executor=executor,
        _processed_done_tasks={},
        _config=SimpleNamespace(server_url="http://127.0.0.1:8052"),
        _task_to_session={},
        _workdir=tmp_path,
        _find_session_for_task=lambda _tid: None,
    )
    task = Task(
        id="remote-task-2",
        title="t",
        description="d",
        role="qa",
        task_type=TaskType.STANDARD,
        status=task_lifecycle.TaskStatus.DONE,
    )
    task.completion_signals = [CompletionSignal(type="path_exists", value="x")]
    monkeypatch.setattr(remote_diff_ingest, "ingest_remote_diff", lambda *a, **k: None)
    monkeypatch.setattr("bernstein.core.security.audit.load_or_create_audit_key", lambda: _KEY)

    with caplog.at_level(logging.INFO, logger="bernstein.core.tasks.task_lifecycle"):
        _enqueue_alive_exit_janitor_pass(orch, task, reason="alive_exit_tick")

    fn, args = submitted[0]
    assert fn is not _verify_against_merge_preview
    assert args[1] == tmp_path
    assert "remote completion without a posted artifact" in caplog.text
