"""CLI tests for ``bernstein ledger`` (issue #2358).

``verify`` walks the chain and names the exact tampered position;
``anchor`` publishes the chain to the ledger ref and mirrors a
``work_ledger.anchor`` event into the HMAC audit chain; ``fetch``
materializes an anchored chain on a clone; ``resume`` verifies end to
end, replays scheduler state, and refuses divergent chains with a clear
operator message; ``gc`` squashes anchor history.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.ledger_cmd import ledger_group
from bernstein.core.git.git_basic import GitResult
from bernstein.core.persistence.ledger_git import anchor_ledger, fetch_ledger_ref, ledger_ref
from bernstein.core.persistence.work_ledger import (
    KIND_RUN_OPEN,
    KIND_RUN_RESUMED,
    KIND_TASK_COMPLETED,
    KIND_TASK_SCHEDULED,
    KIND_TASK_STARTED,
    LedgerError,
    LedgerReader,
    WorkLedger,
    run_ledger_dir,
)
from bernstein.core.security.audit_chain import EVENT_WORK_LEDGER_ANCHOR, AuditChainStore


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.stdout.strip()


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    repo = tmp_path / "origin"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    return repo


def _seed(project: Path, run_id: str = "run-a") -> WorkLedger:
    ledger = WorkLedger.open(run_ledger_dir(project / ".sdd", run_id))
    ledger.append(kind=KIND_RUN_OPEN, payload={"run_id": run_id})
    ledger.append(kind=KIND_TASK_SCHEDULED, task_id="t1")
    ledger.append(kind=KIND_TASK_SCHEDULED, task_id="t2")
    ledger.append(kind=KIND_TASK_STARTED, task_id="t1")
    ledger.append(kind=KIND_TASK_COMPLETED, task_id="t1")
    ledger.append(kind=KIND_TASK_STARTED, task_id="t2")
    return ledger


class TestVerify:
    def test_verify_ok(self, project: Path) -> None:
        _seed(project)
        result = CliRunner().invoke(ledger_group, ["verify", "run-a", "--workdir", str(project)])
        assert result.exit_code == 0, result.output

    def test_verify_missing_ledger(self, project: Path) -> None:
        result = CliRunner().invoke(ledger_group, ["verify", "run-x", "--workdir", str(project)])
        assert result.exit_code == 1, result.output
        assert "No work ledger" in result.stderr
        assert result.stdout == ""

    def test_verify_tamper_names_exact_position(self, project: Path) -> None:
        ledger = _seed(project)
        bucket = ledger.ledger_dir / "000000.jsonl"
        lines = bucket.read_text(encoding="utf-8").splitlines()
        row = json.loads(lines[2])
        row["task_id"] = "evil"
        lines[2] = json.dumps(row, sort_keys=True, separators=(",", ":"))
        bucket.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = CliRunner().invoke(ledger_group, ["verify", "run-a", "--workdir", str(project)])
        assert result.exit_code == 2, result.output
        assert "entry 2" in result.stderr
        assert result.stdout == ""

        json_result = CliRunner().invoke(ledger_group, ["verify", "run-a", "--workdir", str(project), "--json"])
        assert json_result.exit_code == 2, json_result.output
        assert json_result.stderr == ""
        payload = json.loads(json_result.stdout)
        assert payload["ok"] is False
        assert any("entry 2" in message for message in payload["errors"])

    def test_verify_json(self, project: Path) -> None:
        ledger = _seed(project)
        result = CliRunner().invoke(
            ledger_group,
            ["verify", "run-a", "--workdir", str(project), "--json"],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["ok"] is True
        assert payload["head_hash"] == ledger.head_hash


class TestAnchor:
    def test_anchor_publishes_ref_and_audit_event(self, project: Path) -> None:
        _seed(project)
        result = CliRunner().invoke(ledger_group, ["anchor", "run-a", "--workdir", str(project)])
        assert result.exit_code == 0, result.output
        assert _git(project, "rev-parse", "--verify", ledger_ref("run-a"))

        chain = AuditChainStore(project / ".sdd" / "audit")
        anchors = chain.query(event_type=EVENT_WORK_LEDGER_ANCHOR)
        assert len(anchors) == 1
        assert anchors[0].details["run_id"] == "run-a"
        assert anchors[0].details["entry_count"] == 6

    def test_anchor_missing_ledger(self, project: Path) -> None:
        result = CliRunner().invoke(ledger_group, ["anchor", "run-x", "--workdir", str(project)])
        assert result.exit_code == 1, result.output


class TestFetchAndResume:
    def test_fetch_then_resume_on_clone(self, project: Path, tmp_path: Path) -> None:
        ledger = _seed(project)
        CliRunner().invoke(ledger_group, ["anchor", "run-a", "--workdir", str(project)])

        clone = tmp_path / "machine-b"
        _git(project.parent, "clone", str(project), str(clone))
        fetched = CliRunner().invoke(ledger_group, ["fetch", "run-a", "--workdir", str(clone)])
        assert fetched.exit_code == 0, fetched.output

        resumed = CliRunner().invoke(
            ledger_group,
            ["resume", "run-a", "--workdir", str(clone), "--dry-run", "--json"],
        )
        assert resumed.exit_code == 0, resumed.output
        payload = json.loads(resumed.output)
        assert payload["completed_tasks"] == ["t1"]
        assert payload["resume_frontier"] == ["t2"]
        assert payload["head_hash"] == ledger.head_hash
        assert payload["dispatch_status"] == "not_dispatched"
        assert payload["signal_paths"] == []
        assert payload["runtime_acknowledged"] is False

    @pytest.mark.parametrize("output_json", [False, True], ids=["text", "json"])
    def test_resume_writes_signals_and_resume_entry(self, project: Path, output_json: bool) -> None:
        _seed(project)
        args = ["resume", "run-a", "--workdir", str(project)]
        if output_json:
            args.append("--json")
        result = CliRunner().invoke(ledger_group, args, color=True)
        assert result.exit_code == 0, result.output

        signal = project / ".sdd" / "runtime" / "resume" / "t2.signal"
        assert signal.exists()
        assert not signal.with_name("t1.signal").exists()
        entries = list(LedgerReader(run_ledger_dir(project / ".sdd", "run-a")).entries())
        assert entries[-1].kind == KIND_RUN_RESUMED
        if output_json:
            payload = json.loads(result.stdout)
            assert payload["dispatch_status"] == "queued"
            assert payload["signal_paths"] == [str(signal)]
            assert payload["runtime_acknowledged"] is False
        else:
            assert "Runtime acknowledgement: unavailable" in result.output

    def test_resume_empty_frontier_reports_no_dispatch(self, project: Path) -> None:
        ledger = _seed(project)
        ledger.append(kind=KIND_TASK_COMPLETED, task_id="t2")
        ledger.close()

        result = CliRunner().invoke(ledger_group, ["resume", "run-a", "--workdir", str(project), "--json"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["resume_frontier"] == []
        assert payload["dispatch_status"] == "not_dispatched"
        assert payload["signal_paths"] == []
        assert payload["runtime_acknowledged"] is False

    def test_resume_signal_write_failure_reports_signals_already_written(self, project: Path) -> None:
        ledger = _seed(project)
        ledger.append(kind=KIND_TASK_SCHEDULED, task_id="t3")
        ledger.close()
        signal_dir = project / ".sdd" / "runtime" / "resume"
        (signal_dir / "t3.signal").mkdir(parents=True)

        result = CliRunner().invoke(ledger_group, ["resume", "run-a", "--workdir", str(project), "--json"])

        assert result.exit_code == 1, result.output
        payload = json.loads(result.stdout)
        assert payload["signal_paths"] == [str(signal_dir / "t2.signal")]
        assert payload["runtime_acknowledged"] is False
        assert "signal" in payload["error"].lower()

    @pytest.mark.parametrize("dry_run", [False, True], ids=["record", "dry-run"])
    def test_resume_from_torn_append(self, project: Path, dry_run: bool) -> None:
        ledger = _seed(project)
        previous_head = ledger.head_hash
        intact = ledger.bucket_path.read_bytes()
        interrupted = ledger.append(kind=KIND_TASK_COMPLETED, task_id="t2")
        torn = intact + interrupted.canonical_line().encode("utf-8")[:20]
        ledger.bucket_path.write_bytes(torn)
        ledger.close()

        args = ["resume", "run-a", "--workdir", str(project), "--json"]
        if dry_run:
            args.append("--dry-run")
        result = CliRunner().invoke(ledger_group, args)

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["head_hash"] == previous_head
        assert payload["resume_frontier"] == ["t2"]
        if dry_run:
            assert ledger.bucket_path.read_bytes() == torn
        else:
            reader = LedgerReader(ledger.ledger_dir)
            last = list(reader.entries())[-1]
            assert last.kind == KIND_RUN_RESUMED
            assert last.prev_hash == previous_head
            assert reader.verify(expected_head=last.entry_hash).ok
            assert ledger.bucket_path.read_bytes().startswith(intact)

    @pytest.mark.parametrize("local_copy", ["partial", "missing"])
    def test_dry_run_projects_anchored_extension_without_writing(self, project: Path, local_copy: str) -> None:
        ledger = _seed(project)
        intact = ledger.bucket_path.read_bytes()
        completion = ledger.append(kind=KIND_TASK_COMPLETED, task_id="t2")
        anchor_ledger(project, ledger.ledger_dir, run_id="run-a")
        ledger.close()
        local_before = intact + completion.canonical_line().encode("utf-8")[:20]
        if local_copy == "partial":
            ledger.bucket_path.write_bytes(local_before)
        else:
            ledger.bucket_path.unlink()

        result = CliRunner().invoke(ledger_group, ["resume", "run-a", "--workdir", str(project), "--json", "--dry-run"])

        assert result.exit_code == 0, result.output
        report = json.loads(result.stdout)
        assert report["head_hash"] == completion.entry_hash
        assert report["completed_tasks"] == ["t1", "t2"]
        assert report["resume_frontier"] == []
        if local_copy == "partial":
            assert ledger.bucket_path.read_bytes() == local_before
        else:
            assert not ledger.bucket_path.exists()

    def test_resume_append_failure_keeps_json_parseable(self, project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _seed(project)

        def fail_append(*_args: object, **_kwargs: object) -> None:
            raise LedgerError("write failed")

        monkeypatch.setattr(WorkLedger, "append", fail_append)
        result = CliRunner().invoke(ledger_group, ["resume", "run-a", "--workdir", str(project), "--json"])

        assert result.exit_code == 2, result.output
        payload = json.loads(result.stdout)
        assert payload["run_id"] == "run-a"
        assert payload["dispatch_status"] == "not_dispatched"
        assert payload["runtime_acknowledged"] is False
        assert "write failed" in payload["error"]
        assert "Failed to record the resume entry: write failed" in result.stderr

    @pytest.mark.parametrize("denied_mode", ["r+b", "a"], ids=["recovery", "append"])
    def test_resume_writer_io_error_exits_one(
        self, project: Path, monkeypatch: pytest.MonkeyPatch, denied_mode: str
    ) -> None:
        ledger = _seed(project)
        bucket = ledger.bucket_path
        ledger.close()
        before = bucket.read_bytes()
        path_open = Path.open

        def deny_write(path: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
            if path == bucket and mode == denied_mode:
                raise PermissionError("ledger write denied")
            return path_open(path, mode, *args, **kwargs)

        monkeypatch.setattr(Path, "open", deny_write)
        result = CliRunner().invoke(ledger_group, ["resume", "run-a", "--workdir", str(project), "--json"])
        assert result.exit_code == 1, result.output
        report = json.loads(result.stdout)
        assert "ledger write denied" in report["error"]
        assert report["dispatch_status"] == "not_dispatched"
        assert report["signal_paths"] == []
        assert bucket.read_bytes() == before
        assert not (project / ".sdd" / "runtime" / "resume").exists()

    def test_resume_invalid_run_id_returns_json(self, project: Path) -> None:
        result = CliRunner().invoke(ledger_group, ["resume", "bad id", "--workdir", str(project), "--json"])
        assert result.exit_code == 2, result.exception
        report = json.loads(result.stdout)
        assert report["run_id"] == "bad id"
        assert "run id" in report["error"]
        assert report["dispatch_status"] == "not_dispatched"
        assert report["signal_paths"] == []

    @pytest.mark.parametrize("timed_out", ["rev-parse", "cat-file"])
    def test_resume_anchor_lookup_timeout_reports_io_error(self, project: Path, timed_out: str) -> None:
        ledger = _seed(project)
        anchor_ledger(project, ledger.ledger_dir, run_id="run-a")
        before = ledger.bucket_path.read_bytes()
        run = subprocess.run

        def timeout_on_read(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
            if args[:2] == ["git", timed_out]:
                raise subprocess.TimeoutExpired(args, 30)
            return run(args, **kwargs)

        with patch.object(subprocess, "run", side_effect=timeout_on_read):
            result = CliRunner().invoke(ledger_group, ["resume", "run-a", "--workdir", str(project), "--json"])
        assert result.exit_code == 1, result.output
        report = json.loads(result.stdout)
        assert "timed out" in report["error"]
        assert report["dispatch_status"] == "not_dispatched"
        assert ledger.bucket_path.read_bytes() == before

    @pytest.mark.parametrize("dry_run", [False, True], ids=["queue", "dry-run"])
    @pytest.mark.parametrize("damaged", ["config", "HEAD"])
    def test_resume_stops_on_fatal_git_lookup(self, project: Path, dry_run: bool, damaged: str) -> None:
        ledger = _seed(project)
        anchor_ledger(project, ledger.ledger_dir, run_id="run-a")
        before = ledger.bucket_path.read_bytes()
        (project / ".git" / damaged).write_text("incomplete fixture\n", encoding="utf-8")
        args = ["resume", "run-a", "--workdir", str(project), "--json"]
        if dry_run:
            args.append("--dry-run")
        result = CliRunner().invoke(ledger_group, args)
        assert result.exit_code == 1, result.output
        report = json.loads(result.stdout)
        assert "git" in report["error"].lower()
        assert report["dispatch_status"] == "not_dispatched"
        assert report["signal_paths"] == []
        assert ledger.bucket_path.read_bytes() == before
        assert not (project / ".sdd" / "runtime" / "resume").exists()

    def test_resume_local_ledger_outside_git(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        project = tmp_path / "ordinary-workspace"
        monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
        _seed(project)
        (project / "objects").mkdir()
        (project / "refs").mkdir()
        result = CliRunner().invoke(ledger_group, ["resume", "run-a", "--workdir", str(project), "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["dispatch_status"] == "queued"

    def test_git_ceiling_at_current_workdir_does_not_hide_a_broken_checkout(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ledger = _seed(project)
        anchor_ledger(project, ledger.ledger_dir, run_id="run-a")
        before = ledger.bucket_path.read_bytes()
        (project / ".git" / "HEAD").write_text("unreadable ref\n", encoding="utf-8")
        monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(project))

        result = CliRunner().invoke(ledger_group, ["resume", "run-a", "--workdir", str(project), "--json"])
        assert result.exit_code == 1, result.output
        report = json.loads(result.stdout)
        assert "git" in report["error"].lower()
        assert report["signal_paths"] == []
        assert report["dispatch_status"] == "not_dispatched"
        assert ledger.bucket_path.read_bytes() == before
        assert not (project / ".sdd" / "runtime" / "resume").exists()

    @pytest.mark.parametrize("suffix", [os.sep, f"{os.sep}."], ids=["trailing-separator", "dot"])
    def test_resume_local_ledger_below_git_ceiling(
        self, project: Path, monkeypatch: pytest.MonkeyPatch, suffix: str
    ) -> None:
        workdir = project / "workspace"
        _seed(workdir)
        monkeypatch.setenv("GIT_CEILING_DIRECTORIES", f"{project}{suffix}")

        result = CliRunner().invoke(ledger_group, ["resume", "run-a", "--workdir", str(workdir), "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["dispatch_status"] == "queued"

    @pytest.mark.parametrize(
        ("separator", "exit_code", "dispatch"),
        [(os.pathsep, 0, "queued"), (os.pathsep * 2, 1, "not_dispatched")],
        ids=["resolve-symlink", "literal-after-empty-entry"],
    )
    def test_resume_honors_git_ceiling_symlink_rule(
        self, project: Path, monkeypatch: pytest.MonkeyPatch, separator: str, exit_code: int, dispatch: str
    ) -> None:
        workdir = project / "workspace"
        _seed(workdir)
        (project / ".git" / "HEAD").write_text("unreadable ref\n", encoding="utf-8")
        alias = project.parent / "ceiling-link"
        try:
            alias.symlink_to(project, target_is_directory=True)
        except OSError as exc:
            if getattr(exc, "winerror", None) == 1314:
                pytest.skip("Directory symlinks require a Windows privilege")
            raise
        monkeypatch.setenv("GIT_CEILING_DIRECTORIES", f"{project.parent.resolve()}{separator}{alias}")

        result = CliRunner().invoke(ledger_group, ["resume", "run-a", "--workdir", str(workdir), "--json"])
        assert result.exit_code == exit_code, result.output
        assert json.loads(result.stdout)["dispatch_status"] == dispatch

    @pytest.mark.parametrize(
        ("stopped_at_mount", "marker_on_mount", "exit_code"),
        [(True, False, 0), (True, True, 1), (False, False, 1)],
        ids=["checkout-above-mount", "checkout-on-mount", "cross-device-discovery"],
    )
    def test_resume_respects_git_filesystem_boundary(
        self,
        project: Path,
        monkeypatch: pytest.MonkeyPatch,
        stopped_at_mount: bool,
        marker_on_mount: bool,
        exit_code: int,
    ) -> None:
        volume = project.resolve() / "volume"
        workdir = volume / "workspace"
        ledger = _seed(workdir)
        before = ledger.bucket_path.read_bytes()
        (project / ".git" / "HEAD").write_text("unreadable ref\n", encoding="utf-8")
        if marker_on_mount:
            (volume / ".git").mkdir()
        diagnostic = "fatal: not a git repository (or any of the parent directories): .git"
        if stopped_at_mount:
            diagnostic = (
                f"fatal: not a git repository (or any parent up to mount point {project})\n"
                "Stopping at filesystem boundary (GIT_DISCOVERY_ACROSS_FILESYSTEM not set)."
            )
        monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(project.parent.resolve()))
        monkeypatch.setenv("GIT_DISCOVERY_ACROSS_FILESYSTEM", "false" if stopped_at_mount else "true")
        stat_path = Path.stat

        def mounted_stat(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
            result = stat_path(path, follow_symlinks=follow_symlinks)
            if path not in (volume, workdir):
                return result
            fields = list(result)
            fields[stat.ST_DEV] += 1
            return os.stat_result(fields)

        monkeypatch.setattr(Path, "stat", mounted_stat)
        git_failure = GitResult(returncode=128, stdout="", stderr=diagnostic)
        with patch("bernstein.core.persistence.ledger_git.run_git", return_value=git_failure):
            result = CliRunner().invoke(ledger_group, ["resume", "run-a", "--workdir", str(workdir), "--json"])
        assert result.exit_code == exit_code, result.output
        report = json.loads(result.stdout)
        if exit_code == 0:
            assert report["dispatch_status"] == "queued"
        else:
            assert report["signal_paths"] == []
            assert report["dispatch_status"] == "not_dispatched"
            assert ledger.bucket_path.read_bytes() == before
            assert not (workdir / ".sdd" / "runtime" / "resume").exists()

    @pytest.mark.parametrize("git_read", ["ls-tree", "cat-file"], ids=["tree", "blob"])
    @pytest.mark.parametrize(
        ("returncode", "git_stderr", "expected_error"),
        [
            (
                128,
                "error: unable to open loose object: Permission denied\nfatal: unable to read object\n",
                "Permission denied",
            ),
            (-15, "", "signal 15"),
        ],
        ids=["permission-denied", "terminated"],
    )
    def test_resume_reports_git_object_read_io_errors(
        self, project: Path, git_read: str, returncode: int, git_stderr: str, expected_error: str
    ) -> None:
        ledger = _seed(project)
        anchor_ledger(project, ledger.ledger_dir, run_id="run-a")
        before = ledger.bucket_path.read_bytes()
        run = subprocess.run

        def fail_read(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
            if args[:2] == ["git", git_read]:
                if kwargs.get("text"):
                    return subprocess.CompletedProcess(args, returncode, "", git_stderr)
                return subprocess.CompletedProcess(args, returncode, b"", git_stderr.encode())
            return run(args, **kwargs)

        with patch.object(subprocess, "run", side_effect=fail_read):
            result = CliRunner().invoke(ledger_group, ["resume", "run-a", "--workdir", str(project), "--json"])
        assert result.exit_code == 1, result.output
        report = json.loads(result.stdout)
        assert expected_error in report["error"]
        assert report["signal_paths"] == []
        assert report["dispatch_status"] == "not_dispatched"
        assert ledger.bucket_path.read_bytes() == before
        assert not (project / ".sdd" / "runtime" / "resume").exists()

    @pytest.mark.parametrize(("field", "value"), [("ts", "yesterday"), ("redactions", []), ("schema_version", None)])
    def test_resume_reports_invalid_row_metadata(self, project: Path, field: str, value: object) -> None:
        ledger = _seed(project)
        bucket = ledger.bucket_path
        ledger.close()
        rows = bucket.read_text(encoding="utf-8").splitlines()
        row = json.loads(rows[2])
        row[field] = value
        rows[2] = json.dumps(row)
        bucket.write_text("\n".join(rows) + "\n", encoding="utf-8")
        before = bucket.read_bytes()
        result = CliRunner().invoke(ledger_group, ["resume", "run-a", "--workdir", str(project), "--json"])
        assert result.exit_code == 2, result.exception
        report = json.loads(result.stdout)
        assert any("entry 2" in error and field in error for error in report["errors"])
        assert report["signal_paths"] == []
        assert bucket.read_bytes() == before
        assert not (project / ".sdd" / "runtime" / "resume").exists()

    @pytest.mark.parametrize("run_id", ["run-a", "r" * 65], ids=["local-ahead", "local-only-id"])
    def test_resume_verified_local_history_without_an_anchor_update(self, project: Path, run_id: str) -> None:
        ledger = _seed(project, run_id)
        if run_id == "run-a":
            anchor_ledger(project, ledger.ledger_dir, run_id=run_id)
            ledger.append(kind=KIND_TASK_SCHEDULED, task_id="t3")
        result = CliRunner().invoke(ledger_group, ["resume", run_id, "--workdir", str(project), "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["dispatch_status"] == "queued"

    @pytest.mark.parametrize("dry_run", [False, True], ids=["queue", "dry-run"])
    @pytest.mark.parametrize("local_copy", [False, True], ids=["anchor-only", "with-local"])
    @pytest.mark.parametrize("name", ["LEDGER.json", "chunk-000000.jsonl"])
    def test_resume_rejects_invalid_utf8_in_anchor(
        self, project: Path, name: str, local_copy: bool, dry_run: bool
    ) -> None:
        ledger = _seed(project)
        ref = anchor_ledger(project, ledger.ledger_dir, run_id="run-a").ref
        bucket = ledger.bucket_path
        ledger.close()
        before = bucket.read_bytes()
        if not local_copy:
            bucket.unlink()

        # Keep the entry hashes valid so the only fault is the blob's encoding.
        original = _git(project, "show", f"{ref}:{name}").encode("utf-8")
        _git(project, "read-tree", ref)
        (project / name).write_bytes(original.replace(b"{", b'{"note":"\xff",', 1))
        _git(project, "add", "--", name)
        tree = _git(project, "write-tree")
        commit = _git(project, "commit-tree", tree, "-p", ref, "-m", "invalid UTF-8 fixture")
        _git(project, "update-ref", ref, commit)

        args = ["resume", "run-a", "--workdir", str(project), "--json"]
        if dry_run:
            args.append("--dry-run")
        result = CliRunner().invoke(ledger_group, args)
        assert result.exit_code == 2, result.output
        report = json.loads(result.stdout)
        assert "UTF-8" in report["error"]
        assert report["dispatch_status"] == "not_dispatched"
        assert report["signal_paths"] == []
        assert not (project / ".sdd" / "runtime" / "resume").exists()
        if local_copy:
            assert bucket.read_bytes() == before
        else:
            assert not bucket.exists()

    def test_resume_missing_ledger(self, project: Path) -> None:
        result = CliRunner().invoke(ledger_group, ["resume", "run-x", "--workdir", str(project)])
        assert result.exit_code == 1, result.output

        result = CliRunner().invoke(ledger_group, ["resume", "run-x", "--workdir", str(project), "--json"])
        assert result.exit_code == 1, result.output
        payload = json.loads(result.stdout)
        assert payload["dispatch_status"] == "not_dispatched"
        assert payload["runtime_acknowledged"] is False
        assert "No work ledger" in payload["error"]

    @pytest.mark.parametrize("dry_run", [False, True], ids=["record", "dry-run"])
    @pytest.mark.parametrize("bad_encoding", [False, True], ids=["read-error", "invalid-utf8"])
    def test_resume_unreadable_ledger_returns_one_json_error(
        self, project: Path, dry_run: bool, bad_encoding: bool
    ) -> None:
        ledger = _seed(project)
        path = ledger.bucket_path
        ledger.close()
        if bad_encoding:
            path.write_bytes(b"\xff\n")
        else:
            path.unlink()
            path.mkdir()
        args = ["resume", "run-a", "--workdir", str(project), "--json"]
        if dry_run:
            args.append("--dry-run")
        result = CliRunner().invoke(ledger_group, args)
        assert result.exit_code == (2 if bad_encoding else 1), result.output
        report = json.loads(result.stdout)
        assert report["error"]
        assert report["signal_paths"] == []
        assert report["runtime_acknowledged"] is False
        assert report["dispatch_status"] == "not_dispatched"
        assert not (project / ".sdd" / "runtime" / "resume").exists()

    def test_resume_tampered_chain_exits_two(self, project: Path) -> None:
        ledger = _seed(project)
        bucket = ledger.ledger_dir / "000000.jsonl"
        lines = bucket.read_text(encoding="utf-8").splitlines()
        row = json.loads(lines[4])
        row["kind"] = KIND_TASK_STARTED
        row["task_id"] = "evil"
        lines[4] = json.dumps(row, sort_keys=True, separators=(",", ":"))
        bucket.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = CliRunner().invoke(ledger_group, ["resume", "run-a", "--workdir", str(project)])
        assert result.exit_code == 2, result.output
        assert "entry 4" in result.stderr
        assert result.stdout == ""

        result = CliRunner().invoke(ledger_group, ["resume", "run-a", "--workdir", str(project), "--json"])
        assert result.exit_code == 2, result.output
        payload = json.loads(result.stdout)
        assert payload["runtime_acknowledged"] is False
        assert any("entry 4" in error for error in payload["errors"])

    def test_divergent_resumes_refused_with_operator_message(self, project: Path, tmp_path: Path) -> None:
        """AC: two divergent resumes are detected and refused."""
        _seed(project)
        CliRunner().invoke(ledger_group, ["anchor", "run-a", "--workdir", str(project)])

        # Operator A resumes locally: the chain gains a resume entry at seq 6.
        assert CliRunner().invoke(ledger_group, ["resume", "run-a", "--workdir", str(project)]).exit_code == 0

        # Operator B independently resumes the same anchored head on a clone
        # and anchors their continuation back.
        clone = tmp_path / "machine-b"
        _git(project.parent, "clone", str(project), str(clone))
        fetch_ledger_ref(clone, "run-a", remote="origin")
        CliRunner().invoke(ledger_group, ["fetch", "run-a", "--workdir", str(clone)])
        assert CliRunner().invoke(ledger_group, ["resume", "run-a", "--workdir", str(clone)]).exit_code == 0
        _git(clone, "config", "user.email", "b@example.com")
        _git(clone, "config", "user.name", "B")
        anchor_ledger(clone, run_ledger_dir(clone / ".sdd", "run-a"), run_id="run-a")
        _git(project, "fetch", str(clone), f"+{ledger_ref('run-a')}:{ledger_ref('run-a')}")

        # The two chains now extend the same parent entry: A's next resume
        # must refuse, naming the exact fork position.
        result = CliRunner().invoke(ledger_group, ["resume", "run-a", "--workdir", str(project)])
        assert result.exit_code == 3, result.output
        assert "diverge" in result.stderr.lower()
        assert "entry 6" in result.stderr
        assert result.stdout == ""

        result = CliRunner().invoke(ledger_group, ["resume", "run-a", "--workdir", str(project), "--json"])
        assert result.exit_code == 3, result.output
        payload = json.loads(result.stdout)
        assert payload["dispatch_status"] == "not_dispatched"
        assert payload["runtime_acknowledged"] is False
        assert "entry 6" in payload["error"]


class TestTopLevelResumeHint:
    def test_resume_without_checkpoint_hints_at_ledger(self, project: Path) -> None:
        """``bernstein resume <id>`` routes the operator to the ledger surface."""
        from bernstein.cli.commands.resume_cmd import resume_cmd

        _seed(project)
        result = CliRunner().invoke(resume_cmd, ["run-a", "--workdir", str(project)])
        assert result.exit_code == 2, result.output
        assert "bernstein ledger resume run-a" in result.output

    def test_resume_without_checkpoint_or_ledger_has_no_hint(self, project: Path) -> None:
        from bernstein.cli.commands.resume_cmd import resume_cmd

        result = CliRunner().invoke(resume_cmd, ["ghost", "--workdir", str(project)])
        assert result.exit_code == 2, result.output
        assert "ledger resume" not in result.output


class TestGcAndRuns:
    def test_gc_squashes_history(self, project: Path) -> None:
        ledger = _seed(project)
        CliRunner().invoke(ledger_group, ["anchor", "run-a", "--workdir", str(project)])
        ledger.append(kind=KIND_TASK_COMPLETED, task_id="t2")
        CliRunner().invoke(ledger_group, ["anchor", "run-a", "--workdir", str(project)])

        result = CliRunner().invoke(ledger_group, ["gc", "run-a", "--workdir", str(project)])
        assert result.exit_code == 0, result.output
        history = _git(project, "rev-list", ledger_ref("run-a")).splitlines()
        assert len(history) == 1

    def test_gc_missing_ledger_reports_stderr(self, project: Path) -> None:
        result = CliRunner().invoke(ledger_group, ["gc", "run-x", "--workdir", str(project)])

        assert result.exit_code == 1, result.output
        assert "no anchored ledger" in result.stderr
        assert result.stdout == ""

    def test_runs_lists_anchored_ledgers(self, project: Path) -> None:
        _seed(project)
        CliRunner().invoke(ledger_group, ["anchor", "run-a", "--workdir", str(project)])
        result = CliRunner().invoke(ledger_group, ["runs", "--workdir", str(project)])
        assert result.exit_code == 0, result.output
        assert "run-a" in result.output

    def _resume_json(self, project: Path) -> dict[str, Any]:
        result = CliRunner().invoke(ledger_group, ["resume", "run-a", "--workdir", str(project), "--json"])
        assert result.exit_code == 0, result.output
        return json.loads(result.stdout)

    def test_duplicate_ledger_resume_keeps_request_ids_stable(self, project: Path) -> None:
        _seed(project)
        first = self._resume_json(project)
        second = self._resume_json(project)

        assert [item["request_id"] for item in first["resume_requests"]] == [
            item["request_id"] for item in second["resume_requests"]
        ]
        assert [item["dispatch_status"] for item in second["resume_requests"]] == ["queued"]
        assert second["runtime_acknowledged"] is False
        entries = list(LedgerReader(run_ledger_dir(project / ".sdd", "run-a")).entries())
        assert sum(entry.kind == KIND_RUN_RESUMED for entry in entries) == 2

    def test_mixed_ledger_requests_only_the_resume_frontier(self, project: Path) -> None:
        ledger = _seed(project)
        ledger.append(kind=KIND_TASK_SCHEDULED, task_id="t3")
        ledger.append(kind=KIND_TASK_COMPLETED, task_id="t3")
        ledger.append(kind=KIND_TASK_SCHEDULED, task_id="t4")
        ledger.close()

        report = self._resume_json(project)
        assert report["completed_tasks"] == ["t1", "t3"]
        assert report["resume_frontier"] == ["t2", "t4"]
        assert {item["task_id"] for item in report["resume_requests"]} == {"t2", "t4"}
        assert all(item["runtime_acknowledged"] is False for item in report["resume_requests"])
        assert not (project / ".sdd" / "runtime" / "resume" / "t1.signal").exists()
