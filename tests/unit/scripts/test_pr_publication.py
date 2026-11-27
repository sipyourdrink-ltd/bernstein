"""Exercise the publishing boundary using local data and a recorded push invocation."""

import base64
import io
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from scripts import prepare_pr_publication as prepare
from scripts import publish_autosync as autosync
from scripts.observability.build_pr_summary import render_publication
from scripts.verify_autosync import verify


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
    "payload",
    [
        b"[]",
        b"1",
        b'{"backends":["x"]}',
        b'{"summary":1}',
        b'{"backends":"x"}',
        b'{"backends":[{"metrics":["x"]}]}',
    ],
)
def test_render_publication_handles_malformed_json_shapes_without_attribute_error(payload: bytes) -> None:
    report = render_publication(payload, 42, "topic").decode()
    assert "observe JSON could not be parsed" in report
    assert "error=1" in report


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
        "display_title": "PR policy for 12 (opened)",
        "event": "pull_request",
        "conclusion": "success",
    }
    pr = {"state": "open", "head": head, "base": {"ref": "main"}, "labels": [], "user": {"login": "author"}}
    responses = {"repos/example/project/pulls/12": pr, "repos/example/project/branches/topic": {"protected": False}}
    monkeypatch.setattr(prepare, "api", responses.__getitem__)
    monkeypatch.setattr(prepare, "artifact", lambda *_: b"")
    event = {"workflow_run": run, "repository": repo}
    assert prepare.prepare(event) == {}
    monkeypatch.setattr(prepare, "artifact", lambda *_: b"patch")
    assert prepare.prepare(event) == {
        "kind": "autosync",
        "number": "12",
        "ref": "topic",
        "sha": sha,
        "data": base64.b64encode(b"patch").decode(),
    }
    responses["repos/example/project/branches/topic"]["protected"] = True
    with pytest.raises(ValueError, match="protected branch"):
        prepare.prepare(event)
    head["sha"] = "b" * 40
    with pytest.raises(ValueError, match="advanced"):
        prepare.prepare(event)
    monkeypatch.setattr(prepare, "artifact", lambda *_: b"")
    assert prepare.prepare(event) == {}  # No drift, even after the protected head advanced.
    monkeypatch.setattr(prepare, "artifact", lambda *_: b"patch")
    run["display_title"] = "PR policy for 12 (edited)"
    assert prepare.prepare(event) == {}
    run["display_title"] = "PR policy for 12 (opened)"
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
    monkeypatch.setattr(autosync, "current_target", lambda _remote, tip, ref: tip == sha and ref == "refs/heads/topic")
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


@pytest.mark.parametrize(
    ("state", "labels", "expected"),
    [("open", [], True), ("closed", [], False), ("open", [{"name": "skip-autosync"}], False)],
)
def test_recheck_honors_pr_eligibility_at_push_time(
    monkeypatch: pytest.MonkeyPatch, state: str, labels: list, expected: bool
) -> None:
    sha = "a" * 40
    repo = {"full_name": "example/project", "default_branch": "main"}
    pr = {
        "state": state,
        "labels": labels,
        "head": {"ref": "topic", "sha": sha, "repo": repo},
        "base": {"ref": "main", "repo": repo},
    }
    responses = {
        "repos/example/project/pulls/12": pr,
        "repos/example/project/branches/topic": {"protected": False, "commit": {"sha": sha}},
    }
    monkeypatch.setenv("PR_NUMBER", "12")
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/project")
    monkeypatch.setattr(subprocess, "check_output", lambda command: json.dumps(responses[command[-1]]).encode())
    assert autosync.current_target("unused", sha, "refs/heads/topic") is expected


@pytest.mark.skipif(sys.platform == "win32", reason="the verifier runs on the Ubuntu Actions runner")
def test_trusted_regeneration_rejects_semantic_edit_to_python_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trusted = tmp_path / "trusted"
    source = tmp_path / "project"
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    (source / "pyproject.toml").write_text('[project]\nname = "fixture"\nversion = "1.0"\n')
    nested = source / "sdk"
    nested.mkdir()
    nested_settings = nested / "pyproject.toml"
    nested_settings.write_text('[tool.ruff.format]\nquote-style = "single"\n')
    (nested / "example.py").write_text('message="nested"\n')
    target = source / "module.py"
    target.write_text("answer = 42\n")
    git = ["git", "-C", str(source)]
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run(
        [*git, "-c", "user.name=Test", "-c", "user.email=test@example.org", "commit", "-qm", "Initial"], check=True
    )
    subprocess.run(["git", "clone", "-q", str(source), str(trusted)], check=True)
    sha = subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip()
    monkeypatch.setenv("DEFAULT_BRANCH", "main")
    target.write_text("answer = 43\n")
    proposed = subprocess.check_output([*git, "diff", "--full-index"])
    subprocess.run([*git, "restore", "module.py"], check=True)
    with pytest.raises(ValueError, match="differ from the independently regenerated"):
        verify(trusted, source, sha, proposed)
    assert target.read_text() == "answer = 42\n"
    assert (nested / "example.py").read_text() == "message = 'nested'\n"

    # Produce the expected patch using the normal CLI and native nested Ruff
    # discovery on a separate copy, independent of the verifier implementation.
    producer = tmp_path / "producer"
    subprocess.run(["git", "clone", "-q", str(source), str(producer)], check=True)
    tools = Path(sys.executable).parent
    cli = shutil.which("bernstein", path=str(tools))
    ruff = shutil.which("ruff", path=str(tools))
    assert cli and ruff
    subprocess.run([cli, "agents-md", "sync", "--workdir", ".", "--default-branch", "main"], cwd=producer, check=True)
    subprocess.run([ruff, "check", ".", "--fix", "--unsafe-fixes", "--no-cache"], cwd=producer, check=True)
    subprocess.run([ruff, "format", ".", "--no-cache"], cwd=producer, check=True)
    subprocess.run(["git", "add", "-A"], cwd=producer, check=True)
    expected = subprocess.check_output(
        ["git", "diff", "--cached", "--no-renames", "--full-index", "--binary", sha], cwd=producer
    )
    clean = tmp_path / "clean"
    subprocess.run(["git", "clone", "-q", str(source), str(clean)], check=True)
    verify(trusted, clean, sha, expected)

    changed = tmp_path / "changed-config"
    subprocess.run(["git", "clone", "-q", str(source), str(changed)], check=True)
    (changed / "sdk" / "pyproject.toml").write_text('[tool.ruff.format]\nquote-style = "double"\n')
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.org", "commit", "-qam", "Change Ruff config"],
        cwd=changed,
        check=True,
    )
    changed_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=changed, text=True).strip()
    with pytest.raises(ValueError, match="trusted configuration hierarchy"):
        verify(trusted, changed, changed_sha, expected)
