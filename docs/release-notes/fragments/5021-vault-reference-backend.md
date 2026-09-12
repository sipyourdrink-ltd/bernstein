## Vault lease-backed credential store

Added :class:`~bernstein.adapters.vault_lease_backend.VaultLeaseBackend` — a
HashiCorp Vault-backed :class:`~bernstein.core.security.vault.protocol.CredentialVault`
implementation that issues short-lived dynamic leases for credentials. Secrets
are never stored locally; Vault issues ephemeral credentials that auto-expire,
reducing blast radius on compromise. Includes 6 acceptance tests covering lease
creation, renewal, expiry, revocation, and integration with
:py:class:`~bernstein.core.security.secrets_broker.SecretsBroker`. (#5021)
