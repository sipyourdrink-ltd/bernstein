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
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.ledger_cmd import ledger_group
from bernstein.core.persistence.ledger_git import anchor_ledger, fetch_ledger_ref, ledger_ref
from bernstein.core.persistence.work_ledger import (
    KIND_RUN_OPEN,
    KIND_RUN_RESUMED,
    KIND_TASK_COMPLETED,
    KIND_TASK_SCHEDULED,
    KIND_TASK_STARTED,
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
        assert "entry 2" in result.output

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

    def test_resume_writes_signals_and_resume_entry(self, project: Path) -> None:
        _seed(project)
        result = CliRunner().invoke(ledger_group, ["resume", "run-a", "--workdir", str(project)])
        assert result.exit_code == 0, result.output

        signal = project / ".sdd" / "runtime" / "resume" / "t2.signal"
        assert signal.exists()
        entries = list(LedgerReader(run_ledger_dir(project / ".sdd", "run-a")).entries())
        assert entries[-1].kind == KIND_RUN_RESUMED

    def test_resume_missing_ledger(self, project: Path) -> None:
        result = CliRunner().invoke(ledger_group, ["resume", "run-x", "--workdir", str(project)])
        assert result.exit_code == 1, result.output

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
        assert "entry 4" in result.output

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
        _git(clone, "push", "origin", f"{ledger_ref('run-a')}:{ledger_ref('run-a')}", "--force")

        # The two chains now extend the same parent entry: A's next resume
        # must refuse, naming the exact fork position.
        result = CliRunner().invoke(ledger_group, ["resume", "run-a", "--workdir", str(project)])
        assert result.exit_code == 3, result.output
        assert "diverge" in result.output.lower()
        assert "entry 6" in result.output


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

    def test_runs_lists_anchored_ledgers(self, project: Path) -> None:
        _seed(project)
        CliRunner().invoke(ledger_group, ["anchor", "run-a", "--workdir", str(project)])
        result = CliRunner().invoke(ledger_group, ["runs", "--workdir", str(project)])
        assert result.exit_code == 0, result.output
        assert "run-a" in result.output
