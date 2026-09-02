"""Contract for external secret stores used as broker backends (issue #4984).

Why this exists
===============

Operators running compliance-sensitive workflows already have a secret store,
already audit it, and already have policy attached to it. Copying secrets into
a second store to run Bernstein means governing two stores instead of one.

So the broker treats an operator's own store as a backend behind one contract:

* :meth:`ExternalSecretStore.resolve` -- resolve a named secret to *non-secret*
  facts about it (which store holds it, its store-native version or lease id,
  whether it is revoked).
* :meth:`ExternalSecretStore.mint_credential` -- mint a short-lived credential.
* :meth:`ExternalSecretStore.report_revocation` -- report whether the upstream
  secret or credential has been revoked.

What is recorded, and what is not
=================================

The credential *value* never enters the grant chain. What
:mod:`bernstein.core.identity.grants` records is the grant, the identity of the
store that issued the credential, the audience, and the expiry. Bernstein is
not a secret store that gained connectors; it is the authorization record that
can prove which task held which credential power over which window, whichever
store issued it.

Naming
======

A spec names a secret by opaque reference -- ``"<store>:<store-native path>"``
-- never by value. :class:`SecretRef` is that reference; the ``path`` half is
opaque to Bernstein and is interpreted only by the store that owns it.

No vendor SDKs in core
======================

``bernstein.core`` implements the contract and nothing else. A store that needs
a vendor SDK ships as a plugin registered through
:mod:`bernstein.core.security.secret_store_registry`; the SDK import lives in
the plugin, never here.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

__all__ = [
    "REFERENCE_SEPARATOR",
    "ExternalCredential",
    "ExternalSecretStore",
    "ExternalStoreError",
    "SecretDescriptor",
    "SecretRef",
]

#: Separates the store identity from the store-native path in a reference.
REFERENCE_SEPARATOR = ":"


class ExternalStoreError(Exception):
    """Raised when an external store cannot serve a contract call.

    The broker translates this into a
    :class:`~bernstein.core.security.secrets_broker.SecretsBrokerError` so
    callers see one error type regardless of which backend is configured.
    """


@dataclass(frozen=True)
class SecretRef:
    """Opaque reference naming one secret inside one external store.

    Attributes:
        store: Registered store identity (the registry name).
        path: Store-native name -- a Vault path, an ARN, a resource id.
            Bernstein never interprets it.
    """

    store: str
    path: str

    @classmethod
    def parse(cls, reference: str) -> SecretRef:
        """Parse ``"<store>:<path>"``.

        Raises:
            ExternalStoreError: When the reference names no store. Failing
                closed matters here: a bare name would otherwise be resolved
                against whichever store happened to be configured.
        """
        store, sep, path = reference.partition(REFERENCE_SEPARATOR)
        if not sep or not store or not path:
            raise ExternalStoreError(f"secret reference {reference!r} must be '<store>{REFERENCE_SEPARATOR}<path>'")
        return cls(store=store, path=path)

    def __str__(self) -> str:
        return f"{self.store}{REFERENCE_SEPARATOR}{self.path}"


@dataclass(frozen=True)
class SecretDescriptor:
    """Non-secret facts about a named secret. Never carries a value.

    Attributes:
        store_id: Identity of the store that holds the secret. This is what
            the grant chain records alongside the grant.
        upstream_id: Store-native version, lease, or generation id. Non-secret
            by construction; it is what a later revocation check refers to.
        revoked: Whether the store reports the secret as revoked.
        expires_at: Store-imposed expiry as epoch seconds; ``0`` means the
            store imposes none and the broker TTL governs.
    """

    store_id: str
    upstream_id: str = ""
    revoked: bool = False
    expires_at: float = 0.0


@dataclass(frozen=True, repr=False)
class ExternalCredential:
    """A short-lived credential a store minted for one task.

    ``repr`` is redacted so a credential that reaches a log line or a
    traceback does not carry its own value.

    Attributes:
        value: The credential. Held in the broker's in-process registry for
            the token's lifetime and never persisted or recorded.
        expires_at: Store-imposed expiry as epoch seconds; ``0`` means none.
        upstream_id: Store-native lease or version id for revocation checks.
        audience: The audience the store issued the credential for, when the
            store scopes credentials itself.
    """

    value: str
    expires_at: float = 0.0
    upstream_id: str = ""
    audience: str = ""

    def __repr__(self) -> str:
        return (
            f"ExternalCredential(value=<redacted len={len(self.value)}>, "
            f"expires_at={self.expires_at!r}, upstream_id={self.upstream_id!r}, "
            f"audience={self.audience!r})"
        )


class ExternalSecretStore(ABC):
    """One operator secret store, behind three verbs.

    Implementations live outside ``bernstein.core`` -- in an adapter or a
    plugin -- so a vendor SDK never becomes a core import. Every method may
    raise :class:`ExternalStoreError`; the broker records the refusal against
    the grant chain and raises its own error type.
    """

    #: Stable identity recorded in the grant chain beside the grant.
    store_id: str = ""

    @abstractmethod
    def resolve(self, path: str) -> SecretDescriptor:
        """Return non-secret facts about the secret at ``path``.

        Raises:
            ExternalStoreError: When the store holds no such secret.
        """

    @abstractmethod
    def mint_credential(self, path: str, *, audience: str, ttl_seconds: int) -> ExternalCredential:
        """Mint a short-lived credential for the secret at ``path``.

        Args:
            path: Store-native name.
            audience: The downstream target the credential is for; stores that
                scope credentials themselves should honour it.
            ttl_seconds: The lifetime the broker asked for. A store may return
                a shorter expiry; the broker caps the token to whichever is
                sooner. It must not return a longer one.

        Raises:
            ExternalStoreError: When the store refuses to mint.
        """

    @abstractmethod
    def report_revocation(self, path: str, *, upstream_id: str) -> bool:
        """Return ``True`` when the upstream secret or credential is revoked.

        Called before every mint, so revoking in the operator's own store is
        what stops Bernstein minting -- no separate revocation list to keep in
        step.
        """
