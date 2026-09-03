"""Unit tests for grant sweep functionality.

Tests the `bernstein.core.identity.grant_sweep.sweep_grants` function that
checks whether any revoked grants are still present in the active set.

The sweep is run on every reconcile execution to catch the drift where a
revoked grant record appears in the approved grant set, meaning the revocation
has not been properly enforced. Since both sets come from the same chain,
the sweep passes when the chain is valid and the revoked set is absent from
the approved set.

The key invariant: with valid chain data, revoked and approved sets are
disjoint by construction — the sweep catches chain corruption, not normal
operation. To test the failure path, we must create a synthetic result where
the same (task_id, secret_name) key appears in both sets.
"""

from __future__ import annotations

import json

import pytest

from bernstein.core.identity import grants
from bernstein.core.identity.grant_sweep import sweep_grants
from bernstein.core.identity.grants import GrantChainResult, GrantLedger, GrantSigner


@pytest.fixture
def signer() -> GrantSigner:
    return GrantSigner.generate(issuer="manager:test")


@pytest.fixture
def ledger(tmp_path, signer) -> GrantLedger:
    return GrantLedger(root=tmp_path, key=b"k" * 32, signer=signer)


class TestGrantSweep:
    def test_sweep_passes_when_no_revoked_grants_present(self, ledger) -> None:
        """A clean grant set with no revocations passes the sweep."""
        ledger.issue_grant(
            run_id="run-1",
            task_id="t-1",
            secret_name="K",
            audience="aud",
            expiry=0,
        )
        result = grants.verify_grant_chain(root=ledger.root, run_id="run-1", key=b"k" * 32)
        assert result.valid
        finding = sweep_grants(result)
        assert finding is None, f"Unexpected sweep finding: {finding}"

    def test_sweep_passes_for_properly_revoked_grant(self, ledger) -> None:
        """A grant that is properly revoked passes the sweep (revoked ∩ approved = ∅)."""
        g = ledger.issue_grant(
            run_id="run-1",
            task_id="t-1",
            secret_name="K",
            audience="aud",
            expiry=0,
        )
        ledger.revoke_grant(run_id="run-1", grant_id=g.grant_id, reason="test")
        result = grants.verify_grant_chain(root=ledger.root, run_id="run-1", key=b"k" * 32)
        assert result.valid
        # After revocation, the grant should be in revoked but NOT in approved
        revoked, approved = grants.compute_grant_sets(result)
        assert (g.task_id, g.secret_name) in revoked
        assert (g.task_id, g.secret_name) not in approved
        finding = sweep_grants(result)
        assert finding is None, f"Sweep should pass for properly revoked grant: {finding}"

    def test_sweep_finds_overlap_when_revoked_and_approved_share_key(self, ledger) -> None:
        """When the same (task_id, secret_name) appears in both sets, sweep fails.

        In a well-formed chain this cannot happen — revoked grants are excluded
        from the approved set by compute_grant_sets().  Overlap only arises when
        two *different* grant_ids share the same (task_id, secret_name): one
        lifecycle is revoked, another is not.  We simulate this by building a
        GrantChainResult with two records that carry the same salted reference.
        """
        from bernstein.core.identity.grants import GrantReceipt

        # Build two grants with the same (task_id, secret_name) but different
        # grant_ids — one revoked, one still active.
        test_signer = GrantSigner.generate(issuer="manager:test")
        shared_secret = "K"
        records = [
            GrantReceipt(
                run_id="run-1",
                record_index=0,
                kind=grants.GRANT_ISSUED,
                grant_id="grant-a",
                task_id="t-1",
                secret_name=shared_secret,
                audience="aud",
                expiry=0,
                capability_ceiling=(),
                issuer="manager:test",
                issuer_pubkey=test_signer.public_key_pem,
                created=1_000_000_000,
            ),
            GrantReceipt(
                run_id="run-1",
                record_index=1,
                kind=grants.GRANT_REVOKED,
                grant_id="grant-a",
                task_id="t-1",
                secret_name=shared_secret,
                audience="",
                expiry=0,
                capability_ceiling=(),
                issuer="manager:test",
                issuer_pubkey=test_signer.public_key_pem,
                created=1_000_000_001,
                reason="revoked",
            ),
            GrantReceipt(
                run_id="run-1",
                record_index=2,
                kind=grants.GRANT_ISSUED,
                grant_id="grant-b",
                task_id="t-1",
                secret_name=shared_secret,
                audience="aud",
                expiry=0,
                capability_ceiling=(),
                issuer="manager:test",
                issuer_pubkey=test_signer.public_key_pem,
                created=1_000_000_002,
            ),
        ]
        # grant-a is revoked; grant-b is issued and active. Both share
        # (task_id="t-1", secret_name="K").  compute_grant_sets will put the
        # key in both revoked and approved, creating an overlap.
        result = GrantChainResult(valid=True, records=records)
        revoked, approved = grants.compute_grant_sets(result)
        assert (result.records[0].task_id, result.records[0].secret_name) in revoked
        assert (result.records[0].task_id, result.records[0].secret_name) in approved
        finding = sweep_grants(result)
        assert finding is not None, "Sweep should detect overlap between revoked and approved"
        assert finding["severity"] == "critical"
        assert finding["category"] == "grant-sweep"
        assert "t-1" in finding["summary"]
        assert "K" in finding["summary"]

    def test_sweep_ignores_invalid_chain(self, ledger) -> None:
        """An invalid grant chain is not examined by the sweep (silent no-op)."""
        ledger.issue_grant(
            run_id="run-1",
            task_id="t-1",
            secret_name="K",
            audience="aud",
            expiry=0,
        )
        # Tamper the record by adding a field
        path = ledger.receipt_path("run-1")
        raw = path.read_text(encoding="utf-8").splitlines()
        obj = json.loads(raw[0])
        obj["tampered_field"] = "BAD"
        path.write_text(json.dumps(obj, sort_keys=True) + "\n", encoding="utf-8")
        result = grants.verify_grant_chain(root=ledger.root, run_id="run-1", key=b"k" * 32)
        assert not result.valid
        finding = sweep_grants(result)
        assert finding is None, f"Sweep should not run on invalid chain: {finding}"

    def test_sweep_detects_multiple_revoked_grants_present(self, ledger) -> None:
        """Multiple revoked grants that overlap with approved all trigger the finding."""
        # Issue two grants for different tasks, revoke one, then issue a second
        # grant for the SAME task/secret as the revoked one to create overlap.
        g1 = ledger.issue_grant(
            run_id="run-1",
            task_id="t-1",
            secret_name="K",
            audience="aud",
            expiry=0,
        )
        g2 = ledger.issue_grant(
            run_id="run-1",
            task_id="t-2",
            secret_name="L",
            audience="aud",
            expiry=0,
        )
        ledger.revoke_grant(run_id="run-1", grant_id=g1.grant_id, reason="test1")
        # Re-issue a NEW grant for the same (t-1, K) — different grant_id but
        # same task_id.  With different salts the secret_name differs, so we
        # construct the synthetic overlap directly.
        result = grants.verify_grant_chain(root=ledger.root, run_id="run-1", key=b"k" * 32)
        assert result.valid

        revoked, approved = grants.compute_grant_sets(result)
        # In a valid chain, g1 is in revoked but not approved
        assert (g1.task_id, g1.secret_name) in revoked
        assert (g1.task_id, g1.secret_name) not in approved
        # g2 is in approved
        assert (g2.task_id, g2.secret_name) in approved
        assert (g2.task_id, g2.secret_name) not in revoked

        # The intersection should be empty for valid chain
        finding = sweep_grants(result)
        assert finding is None, f"Sweep should pass for valid chain with multiple revocations: {finding}"

        # Now test the failure path: simulate a second grant for (t-1, K) with
        # the SAME salted reference (out-of-band re-add).
        from bernstein.core.identity.grants import GrantReceipt, GrantSigner

        test_signer = GrantSigner.generate(issuer="manager:test")
        records = [
            *result.records,
            GrantReceipt(
                run_id="run-1",
                record_index=len(result.records),
                kind=grants.GRANT_ISSUED,
                grant_id="grant-c",
                task_id=g1.task_id,
                secret_name=g1.secret_name,  # same salted reference
                audience="aud",
                expiry=0,
                capability_ceiling=(),
                issuer="manager:test",
                issuer_pubkey=test_signer.public_key_pem,
                created=int(result.records[-1].created) + 1,
            ),
        ]
        corrupted = GrantChainResult(valid=True, records=records)
        revoked2, approved2 = grants.compute_grant_sets(corrupted)
        assert (g1.task_id, g1.secret_name) in revoked2
        assert (g1.task_id, g1.secret_name) in approved2
        finding2 = sweep_grants(corrupted)
        assert finding2 is not None
        assert "t-1" in finding2["summary"]
        assert g1.secret_name in finding2["summary"]
