"""Chaos round-trip for the durable work ledger (issue #2358).

Simulates the full recovery story end to end, in CI:

1. A run records task-graph transitions into the hash-chained ledger and
   anchors the chain to the ledger ref after every completed task.
2. The host dies mid-write (a torn trailing line, exactly what a SIGKILL
   during ``write`` leaves behind).
3. Same-host recovery: reopening the ledger drops the torn tail and keeps
   every recorded transition.
4. Machine move: ``git clone`` to a second machine, fetch the ledger ref,
   materialize, resume. Every completed task survives with zero loss and
   the in-flight task is the resume frontier.
5. The round trip (anchor, fetch, materialize, resume) reproduces the
   byte-identical canonical chain and the same head hash.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.ledger_cmd import ledger_group
from bernstein.core.persistence.ledger_git import anchor_ledger
from bernstein.core.persistence.work_ledger import (
    KIND_RUN_OPEN,
    KIND_TASK_COMPLETED,
    KIND_TASK_SCHEDULED,
    KIND_TASK_STARTED,
    LedgerReader,
    WorkLedger,
    replay_state,
    run_ledger_dir,
)


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
def host_a(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    repo = tmp_path / "host-a"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "a@example.com")
    _git(repo, "config", "user.name", "A")
    (repo / "README.md").write_text("project\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    return repo


def test_kill_mid_run_clone_resume_zero_lost_completed_work(host_a: Path, tmp_path: Path) -> None:
    """AC: kill the host mid-run, clone elsewhere, resume with zero loss."""
    run_id = "goal-42"
    ledger = WorkLedger.open(run_ledger_dir(host_a / ".sdd", run_id))
    ledger.append(kind=KIND_RUN_OPEN, payload={"run_id": run_id})
    for task in ("t1", "t2", "t3"):
        ledger.append(kind=KIND_TASK_SCHEDULED, task_id=task)

    # Two tasks complete; the chain is anchored after each completion.
    for task in ("t1", "t2"):
        ledger.append(kind=KIND_TASK_STARTED, task_id=task)
        ledger.append(kind=KIND_TASK_COMPLETED, task_id=task, payload={"commit": f"sha-{task}"})
        anchor_ledger(host_a, ledger.ledger_dir, run_id=run_id)

    # Third task starts and is anchored mid-flight...
    ledger.append(kind=KIND_TASK_STARTED, task_id="t3")
    last_anchor = anchor_ledger(host_a, ledger.ledger_dir, run_id=run_id)

    # ...then the host dies mid-write: a torn, half-flushed trailing line.
    bucket = ledger.ledger_dir / "000000.jsonl"
    with bucket.open("a", encoding="utf-8") as fh:
        fh.write('{"seq": 99, "kind": "task.comp')

    # Same-host recovery: reopening drops the torn tail, keeps every entry.
    recovered = WorkLedger.open(ledger.ledger_dir)
    assert recovered.head_hash == last_anchor.head_hash
    assert recovered.next_seq == last_anchor.entry_count

    # Machine move: clone, fetch the ledger ref, resume.
    host_b = tmp_path / "host-b"
    _git(host_a.parent, "clone", str(host_a), str(host_b))
    runner = CliRunner()
    assert runner.invoke(ledger_group, ["fetch", run_id, "--workdir", str(host_b)]).exit_code == 0

    resumed = runner.invoke(
        ledger_group,
        ["resume", run_id, "--workdir", str(host_b), "--json"],
    )
    assert resumed.exit_code == 0, resumed.output
    payload = json.loads(resumed.output)

    # Zero lost completed work; the in-flight task is the resume frontier.
    assert payload["completed_tasks"] == ["t1", "t2"]
    assert payload["resume_frontier"] == ["t3"]
    assert payload["head_hash"] == last_anchor.head_hash
    assert (host_b / ".sdd" / "runtime" / "resume" / "t3.signal").exists()


def test_round_trip_is_byte_identical(host_a: Path, tmp_path: Path) -> None:
    """AC: ledger round-trip (export, import, resume) reproduces the chain."""
    run_id = "goal-7"
    ledger = WorkLedger.open(run_ledger_dir(host_a / ".sdd", run_id))
    ledger.append(kind=KIND_RUN_OPEN, payload={"run_id": run_id})
    ledger.append(kind=KIND_TASK_SCHEDULED, task_id="t1")
    ledger.append(kind=KIND_TASK_STARTED, task_id="t1")
    ledger.append(kind=KIND_TASK_COMPLETED, task_id="t1")
    anchor = anchor_ledger(host_a, ledger.ledger_dir, run_id=run_id)

    host_b = tmp_path / "host-b"
    _git(host_a.parent, "clone", str(host_a), str(host_b))
    runner = CliRunner()
    assert runner.invoke(ledger_group, ["fetch", run_id, "--workdir", str(host_b)]).exit_code == 0

    source = (ledger.ledger_dir / "000000.jsonl").read_bytes()
    mirrored = (run_ledger_dir(host_b / ".sdd", run_id) / "000000.jsonl").read_bytes()
    assert mirrored == source

    reader = LedgerReader(run_ledger_dir(host_b / ".sdd", run_id))
    verification = reader.verify(expected_head=anchor.head_hash)
    assert verification.ok

    state = replay_state(reader.entries(), run_id=run_id)
    assert state.completed_tasks == ["t1"]
    assert state.head_hash == anchor.head_hash
