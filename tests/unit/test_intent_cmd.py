"""CLI tests for ``bernstein intent show|verify`` (#2514).

``verify`` recomputes the conformance verdict offline from the run journal and
the approved intent capsule: it walks the journal Merkle chain, checks the
capsule hash against the audit chain, and maps observed action classes against
the capsule. A clean run verifies; a drifted run reports the divergence; a
tampered capsule or reordered journal fails.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.intent_cmd import intent_group
from bernstein.core.replay.journal import EventJournal
from bernstein.core.security.audit_chain import AuditChainStore
from bernstein.core.security.intent_capsule import (
    approve_and_capsule,
    bind_capsule_into_journal,
    capsule_hash,
)
from bernstein.core.tasks.models import TaskCostEstimate, TaskPlan

_RUN_ID = "run-intent-cli"
_TASK_ID = "task-cli-1"


def _plan() -> TaskPlan:
    return TaskPlan(
        id="plancli",
        goal="Tidy the config loader.",
        task_estimates=[TaskCostEstimate(task_id=_TASK_ID, title="Tidy", role="backend", estimated_tokens=40_000)],
        total_estimated_cost_usd=0.1,
    )


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    (tmp_path / ".sdd").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _approve(project: Path, *, drift: bool) -> None:
    from bernstein.core.security.audit import load_or_create_audit_key

    sdd = project / ".sdd"
    key = load_or_create_audit_key()
    chain = AuditChainStore(sdd / "audit", key=key)
    cap, _ = approve_and_capsule(
        chain=chain,
        sdd_dir=sdd,
        plan=_plan(),
        task_id=_TASK_ID,
        run_id=_RUN_ID,
        allowed_action_classes=["fs.read", "fs.write", "git.commit"],
        file_scope_globs=["**"],
        permitted_adapters=["claude"],
        egress_classes=[],
        expiry_ts=1_700_100_000,
    )
    journal = EventJournal(_RUN_ID, sdd)
    bind_capsule_into_journal(journal, task_id=_TASK_ID, capsule_hash=capsule_hash(cap))
    journal.record("tool.call", tool="Read", seq=1)
    journal.record("tool.call", tool="Edit", seq=2)
    if drift:
        journal.record("tool.call", tool="WebFetch", seq=3)


def test_show_prints_capsule_projection(project: Path) -> None:
    _approve(project, drift=False)
    runner = CliRunner()
    res = runner.invoke(intent_group, ["show", _TASK_ID, "-w", str(project)])
    assert res.exit_code == 0, res.output
    assert "allowed_action_classes" in res.output
    assert "fs.read" in res.output
    # The free-text goal is never printed - only its digest.
    assert "Tidy the config loader" not in res.output


def test_verify_ok_on_clean_run(project: Path) -> None:
    _approve(project, drift=False)
    runner = CliRunner()
    res = runner.invoke(intent_group, ["verify", _TASK_ID, "-w", str(project)])
    assert res.exit_code == 0, res.output
    assert "OK" in res.output


def test_verify_reports_drift(project: Path) -> None:
    _approve(project, drift=True)
    runner = CliRunner()
    res = runner.invoke(intent_group, ["verify", _TASK_ID, "-w", str(project)])
    assert res.exit_code == 2, res.output
    assert "DRIFT" in res.output or "web.fetch" in res.output


def test_verify_missing_capsule(project: Path) -> None:
    runner = CliRunner()
    res = runner.invoke(intent_group, ["verify", "no-such-task", "-w", str(project)])
    assert res.exit_code == 1, res.output
