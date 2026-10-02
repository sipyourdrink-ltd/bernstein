"""Live integration test against a real Vault dev server.

Skipped unless ``BERNSTEIN_VAULT_LIVE_TEST_ADDR`` and
``BERNSTEIN_VAULT_LIVE_TEST_TOKEN`` point at a running Vault (e.g.
``hashicorp/vault`` in ``-dev`` mode) with a token role named
``bernstein-agent`` already created:

    vault write auth/token/roles/bernstein-agent \\
        allowed_policies=default orphan=true renewable=true \\
        explicit_max_ttl=600s

Not run in CI by default: it is here so this plugin's claims are
checked against a real server at least once, and so anyone who doubts
the fake-transport unit tests can reproduce that check themselves.
"""

from __future__ import annotations

import os

import pytest
from custom_vault_token_store import VaultHttpTransport, VaultTokenRoleStore

ADDR = os.environ.get("BERNSTEIN_VAULT_LIVE_TEST_ADDR", "")
TOKEN = os.environ.get("BERNSTEIN_VAULT_LIVE_TEST_TOKEN", "")

pytestmark = pytest.mark.skipif(
    not ADDR or not TOKEN,
    reason="set BERNSTEIN_VAULT_LIVE_TEST_ADDR/_TOKEN to run against a real Vault dev server",
)


def test_mint_resolve_and_revoke_round_trip() -> None:
    store = VaultTokenRoleStore(transport=VaultHttpTransport(addr=ADDR, token=TOKEN))

    descriptor = store.resolve("bernstein-agent")
    assert descriptor.store_id == "vault"
    assert descriptor.revoked is False

    credential = store.mint_credential("bernstein-agent", audience="live-test", ttl_seconds=120)
    assert credential.value.startswith("hvs.") or credential.value.startswith("s.")
    assert credential.upstream_id

    # A live Vault token, in use: lookup-self against the minted client
    # token proves it is a real, usable Vault credential, not just a
    # well-shaped string.
    self_lookup = VaultHttpTransport(addr=ADDR, token=credential.value)("GET", "auth/token/lookup-self")
    assert self_lookup is not None
    assert self_lookup["data"]["display_name"].endswith("live-test")

    assert store.report_revocation("bernstein-agent", upstream_id=credential.upstream_id) is False

    # Revoke by accessor (what an operator would do to kill a compromised
    # task's credential) and confirm the store reports it as revoked.
    admin = VaultHttpTransport(addr=ADDR, token=TOKEN)
    admin("POST", "auth/token/revoke-accessor", {"accessor": credential.upstream_id})
    assert store.report_revocation("bernstein-agent", upstream_id=credential.upstream_id) is True
