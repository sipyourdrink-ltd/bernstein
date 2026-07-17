"""Tests for ``bernstein secrets grants list|verify`` (issue #2516, Phase 3).

The grants surface reconstructs a run's full issue / exchange / revoke history
offline from the chain alone, following the pattern of ``bernstein delegation
verify``. A tampered, deleted, or reordered record flips ``verify`` to a
non-zero exit naming the failing record, and two ``--json`` runs over the same
chain slice produce byte-identical reports.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.secrets_cmd import secrets_group
from bernstein.core.identity import grants


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def _pin_audit_key(monkeypatch):
    # Pin the install audit key so the CLI reconstructs with a known key.
    monkeypatch.setattr(grants, "_audit_key", lambda: b"k" * 32)


def _seed(root, *, revoke: bool = True) -> grants.GrantReceipt:
    signer = grants.GrantSigner.generate(issuer="manager:test")
    ledger = grants.GrantLedger(root=root, key=b"k" * 32, signer=signer)
    g = ledger.issue_grant(
        run_id="run-1",
        task_id="t-1",
        secret_name="K",
        audience="api.anthropic.com",
        expiry=2_000_000_000,
        capability_ceiling=("read",),
    )
    ledger.record_exchange(run_id="run-1", grant_id=g.grant_id, token_id="brn-tok-1")
    if revoke:
        ledger.revoke_grant(run_id="run-1", grant_id=g.grant_id, reason="task-exit")
    return g


class TestGrantsVerify:
    def test_verify_ok_for_intact_chain(self, runner, tmp_path) -> None:
        _seed(tmp_path)
        result = runner.invoke(secrets_group, ["grants", "verify", "run-1", "--root", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "grant_issued" in result.output

    def test_verify_json_is_byte_identical(self, runner, tmp_path) -> None:
        _seed(tmp_path)
        r1 = runner.invoke(secrets_group, ["grants", "verify", "run-1", "--root", str(tmp_path), "--json"])
        r2 = runner.invoke(secrets_group, ["grants", "verify", "run-1", "--root", str(tmp_path), "--json"])
        assert r1.exit_code == 0, r1.output
        assert r1.output == r2.output
        payload = json.loads(r1.output)
        assert payload["run"] == "run-1"
        assert payload["valid"] is True

    def test_verify_tampered_expiry_exits_nonzero_naming_record(self, runner, tmp_path) -> None:
        _seed(tmp_path)
        # Tamper the persisted expiry.
        signer = grants.GrantSigner.generate(issuer="x")
        path = grants.GrantLedger(root=tmp_path, key=b"k" * 32, signer=signer).receipt_path("run-1")
        raw = path.read_text(encoding="utf-8")
        path.write_text(raw.replace("2000000000", "9999999999", 1), encoding="utf-8")
        result = runner.invoke(secrets_group, ["grants", "verify", "run-1", "--root", str(tmp_path)])
        assert result.exit_code == 1
        assert "record 0" in result.output or "index 0" in result.output

    def test_verify_missing_run_exits_nonzero(self, runner, tmp_path) -> None:
        result = runner.invoke(secrets_group, ["grants", "verify", "absent", "--root", str(tmp_path)])
        assert result.exit_code == 1


class TestGrantsList:
    def test_list_shows_lifecycle(self, runner, tmp_path) -> None:
        _seed(tmp_path)
        result = runner.invoke(secrets_group, ["grants", "list", "run-1", "--root", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "t-1" in result.output
        assert "revoked" in result.output.lower()

    def test_list_json(self, runner, tmp_path) -> None:
        _seed(tmp_path, revoke=False)
        result = runner.invoke(secrets_group, ["grants", "list", "run-1", "--root", str(tmp_path), "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["run"] == "run-1"
        assert payload["grants"]
        assert payload["grants"][0]["task_id"] == "t-1"
