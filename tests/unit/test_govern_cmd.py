"""CLI tests for ``bernstein govern verify <run>`` (#2309) after rename to ``bernstein govern``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.governance_cmd import govern_group
from bernstein.core.cost.spend_ledger import CallTags, SpendLedger
from bernstein.core.identity import http_signing
from bernstein.core.security.agent_card_keystore import AgentCardKeystore
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
        govern_group,
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
        govern_group,
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
        govern_group,
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
        govern_group,
        ["verify", "run-1", "--bindings", str(bindings_path), "--workdir", str(project)],
    )
    assert result.exit_code == 2
    assert "MISMATCH" in result.output


def _verifier_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return an isolated home directory for verifier files."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


@pytest.mark.parametrize("filename", ["local.json", "server.json"])
def test_audit_no_verifier_files_exit_0(filename: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No verifier files means nothing to measure -> exit 0."""
    _verifier_home(tmp_path, monkeypatch)
    key_dir = tmp_path / "keys"
    AgentCardKeystore(key_dir).load_or_generate()
    monkeypatch.setenv(http_signing.ENV_KEY_DIR, str(key_dir))

    runner = CliRunner()
    result = runner.invoke(govern_group, ["audit"])
    assert result.exit_code == 0, result.output
    assert "up to date" in result.output


@pytest.mark.parametrize("filename", ["local.json", "server.json"])
def test_audit_current_keyid_exit_0(filename: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A verifier file containing the current keyid is up to date -> exit 0."""
    home = _verifier_home(tmp_path, monkeypatch)
    key_dir = tmp_path / "keys"
    _priv, pub = AgentCardKeystore(key_dir).load_or_generate()
    current_keyid = http_signing.install_identity_keyid(pub)
    monkeypatch.setenv(http_signing.ENV_KEY_DIR, str(key_dir))

    verifier_dir = home / ".config" / "bernstein" / "verifier"
    verifier_dir.mkdir(parents=True)
    verifier_dir.joinpath(filename).write_text(json.dumps({"keys": [{"kid": current_keyid}]}))

    runner = CliRunner()
    result = runner.invoke(govern_group, ["audit"])
    assert result.exit_code == 0, result.output
    assert "up to date" in result.output


@pytest.mark.parametrize("filename", ["local.json", "server.json"])
def test_audit_stale_keyid_exit_2(filename: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A verifier exported from one keystore is stale after rotation to another -> exit 2."""
    home = _verifier_home(tmp_path, monkeypatch)
    first_key_dir = tmp_path / "keys1"
    _priv1, pub1 = AgentCardKeystore(first_key_dir).load_or_generate()
    first_keyid = http_signing.install_identity_keyid(pub1)

    verifier_dir = home / ".config" / "bernstein" / "verifier"
    verifier_dir.mkdir(parents=True)
    verifier_dir.joinpath(filename).write_text(json.dumps({"keys": [{"kid": first_keyid}]}))

    second_key_dir = tmp_path / "keys2"
    AgentCardKeystore(second_key_dir).load_or_generate()
    monkeypatch.setenv(http_signing.ENV_KEY_DIR, str(second_key_dir))

    runner = CliRunner()
    result = runner.invoke(govern_group, ["audit"])
    assert result.exit_code == 2, result.output
    assert "stale" in result.output.lower() or "predates" in result.output.lower()


@pytest.mark.parametrize("filename", ["local.json", "server.json"])
def test_audit_unreadable_verifier_exit_1(filename: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unreadable verifier file is not measurable -> exit 1."""
    home = _verifier_home(tmp_path, monkeypatch)
    key_dir = tmp_path / "keys"
    AgentCardKeystore(key_dir).load_or_generate()
    monkeypatch.setenv(http_signing.ENV_KEY_DIR, str(key_dir))

    verifier_dir = home / ".config" / "bernstein" / "verifier"
    verifier_dir.mkdir(parents=True)
    verifier_dir.joinpath(filename).write_text("not valid json")

    runner = CliRunner()
    result = runner.invoke(govern_group, ["audit"])
    assert result.exit_code == 1, result.output
    assert "unreadable" in result.output.lower()
