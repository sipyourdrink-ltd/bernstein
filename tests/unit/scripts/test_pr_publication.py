"""Exercise the publishing boundary using local data and a recorded push invocation."""

import base64
import io
import json
import subprocess
import zipfile
from pathlib import Path

import pytest
from scripts import prepare_pr_publication as prepare
from scripts import publish_autosync as autosync
from scripts.observability.build_pr_summary import render_publication


def test_report_renders_the_trusted_table_and_keeps_fields_literal() -> None:
    observed = {
        "summary": {"ok": 1},
        "backends": [{"backend": "Code Scanning", "status": "ok", "metrics": [{"name": "item|count", "value": 4}]}],
    }
    report = render_publication(json.dumps(observed).encode(), 42, "topic").decode()
    assert "**bernstein doctor observe** for PR #42 (`topic`): ok=1" in report
    assert "### Code Scanning -- OK" in report
    assert "| item&#124;count | 4 |" in report
    assert "observe JSON could not be parsed" in render_publication(b"", 42, "").decode()
    with pytest.raises(ValueError, match="64 KiB"):
        render_publication(b" " * 65537, 42, "")


@pytest.mark.parametrize(
    ("name", "length", "valid"), [("observe.json", 4, True), ("other.json", 4, False), ("observe.json", 65537, False)]
)
def test_artifact_requires_the_named_bounded_member(name: str, length: int, valid: bool) -> None:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, b" " * length)
    if valid:
        assert prepare.read_member(stream.getvalue(), "observe.json") == b" " * length
    else:
        with pytest.raises(ValueError, match="member or size"):
            prepare.read_member(stream.getvalue(), "observe.json")


def test_run_and_live_pr_select_the_autosync_destination(monkeypatch: pytest.MonkeyPatch) -> None:
    sha = "a" * 40
    repo = {"id": 7, "full_name": "example/project", "default_branch": "main"}
    head = {"repo": repo, "sha": sha, "ref": "topic"}
    run = {
        "head_repository": repo,
        "head_sha": sha,
        "head_branch": "topic",
        "pull_requests": [{"number": 12, "head": {"sha": sha}}],
        "path": prepare.POLICY,
        "event": "pull_request",
        "conclusion": "success",
    }
    pr = {"state": "open", "head": head, "base": {"ref": "main"}, "labels": [], "user": {"login": "author"}}
    responses = {"repos/example/project/pulls/12": pr, "repos/example/project/branches/topic": {"protected": False}}
    monkeypatch.setattr(prepare, "api", responses.__getitem__)
    monkeypatch.setattr(prepare, "artifact", lambda *_: b"")
    event = {"workflow_run": run, "repository": repo}
    assert prepare.prepare(event) == {"kind": "autosync", "ref": "topic", "sha": sha, "data": ""}
    responses["repos/example/project/branches/topic"]["protected"] = True
    with pytest.raises(ValueError, match="protected branch"):
        prepare.prepare(event)
    head["sha"] = "b" * 40
    with pytest.raises(ValueError, match="advanced"):
        prepare.prepare(event)
    pr["labels"] = [{"name": "skip-autosync"}]
    assert prepare.prepare(event) == {}
    run["head_repository"] = {"id": 8}
    assert prepare.prepare(event) == {}


def test_publish_builds_a_single_parent_commit_without_a_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = tmp_path / "project.git"
    directory = tmp_path / "objects"
    directory.mkdir()
    subprocess.run(["git", "init", "-q", str(origin)], check=True)
    git = ["git", "-C", str(origin)]
    script = origin / "module.py"
    script.write_text("answer=42\n")
    subprocess.run([*git, "add", "module.py"], check=True)
    subprocess.run(
        [*git, "-c", "user.name=Test", "-c", "user.email=test@example.org", "commit", "-qm", "Initial"], check=True
    )
    sha = subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip()
    script.write_text("answer = 42\n")
    diff = subprocess.check_output([*git, "diff"])

    monkeypatch.setenv("GITHUB_SERVER_URL", tmp_path.as_uri())
    monkeypatch.setenv("GITHUB_REPOSITORY", "project")
    monkeypatch.setenv("BERNSTEIN_AUTOSYNC_TOKEN", "fixture")
    monkeypatch.setenv("PATCH", base64.b64encode(diff).decode())
    monkeypatch.setenv("PR_HEAD_SHA", sha)
    monkeypatch.setenv("PR_HEAD_REF", "topic")
    real_run = subprocess.run
    pushes = []

    def record_push(args: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        if "push" in args:
            pushes.append(args)
            return subprocess.CompletedProcess(args, 0)
        return real_run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", record_push)
    autosync.publish(str(directory))
    (push,) = pushes
    new_commit, ref = push[-1].split(":")
    assert ref == "refs/heads/topic" and f"--force-with-lease={ref}:{sha}" in push
    bare = ["git", "-C", str(directory)]
    assert subprocess.check_output([*bare, "rev-parse", "--is-bare-repository"], text=True).strip() == "true"
    assert subprocess.check_output([*bare, "show", "-s", "--format=%P", new_commit], text=True).strip() == sha
    assert subprocess.check_output([*bare, "show", f"{new_commit}:module.py"]) == script.read_bytes()
    assert not (directory / "module.py").exists()


@pytest.mark.parametrize(
    ("modes", "change", "name"),
    [
        ("100644 100644", "M", ".github/workflows/main.yml"),
        ("100644 100755", "M", "app.py"),
        ("000000 100644", "A", "new.py"),
    ],
)
def test_branch_publisher_rejects_changes_outside_the_formatting_contract(modes: str, change: str, name: str) -> None:
    with pytest.raises(ValueError):
        autosync.validate_changes(f":{modes} abc def {change}\0{name}\0".encode())
