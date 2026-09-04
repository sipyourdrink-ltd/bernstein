"""Tests for the directory-provisioning adapter (issue #4972).

The adapter maps the standard SCIM 2.0 provisioning resources onto the
Bernstein principal objects: a ``User`` resource becomes a principal, its
group memberships become the capability ceiling, and ``active: false`` (or an
explicit delete) becomes a deprovision record. No vendor SDK and no third
schema: ``bernstein.core`` never learns what a directory is.
"""

from __future__ import annotations

import pytest

from bernstein.adapters.directory import scim
from bernstein.core.identity import grants, principals


@pytest.fixture
def ledger(tmp_path) -> principals.PrincipalLedger:
    signer = grants.GrantSigner.generate(issuer="manager:test")
    return principals.PrincipalLedger(root=tmp_path, key=b"k" * 32, signer=signer)


USER = {
    "schemas": [scim.USER_SCHEMA],
    "id": "2819c223-7f76-453a-919d-413861904646",
    "externalId": "dir-9911",
    "userName": "agent:nightly-refactor",
    "displayName": "Nightly refactor agent",
    "active": True,
    "groups": [
        {"value": "g-2", "display": "read"},
        {"value": "g-1", "display": "list"},
    ],
}


class TestSchemaMapping:
    def test_directory_user_groups_become_the_capability_ceiling(self) -> None:
        principal = scim.principal_from_user(USER)
        assert principal.principal_id == "agent:nightly-refactor"
        assert principal.external_id == "dir-9911"
        assert principal.display_name == "Nightly refactor agent"
        assert principal.active is True
        # Sorted and de-duplicated so two directories that list the same
        # groups in a different order imply the same ceiling.
        assert principal.capability_ceiling == ("list", "read")

    def test_directory_group_resource_maps_to_capability_and_members(self) -> None:
        group = {
            "schemas": [scim.GROUP_SCHEMA],
            "id": "g-1",
            "displayName": "list",
            "members": [
                {"value": "2819c223", "display": "agent:nightly-refactor"},
                {"value": "9f2b", "display": "agent:release-notes"},
            ],
        }
        assert scim.capability_from_group(group) == "list"
        assert scim.principals_in_group(group) == ("agent:nightly-refactor", "agent:release-notes")

    def test_user_resource_without_an_identifier_is_refused(self) -> None:
        with pytest.raises(scim.DirectorySchemaError):
            scim.principal_from_user({"schemas": [scim.USER_SCHEMA], "active": True})


class TestLifecycle:
    def test_directory_deactivation_deprovisions_the_principal(self, tmp_path, ledger) -> None:
        scim.apply_user(ledger, USER, created=1_000)
        deactivated = dict(USER, active=False)
        receipt = scim.apply_user(ledger, deactivated, created=2_000)
        assert receipt.kind == principals.PRINCIPAL_DEPROVISIONED

        result = principals.verify_principal_chain(root=tmp_path, key=b"k" * 32)
        assert result.valid, result.errors
        state = result.registry()["agent:nightly-refactor"]
        assert state.deprovisioned_at == 2_000
        assert not state.active_at(2_000)

    def test_apply_user_provisions_with_the_ceiling_the_directory_implies(self, tmp_path, ledger) -> None:
        receipt = scim.apply_user(ledger, USER, created=1_000)
        assert receipt.kind == principals.PRINCIPAL_PROVISIONED
        result = principals.verify_principal_chain(root=tmp_path, key=b"k" * 32)
        assert result.registry()["agent:nightly-refactor"].capability_ceiling == ("list", "read")
