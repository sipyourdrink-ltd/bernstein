"""Tests for agent-principal provisioning and deprovisioning (issue #4972).

A scoped per-task grant bounds one task's blast radius, but nothing used to
remove the *principal* the grants were issued to. These tests pin the
lifecycle: a principal is provisioned as a signed chain record carrying the
ceiling the directory implies, deprovisioning is a second chain record with
finality, and the grant-validity path in
:mod:`bernstein.core.identity.grants` consults that chain rather than an
out-of-band flag.

Deprovisioning is deliberately *not* retroactive: a grant that was active
before the line was drawn still verifies for the window it covered, while any
check performed at or after the deprovision timestamp refuses it.
"""

from __future__ import annotations

import json

import pytest

from bernstein.core.identity import grants, principals


@pytest.fixture
def signer() -> grants.GrantSigner:
    return grants.GrantSigner.generate(issuer="manager:test")


@pytest.fixture
def grant_ledger(tmp_path, signer) -> grants.GrantLedger:
    return grants.GrantLedger(root=tmp_path, key=b"k" * 32, signer=signer)


@pytest.fixture
def principal_ledger(tmp_path, signer) -> principals.PrincipalLedger:
    return principals.PrincipalLedger(root=tmp_path, key=b"k" * 32, signer=signer)


def _grant_chain(tmp_path, run_id: str = "run-1") -> grants.GrantChainResult:
    return grants.verify_grant_chain(root=tmp_path, run_id=run_id, key=b"k" * 32)


class TestProvisioning:
    def test_provisioning_creates_principal_in_registry_with_directory_ceiling(
        self, tmp_path, principal_ledger
    ) -> None:
        receipt = principal_ledger.provision(
            principal_id="agent:nightly-refactor",
            display_name="Nightly refactor agent",
            capability_ceiling=("read", "list"),
            external_id="dir-9911",
            created=1_000,
        )
        assert receipt.kind == principals.PRINCIPAL_PROVISIONED
        assert receipt.prev_hmac == principals.GENESIS_HMAC
        assert grants.verify_grant_signature(receipt.issuer_pubkey, receipt.signed_body(), receipt.signature)

        result = principals.verify_principal_chain(root=tmp_path, key=b"k" * 32)
        assert result.valid, result.errors
        registry = result.registry()
        state = registry["agent:nightly-refactor"]
        assert state.capability_ceiling == ("list", "read")
        assert state.external_id == "dir-9911"
        assert state.provisioned_at == 1_000
        assert state.deprovisioned_at == 0
        assert state.active_at(1_500)

    def test_tampered_deprovision_record_breaks_the_principal_chain(self, tmp_path, principal_ledger) -> None:
        principal_ledger.provision(principal_id="agent:a", capability_ceiling=("read",), created=1_000)
        principal_ledger.deprovision(principal_id="agent:a", created=2_000)
        path = principal_ledger.receipt_path()
        lines = path.read_text(encoding="utf-8").splitlines()
        obj = json.loads(lines[1])
        obj["kind"] = principals.PRINCIPAL_PROVISIONED
        lines[1] = json.dumps(obj, sort_keys=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = principals.verify_principal_chain(root=tmp_path, key=b"k" * 32)
        assert not result.valid
        assert any("record 1" in err for err in result.errors)


class TestDeprovisioningIsConsultedByGrantValidity:
    def test_deprovisioning_makes_every_later_grant_check_fail(self, tmp_path, grant_ledger, principal_ledger) -> None:
        principal_ledger.provision(principal_id="agent:a", capability_ceiling=("read",), created=1_000)
        grant_ledger.issue_grant(
            run_id="run-1",
            task_id="t-1",
            secret_name="K",
            audience="aud",
            expiry=9_000_000_000,
            principal="agent:a",
            created=1_100,
        )
        chain = _grant_chain(tmp_path)
        assert chain.valid, chain.errors
        assert grants.find_active_grant(chain, task_id="t-1", secret_name="K", now=1_200) is not None

        principal_ledger.deprovision(principal_id="agent:a", reason="run finished", created=2_000)
        after = _grant_chain(tmp_path)
        assert grants.find_active_grant(after, task_id="t-1", secret_name="K", now=2_000) is None
        assert grants.find_active_grant(after, task_id="t-1", secret_name="K", now=8_000) is None

    def test_grant_issued_before_deprovisioning_still_verifies_for_its_window(
        self, tmp_path, grant_ledger, principal_ledger
    ) -> None:
        principal_ledger.provision(principal_id="agent:a", capability_ceiling=("read",), created=1_000)
        issued = grant_ledger.issue_grant(
            run_id="run-1",
            task_id="t-1",
            secret_name="K",
            audience="aud",
            expiry=9_000_000_000,
            principal="agent:a",
            created=1_100,
        )
        principal_ledger.deprovision(principal_id="agent:a", created=2_000)

        chain = _grant_chain(tmp_path)
        # The record itself is untouched: deprovisioning draws a line, it does
        # not retroactively falsify the grant's own signature or linkage.
        assert chain.valid, chain.errors
        window = grants.find_active_grant(chain, task_id="t-1", secret_name="K", now=1_500)
        assert window is not None
        assert window.grant_id == issued.grant_id

    def test_replayed_grant_of_deprovisioned_principal_fails_against_now(
        self, tmp_path, grant_ledger, principal_ledger
    ) -> None:
        principal_ledger.provision(principal_id="agent:a", capability_ceiling=("read",), created=1_000)
        grant_ledger.issue_grant(
            run_id="run-1",
            task_id="t-1",
            secret_name="K",
            audience="aud",
            expiry=0,
            principal="agent:a",
            created=1_100,
        )
        principal_ledger.deprovision(principal_id="agent:a", created=2_000)
        chain = _grant_chain(tmp_path)
        # No expiry at all: only the deprovision fact can stop the replay.
        assert grants.find_active_grant(chain, task_id="t-1", secret_name="K", now=9_999_999) is None

    def test_missing_principal_chain_denies_a_principal_bound_grant(self, tmp_path, grant_ledger) -> None:
        grant_ledger.issue_grant(
            run_id="run-1",
            task_id="t-1",
            secret_name="K",
            audience="aud",
            expiry=9_000_000_000,
            principal="agent:ghost",
            created=1_100,
        )
        chain = _grant_chain(tmp_path)
        assert chain.valid, chain.errors
        # Deleting or never writing the principal chain must not resurrect the
        # grant: an unknown principal is refused, not waved through.
        assert grants.find_active_grant(chain, task_id="t-1", secret_name="K", now=1_200) is None

    def test_grant_without_a_principal_is_unaffected_by_the_principal_chain(
        self, tmp_path, grant_ledger, principal_ledger
    ) -> None:
        principal_ledger.provision(principal_id="agent:a", created=1_000)
        principal_ledger.deprovision(principal_id="agent:a", created=2_000)
        grant_ledger.issue_grant(
            run_id="run-1",
            task_id="t-1",
            secret_name="K",
            audience="aud",
            expiry=9_000_000_000,
            created=2_100,
        )
        chain = _grant_chain(tmp_path)
        assert grants.find_active_grant(chain, task_id="t-1", secret_name="K", now=3_000) is not None
