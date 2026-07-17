"""``bernstein audit verify`` includes credential grant chains (issue #2516).

A tampered grant record makes ``bernstein audit verify`` fail with the run and
record named, exactly like a tampered chain entry. When no grant chains exist
the check is a silent no-op that does not affect the exit code.
"""

from __future__ import annotations

import pytest

from bernstein.cli.commands import audit_cmd
from bernstein.core.identity import grants


@pytest.fixture(autouse=True)
def _pin_audit_key(monkeypatch):
    # The audit-verify path loads the install audit key directly, so pin it.
    monkeypatch.setattr("bernstein.core.security.audit.load_or_create_audit_key", lambda *a, **k: b"k" * 32)


def _seed_grants(audit_dir, *, tamper: bool = False) -> None:
    signer = grants.GrantSigner.generate(issuer="manager:test")
    ledger = grants.GrantLedger(root=audit_dir, key=b"k" * 32, signer=signer)
    g = ledger.issue_grant(run_id="run-1", task_id="t-1", secret_name="K", audience="aud", expiry=2_000_000_000)
    ledger.record_exchange(run_id="run-1", grant_id=g.grant_id, token_id="brn-tok-1")
    if tamper:
        path = ledger.receipt_path("run-1")
        raw = path.read_text(encoding="utf-8")
        path.write_text(raw.replace("aud", "attacker", 1), encoding="utf-8")


def test_verify_grant_chains_passes_for_intact(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(audit_cmd, "AUDIT_DIR", tmp_path)
    _seed_grants(tmp_path)
    assert audit_cmd._verify_grant_chains() is True


def test_verify_grant_chains_fails_for_tampered(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(audit_cmd, "AUDIT_DIR", tmp_path)
    _seed_grants(tmp_path, tamper=True)
    assert audit_cmd._verify_grant_chains() is False


def test_verify_grant_chains_noop_when_absent(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(audit_cmd, "AUDIT_DIR", tmp_path)
    assert audit_cmd._verify_grant_chains() is True
