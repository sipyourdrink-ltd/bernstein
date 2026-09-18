"""``bernstein audit verify`` includes credential grant chains (issue #2516).

A tampered grant record makes ``bernstein audit verify`` fail with the run and
record named, exactly like a tampered chain entry. When no grant chains exist
the check is a silent no-op that does not affect the exit code.

A cryptographically intact chain is not on its own a clean verdict: the grant
sweep can still report that a revoked grant is present in the approved set, and
a verdict that ignores that certifies a failing grant state as passing.
"""

from __future__ import annotations

import pytest

from bernstein.cli.commands import audit_cmd
from bernstein.cli.helpers import console
from bernstein.core.identity import grant_sweep, grants


@pytest.fixture(autouse=True)
def _pin_audit_key(monkeypatch):
    # The audit-verify path loads the install audit key directly (load-only,
    # never minting one -- #4210), so pin it.
    monkeypatch.setattr("bernstein.core.security.audit.load_audit_key", lambda *a, **k: b"k" * 32)


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


def test_verify_grant_chains_fails_when_the_sweep_reports_a_finding(tmp_path, monkeypatch) -> None:
    """A valid chain plus a critical sweep finding is a failure, not a pass.

    The chain signatures and the HMAC linkage all verify here - the only thing
    wrong is what the sweep found - so this is the case a verdict keyed on
    chain validity alone gets wrong. The command's return value is what
    operators and automation branch on, so it is asserted alongside the panel.
    """
    monkeypatch.setattr(audit_cmd, "AUDIT_DIR", tmp_path)
    _seed_grants(tmp_path)

    summary = "revoked grant K is still present in the approved set"
    monkeypatch.setattr(
        grant_sweep,
        "sweep_grants",
        lambda result, **kwargs: {"severity": "critical", "category": "grant-sweep", "summary": summary},
    )

    with console.capture() as captured:
        verdict = audit_cmd._verify_grant_chains()
    output = captured.get()

    assert verdict is False
    assert "Grant Chain Verification Passed" not in output
    assert "Grant Chain Verification FAILED" in output
    assert summary in output


def test_verify_grant_chains_still_passes_when_the_sweep_is_clean(tmp_path, monkeypatch) -> None:
    """The sweep returning nothing must stay a pass, or the check is useless.

    Guards the obvious over-correction: failing whenever the sweep runs at all.
    """
    monkeypatch.setattr(audit_cmd, "AUDIT_DIR", tmp_path)
    _seed_grants(tmp_path)
    monkeypatch.setattr(grant_sweep, "sweep_grants", lambda result, **kwargs: None)

    with console.capture() as captured:
        verdict = audit_cmd._verify_grant_chains()

    assert verdict is True
    assert "Grant Chain Verification Passed" in captured.get()
