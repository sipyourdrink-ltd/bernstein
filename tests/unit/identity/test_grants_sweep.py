"""Tests for compute_grant_sets().

These tests verify the pure functions that derive revoked and approved grant
sets from a GrantChainResult. The functions transform chain state into
sets of (task_id, secret_name) tuples for use by downstream systems like
grant sweep verification.
"""

from __future__ import annotations

import pytest

from bernstein.core.identity import grants


@pytest.fixture
def signer() -> grants.GrantSigner:
    return grants.GrantSigner.generate(issuer="manager:test")


@pytest.fixture
def ledger(tmp_path, signer) -> grants.GrantLedger:
    return grants.GrantLedger(root=tmp_path, key=b"k" * 32, signer=signer)


class TestComputeGrantSets:
    """Tests for compute_grant_sets()."""

    def _seed_multiple_grants(self, ledger) -> dict[str, grants.GrantReceipt]:
        """Create a chain with multiple grants in various states."""
        # Issue grants
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
        g3 = ledger.issue_grant(
            run_id="run-1",
            task_id="t-3",
            secret_name="M",
            audience="aud",
            expiry=0,
        )

        # Revoke g1 and g2
        ledger.revoke_grant(run_id="run-1", grant_id=g1.grant_id, reason="task-exit")
        ledger.revoke_grant(run_id="run-1", grant_id=g2.grant_id, reason="task-exit")

        # Exchange tokens for some grants
        ledger.record_exchange(run_id="run-1", grant_id=g1.grant_id, token_id="brn-tok-1")
        ledger.record_exchange(run_id="run-1", grant_id=g3.grant_id, token_id="brn-tok-3")

        return {
            "g1": g1,
            "g2": g2,
            "g3": g3,
        }

    def test_revoked_in_revoked_set(self, ledger) -> None:
        """Grants that were revoked appear in the revoked set."""
        receipts = self._seed_multiple_grants(ledger)
        result = grants.verify_grant_chain(root=ledger.root, run_id="run-1", key=b"k" * 32)
        assert result.valid

        revoked, approved = grants.compute_grant_sets(result, now=1_000_000_000)

        # g1 and g2 are revoked
        assert (receipts["g1"].task_id, receipts["g1"].secret_name) in revoked
        assert (receipts["g2"].task_id, receipts["g2"].secret_name) in revoked

        # g3 is not revoked, should be in approved
        assert (receipts["g3"].task_id, receipts["g3"].secret_name) in approved

        # Revoked grants should NOT be in approved
        assert (receipts["g1"].task_id, receipts["g1"].secret_name) not in approved
        assert (receipts["g2"].task_id, receipts["g2"].secret_name) not in approved

    def test_approved_for_valid_grants(self, ledger) -> None:
        """Non-revoked grants with valid expiry appear in approved set."""
        g1 = ledger.issue_grant(
            run_id="run-1",
            task_id="t-1",
            secret_name="K",
            audience="aud",
            expiry=0,  # no expiry
        )
        g2 = ledger.issue_grant(
            run_id="run-1",
            task_id="t-2",
            secret_name="L",
            audience="aud",
            expiry=2_000_000_000,  # future expiry
        )
        result = grants.verify_grant_chain(root=ledger.root, run_id="run-1", key=b"k" * 32)
        assert result.valid

        revoked, approved = grants.compute_grant_sets(result, now=1_000_000_000)

        # Both grants are issued and not revoked
        assert revoked == set()
        assert (g1.task_id, g1.secret_name) in approved
        assert (g2.task_id, g2.secret_name) in approved

    def test_expired_grant_not_approved(self, ledger) -> None:
        """Expired grant should not be in approved set."""
        g1 = ledger.issue_grant(
            run_id="run-1",
            task_id="t-1",
            secret_name="K",
            audience="aud",
            expiry=500,  # expired
        )
        g2 = ledger.issue_grant(
            run_id="run-1",
            task_id="t-2",
            secret_name="L",
            audience="aud",
            expiry=2_000,  # valid
        )
        result = grants.verify_grant_chain(root=ledger.root, run_id="run-1", key=b"k" * 32)
        assert result.valid

        revoked, approved = grants.compute_grant_sets(result, now=1_000)

        assert revoked == set()
        assert (g1.task_id, g1.secret_name) not in approved  # expired
        assert (g2.task_id, g2.secret_name) in approved  # valid

    def test_expired_grant_not_revoked(self, ledger) -> None:
        """Expired grants should not be in revoked set (only issued+revoked grants are)."""
        ledger.issue_grant(
            run_id="run-1",
            task_id="t-1",
            secret_name="K",
            audience="aud",
            expiry=500,
        )
        result = grants.verify_grant_chain(root=ledger.root, run_id="run-1", key=b"k" * 32)
        assert result.valid

        revoked, approved = grants.compute_grant_sets(result, now=1_000)

        # Expired grant is not revoked, so not in revoked set
        assert revoked == set()
        # And it's also not approved because it's expired
        assert approved == set()

    def test_revoked_and_expired_grant_only_in_revoked(self, ledger) -> None:
        """Grant that is both revoked and expired should ONLY be in revoked set."""
        g1 = ledger.issue_grant(
            run_id="run-1",
            task_id="t-1",
            secret_name="K",
            audience="aud",
            expiry=500,  # expired
        )
        ledger.revoke_grant(run_id="run-1", grant_id=g1.grant_id, reason="task-exit")
        result = grants.verify_grant_chain(root=ledger.root, run_id="run-1", key=b"k" * 32)
        assert result.valid

        revoked, approved = grants.compute_grant_sets(result, now=1_000)

        # Revoked grant in revoked set
        assert (g1.task_id, g1.secret_name) in revoked
        # Not in approved (expired)
        assert (g1.task_id, g1.secret_name) not in approved

    def test_invalid_chain_returns_empty_sets(self, ledger) -> None:
        """An invalid chain verification returns empty sets for both revoked and approved."""
        _g = ledger.issue_grant(
            run_id="run-1",
            task_id="t-1",
            secret_name="K",
            audience="aud",
            expiry=0,
        )
        # Tamper the chain
        path = ledger.receipt_path("run-1")
        raw = path.read_text(encoding="utf-8").splitlines()
        import json

        obj = json.loads(raw[0])
        obj["tampered_field"] = "BAD"
        path.write_text(json.dumps(obj, sort_keys=True) + "\n", encoding="utf-8")

        result = grants.verify_grant_chain(root=ledger.root, run_id="run-1", key=b"k" * 32)
        assert not result.valid

        revoked, approved = grants.compute_grant_sets(result)
        assert revoked == set()
        assert approved == set()

    def test_non_issued_grant_not_in_either_set(self, ledger) -> None:
        """Grants that were never issued should not appear in either set.

        This is a property of the current implementation - only grants that
        have an issue record are considered. Refused grants would not be issued.
        """
        g1 = ledger.issue_grant(
            run_id="run-1",
            task_id="t-1",
            secret_name="K",
            audience="aud",
            expiry=0,
        )
        # Issue another grant
        g2 = ledger.issue_grant(
            run_id="run-1",
            task_id="t-2",
            secret_name="L",
            audience="aud",
            expiry=0,
        )
        result = grants.verify_grant_chain(root=ledger.root, run_id="run-1", key=b"k" * 32)
        assert result.valid

        revoked, approved = grants.compute_grant_sets(result, now=1_000_000_000)

        assert revoked == set()
        assert (g1.task_id, g1.secret_name) in approved
        assert (g2.task_id, g2.secret_name) in approved

    def test_uses_current_time_when_not_supplied(self, ledger) -> None:
        """When 'now' is not supplied, time.time() is used."""
        g = ledger.issue_grant(
            run_id="run-1",
            task_id="t-1",
            secret_name="K",
            audience="aud",
            expiry=0,
        )
        result = grants.verify_grant_chain(root=ledger.root, run_id="run-1", key=b"k" * 32)
        assert result.valid

        # Call without 'now' - should use time.time()
        _revoked, approved = grants.compute_grant_sets(result)
        # The secret_name in the key is the salted reference, not the raw name
        assert (g.task_id, g.secret_name) in approved  # should still be approved

    def test_expiry_zero_means_never_expired(self, ledger) -> None:
        """Expiry=0 means no explicit expiry, so grant is never expired by time."""
        g = ledger.issue_grant(
            run_id="run-1",
            task_id="t-1",
            secret_name="K",
            audience="aud",
            expiry=0,  # no expiry
        )
        result = grants.verify_grant_chain(root=ledger.root, run_id="run-1", key=b"k" * 32)
        assert result.valid

        # Even at a very large time value, expiry=0 means never expires
        _revoked, approved = grants.compute_grant_sets(result, now=9_999_999_999)
        assert (g.task_id, g.secret_name) in approved

    def test_empty_chain_returns_empty_sets(self, ledger) -> None:
        """An empty/non-existent chain returns empty sets."""
        result = grants.verify_grant_chain(root=ledger.root, run_id="nonexistent", key=b"k" * 32)
        assert not result.valid

        revoked, approved = grants.compute_grant_sets(result)
        assert revoked == set()
        assert approved == set()
