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
from bernstein.core.identity.delegation_scope import DecisionBinding, DelegationScope
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

    def test_verify_reports_an_unscoped_chain_as_unproven(self, runner, tmp_path, monkeypatch):
        """These receipts record no scope, so narrowing was never checked.

        This chain exited 0 before #2554's graded verdict, which read the same
        as a chain whose narrowing was checked and held. It now exits 3.
        """
        key = b"k" * 32
        monkeypatch.setattr(delegation, "_audit_key", lambda: key)
        self._seed_chain(tmp_path, key)
        result = runner.invoke(delegation_group, ["verify", "run-42", "--root", str(tmp_path)])
        assert result.exit_code == 3, result.output
        assert "unproven" in result.output
        assert "principal:alex" in result.output
        assert "sub-agent:backend" in result.output

    def test_verify_json_output(self, runner, tmp_path, monkeypatch):
        key = b"k" * 32
        monkeypatch.setattr(delegation, "_audit_key", lambda: key)
        self._seed_chain(tmp_path, key)
        result = runner.invoke(delegation_group, ["verify", "run-42", "--root", str(tmp_path), "--json"])
        assert result.exit_code == 3, result.output
        payload = json.loads(result.output)
        assert payload["valid"] is True
        assert payload["hops"] == 2
        assert [h["issuer"] for h in payload["receipts"]] == ["principal:alex", "orchestrator"]
        assert payload["verdict"]["verdict"] == "unproven"
        assert payload["verdict"]["unproven_hops"] == 2
        assert "no_scope_recorded" in payload["verdict"]["reasons"]

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


class TestDelegationVerifyNarrowing:
    """``delegation verify`` recomputes narrowing and duty separation (#2554)."""

    def _seed_widening_chain(self, root: Path, key: bytes) -> None:
        ledger = delegation.DelegationLedger(root=root, key=key)
        ledger.record_hop(
            run_id="run-widen",
            issuer="principal:alex",
            subject="orchestrator",
            audience="orchestrator",
            act="run.authorize",
            scope=DelegationScope(permissions=frozenset({"files.read"}), max_depth=2),
        )
        ledger.record_hop(
            run_id="run-widen",
            issuer="orchestrator",
            subject="sub-agent:backend",
            audience="sub-agent:backend",
            act="task.delegate",
            scope=DelegationScope(permissions=frozenset({"files.read", "files.write"}), max_depth=1),
        )

    def test_verify_nonzero_on_widening_hop(self, runner, tmp_path, monkeypatch):
        key = b"k" * 32
        monkeypatch.setattr(delegation, "_audit_key", lambda: key)
        self._seed_widening_chain(tmp_path, key)
        result = runner.invoke(delegation_group, ["verify", "run-widen", "--root", str(tmp_path)])
        assert result.exit_code == 1
        assert "permissions" in result.output

    def test_verify_json_reports_the_offending_axis(self, runner, tmp_path, monkeypatch):
        key = b"k" * 32
        monkeypatch.setattr(delegation, "_audit_key", lambda: key)
        self._seed_widening_chain(tmp_path, key)
        result = runner.invoke(delegation_group, ["verify", "run-widen", "--root", str(tmp_path), "--json"])
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["chain_ok"] is True
        assert payload["valid"] is False
        assert payload["authority"]["narrowing_ok"] is False
        assert payload["authority"]["scope_coverage"] == "full"
        axes = {v["axis"] for v in payload["authority"]["violations"]}
        assert "permissions" in axes
        assert payload["receipts"][1]["scope_ref"].startswith("sha256:")

    def test_verify_reports_the_certificate_binding_per_hop(self, runner, tmp_path, monkeypatch):
        key = b"k" * 32
        monkeypatch.setattr(delegation, "_audit_key", lambda: key)
        ledger = delegation.DelegationLedger(root=tmp_path, key=key)
        binding = DecisionBinding(
            charter_hash="sha256:charter",
            certificate_hash="sha256:cert",
            certificate_version="7",
        )
        for issuer, subject, act in (
            ("principal:alex", "orchestrator", "run.authorize"),
            ("orchestrator", "reviewer:9", "gate.approve"),
        ):
            ledger.record_hop(
                run_id="run-bound",
                issuer=issuer,
                subject=subject,
                audience=subject,
                act=act,
                binding=binding,
            )

        result = runner.invoke(delegation_group, ["verify", "run-bound", "--root", str(tmp_path), "--json"])

        # Bound, but still scopeless: binding_ok says nothing about narrowing.
        assert result.exit_code == 3, result.output
        payload = json.loads(result.output)
        assert all(r["binding"]["certificate_hash"] == "sha256:cert" for r in payload["receipts"])
        assert payload["authority"]["binding_ok"] is True
        assert payload["verdict"]["verdict"] == "unproven"

    def test_verify_nonzero_when_one_principal_holds_separated_duties(self, runner, tmp_path, monkeypatch):
        key = b"k" * 32
        monkeypatch.setattr(delegation, "_audit_key", lambda: key)
        ledger = delegation.DelegationLedger(root=tmp_path, key=key)
        ledger.record_hop(
            run_id="run-sod",
            issuer="principal:alex",
            subject="worker:7",
            audience="sub-agent:backend",
            act="task.spawn",
        )
        ledger.record_hop(
            run_id="run-sod",
            issuer="principal:alex",
            subject="worker:7",
            audience="gate:merge",
            act="gate.approve",
        )

        result = runner.invoke(delegation_group, ["verify", "run-sod", "--root", str(tmp_path), "--json"])

        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["chain_ok"] is True
        assert payload["authority"]["duties_ok"] is False
        assert payload["authority"]["narrowing_ok"] is True


class TestDelegationVerifyExitCodes:
    """0 pass, 1 fail, 3 unproven (#2554). 2 stays click's usage-error code."""

    def _seed(self, root: Path, key: bytes, run: str, scopes) -> None:
        ledger = delegation.DelegationLedger(root=root, key=key)
        for i, scope in enumerate(scopes):
            ledger.record_hop(
                run_id=run,
                issuer=f"p{i}",
                subject=f"p{i}",
                audience=f"p{i + 1}",
                act="task.delegate",
                scope=scope,
            )

    @pytest.fixture(autouse=True)
    def _key(self, monkeypatch):
        monkeypatch.setattr(delegation, "_audit_key", lambda: b"k" * 32)

    def test_a_checked_and_held_chain_exits_zero(self, runner, tmp_path):
        wide = DelegationScope(permissions=frozenset({"files.read", "files.write"}), max_depth=3)
        narrow = DelegationScope(permissions=frozenset({"files.read"}), max_depth=2)
        self._seed(tmp_path, b"k" * 32, "run-pass", [wide, narrow])
        result = runner.invoke(delegation_group, ["verify", "run-pass", "--root", str(tmp_path)])
        assert result.exit_code == 0, result.output

    def test_a_widening_chain_exits_one(self, runner, tmp_path):
        narrow = DelegationScope(permissions=frozenset({"files.read"}), max_depth=2)
        wide = DelegationScope(permissions=frozenset({"files.read", "files.write"}), max_depth=1)
        self._seed(tmp_path, b"k" * 32, "run-fail", [narrow, wide])
        result = runner.invoke(delegation_group, ["verify", "run-fail", "--root", str(tmp_path)])
        assert result.exit_code == 1, result.output

    def test_a_chain_with_nothing_to_check_exits_three(self, runner, tmp_path):
        self._seed(tmp_path, b"k" * 32, "run-unproven", [None, None])
        result = runner.invoke(delegation_group, ["verify", "run-unproven", "--root", str(tmp_path)])
        assert result.exit_code == 3, result.output

    def test_the_unproven_code_is_not_the_click_usage_code(self, runner, tmp_path):
        usage = runner.invoke(delegation_group, ["verify"])
        assert usage.exit_code == 2
        self._seed(tmp_path, b"k" * 32, "run-u2", [None])
        unproven = runner.invoke(delegation_group, ["verify", "run-u2", "--root", str(tmp_path)])
        assert unproven.exit_code == 3, unproven.output
