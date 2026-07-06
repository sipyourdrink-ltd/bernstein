"""CLI tests for ``bernstein governance verify <run>`` (#2309)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.governance_cmd import governance_group
from bernstein.core.cost.spend_ledger import CallTags, SpendLedger
from bernstein.core.security.audit import load_or_create_audit_key
from bernstein.core.security.governance import (
    RoleBindings,
    check_budget_decision,
    decide_access,
)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A workspace with an isolated, repo-local audit key."""
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    (tmp_path / ".sdd").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _bindings(key: bytes) -> RoleBindings:
    return RoleBindings(
        group_to_role={"eng": "operator", "readers": "viewer"},
        role_permissions={
            "operator": ("tasks:write", "costs:read"),
            "viewer": ("costs:read",),
        },
    ).sign(key)


def _write_bindings(project: Path, bindings: RoleBindings) -> Path:
    path = project / "bindings.json"
    path.write_text(json.dumps(bindings.to_dict()), encoding="utf-8")
    return path


def test_verify_ok_roundtrip(project: Path) -> None:
    key = load_or_create_audit_key(project / "audit.key")
    bindings = _bindings(key)
    lineage_root = project / ".sdd" / "lineage"
    decide_access(
        run_id="run-1",
        lineage_root=lineage_root,
        hmac_key=key,
        subject="alice",
        idp_groups=("eng",),
        action="tasks:write",
        bindings=bindings,
        now=1000,
    )
    bindings_path = _write_bindings(project, bindings)

    runner = CliRunner()
    result = runner.invoke(
        governance_group,
        ["verify", "run-1", "--bindings", str(bindings_path), "--workdir", str(project)],
    )
    assert result.exit_code == 0, result.output
    assert "OK" in result.output


def test_verify_includes_budget_and_reports_ledger(project: Path) -> None:
    key = load_or_create_audit_key(project / "audit.key")
    bindings = _bindings(key)
    lineage_root = project / ".sdd" / "lineage"
    ledger_path = project / ".sdd" / "cost" / "ledger.jsonl"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger = SpendLedger(path=ledger_path)
    ledger.record(tags=CallTags(agent_id="alice"), model="haiku", cost_usd=1.0)

    check_budget_decision(
        run_id="run-1",
        lineage_root=lineage_root,
        hmac_key=key,
        subject="alice",
        cap_usd=10.0,
        next_cost_usd=2.0,
        ledger_path=ledger_path,
        now=1000,
    )
    bindings_path = _write_bindings(project, bindings)

    runner = CliRunner()
    result = runner.invoke(
        governance_group,
        [
            "verify",
            "run-1",
            "--bindings",
            str(bindings_path),
            "--ledger",
            str(ledger_path),
            "--workdir",
            str(project),
        ],
    )
    assert result.exit_code == 0, result.output


def test_verify_no_records_exits_1(project: Path) -> None:
    key = load_or_create_audit_key(project / "audit.key")
    bindings_path = _write_bindings(project, _bindings(key))
    runner = CliRunner()
    result = runner.invoke(
        governance_group,
        ["verify", "run-absent", "--bindings", str(bindings_path), "--workdir", str(project)],
    )
    assert result.exit_code == 1
    assert "NO RECORDS" in result.output or "no" in result.output.lower()


def test_verify_tampered_exits_2(project: Path) -> None:
    key = load_or_create_audit_key(project / "audit.key")
    bindings = _bindings(key)
    lineage_root = project / ".sdd" / "lineage"
    decide_access(
        run_id="run-1",
        lineage_root=lineage_root,
        hmac_key=key,
        subject="alice",
        idp_groups=("eng",),
        action="tasks:write",
        bindings=bindings,
        now=1000,
    )
    from bernstein.core.security.governance import decisions_dir

    out_dir = decisions_dir(lineage_root, "run-1")
    (target,) = list(out_dir.glob("*.json"))
    raw = target.read_text(encoding="utf-8")
    target.write_text(raw.replace('"allow"', '"deny"'), encoding="utf-8")

    bindings_path = _write_bindings(project, bindings)
    runner = CliRunner()
    result = runner.invoke(
        governance_group,
        ["verify", "run-1", "--bindings", str(bindings_path), "--workdir", str(project)],
    )
    assert result.exit_code == 2
    assert "MISMATCH" in result.output
