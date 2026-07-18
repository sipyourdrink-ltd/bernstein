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
    seal_run_journal,
)
from bernstein.core.tasks.models import TaskCostEstimate, TaskPlan

_RUN_ID = "run-intent-cli"
_TASK_ID = "task-cli-1"

#: Far-future expiry: a real journal's events carry a wall-clock ``ts`` and the
#: capsule expiry is enforced, so a past expiry would read as drift.
_FUTURE_EXPIRY = 4_102_444_800  # 2100-01-01T00:00:00Z


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
        expiry_ts=_FUTURE_EXPIRY,
    )
    journal = EventJournal(_RUN_ID, sdd)
    bind_capsule_into_journal(journal, task_id=_TASK_ID, capsule_hash=capsule_hash(cap))
    journal.record("tool.call", tool="Read", adapter="claude", seq=1)
    journal.record("tool.call", tool="Edit", adapter="claude", path="src/pricing/rates.py", seq=2)
    if drift:
        journal.record("tool.call", tool="WebFetch", adapter="claude", seq=3)
    # Offline verify requires the journal head and length to be committed to the
    # chain, otherwise a truncated prefix would verify as a clean run (#2649).
    seal_run_journal(chain=chain, sdd_dir=sdd, task_id=_TASK_ID, run_id=_RUN_ID, capsule=cap)


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
    from bernstein.core.security.audit import load_or_create_audit_key

    # A machine that has an audit key but no capsule for the task.
    load_or_create_audit_key()
    runner = CliRunner()
    res = runner.invoke(intent_group, ["verify", "no-such-task", "-w", str(project)])
    assert res.exit_code == 1, res.output


def test_verify_without_audit_key_does_not_mint_one(project: Path, tmp_path: Path) -> None:
    """A read-only verify must never create key material, nor cry tamper (#2649).

    Minting a fresh key would make the chain unverifiable against its real key
    and surface as a bogus integrity failure, so the command has to fail loudly
    on the missing key instead.
    """
    _approve(project, drift=False)
    key_path = tmp_path / "audit.key"
    assert key_path.exists()
    key_path.unlink()

    runner = CliRunner()
    res = runner.invoke(intent_group, ["verify", _TASK_ID, "-w", str(project)])

    assert not key_path.exists(), "read-only verify minted audit key material"
    assert res.exit_code == 3, res.output
    assert "MISMATCH" not in res.output
    assert "DRIFT" not in res.output
    assert "CANNOT VERIFY" in res.output


def test_verify_without_audit_key_reports_missing_key_in_json(project: Path, tmp_path: Path) -> None:
    _approve(project, drift=False)
    (tmp_path / "audit.key").unlink()

    runner = CliRunner()
    res = runner.invoke(intent_group, ["verify", _TASK_ID, "-w", str(project), "--json"])

    assert res.exit_code == 3, res.output
    assert not (tmp_path / "audit.key").exists()


def test_load_audit_key_refuses_to_create(tmp_path: Path) -> None:
    from bernstein.core.security.audit import AuditKeyMissingError, load_audit_key

    missing = tmp_path / "nowhere" / "audit.key"
    with pytest.raises(AuditKeyMissingError):
        load_audit_key(missing)
    assert not missing.exists()
    assert not missing.parent.exists()


def test_load_audit_key_reads_an_existing_key(tmp_path: Path) -> None:
    from bernstein.core.security.audit import load_audit_key, load_or_create_audit_key

    key_path = tmp_path / "audit.key"
    created = load_or_create_audit_key(key_path)
    assert load_audit_key(key_path) == created
