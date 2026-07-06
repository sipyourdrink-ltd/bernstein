"""Tests for ``bernstein identity keydir`` and ``bernstein delegation verify``.

Covers issue #2305 CLI surface plus AC4 (delegation verify reconstructs the
principal->orchestrator->sub-agent chain for a run).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.delegation_cmd import delegation_group
from bernstein.cli.commands.identity_cmd import identity_group
from bernstein.core.identity import delegation, http_signing
from bernstein.core.security.agent_card_keystore import AgentCardKeystore


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


class TestIdentityKeydir:
    def test_keydir_prints_install_identity_jwks(self, runner, tmp_path, monkeypatch):
        monkeypatch.setenv(http_signing.ENV_KEY_DIR, str(tmp_path / "keys"))
        result = runner.invoke(identity_group, ["keydir"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["keys"]
        jwk = payload["keys"][0]
        assert jwk["crv"] == "Ed25519"
        _priv, pub = AgentCardKeystore(tmp_path / "keys").load_or_generate()
        assert jwk["kid"] == http_signing.install_identity_keyid(pub)


class TestDelegationVerify:
    def _seed_chain(self, root: Path, key: bytes) -> None:
        ledger = delegation.DelegationLedger(root=root, key=key)
        ledger.record_hop(
            run_id="run-42",
            issuer="principal:alex",
            subject="orchestrator",
            audience="orchestrator",
            act="run.authorize",
        )
        ledger.record_hop(
            run_id="run-42",
            issuer="orchestrator",
            subject="orchestrator",
            audience="sub-agent:backend",
            act="task.spawn",
        )

    def test_verify_ok_for_intact_chain(self, runner, tmp_path, monkeypatch):
        key = b"k" * 32
        monkeypatch.setattr(delegation, "_audit_key", lambda: key)
        self._seed_chain(tmp_path, key)
        result = runner.invoke(delegation_group, ["verify", "run-42", "--root", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "principal:alex" in result.output
        assert "sub-agent:backend" in result.output

    def test_verify_json_output(self, runner, tmp_path, monkeypatch):
        key = b"k" * 32
        monkeypatch.setattr(delegation, "_audit_key", lambda: key)
        self._seed_chain(tmp_path, key)
        result = runner.invoke(delegation_group, ["verify", "run-42", "--root", str(tmp_path), "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["valid"] is True
        assert payload["hops"] == 2
        assert [h["issuer"] for h in payload["receipts"]] == ["principal:alex", "orchestrator"]

    def test_verify_nonzero_on_tamper(self, runner, tmp_path, monkeypatch):
        key = b"k" * 32
        monkeypatch.setattr(delegation, "_audit_key", lambda: key)
        self._seed_chain(tmp_path, key)
        path = tmp_path / "delegation" / "run-42.jsonl"
        path.write_text(
            path.read_text(encoding="utf-8").replace("principal:alex", "principal:mallory"),
            encoding="utf-8",
        )
        result = runner.invoke(delegation_group, ["verify", "run-42", "--root", str(tmp_path)])
        assert result.exit_code == 1

    def test_verify_nonzero_on_missing_run(self, runner, tmp_path, monkeypatch):
        monkeypatch.setattr(delegation, "_audit_key", lambda: b"k" * 32)
        result = runner.invoke(delegation_group, ["verify", "absent", "--root", str(tmp_path)])
        assert result.exit_code == 1
