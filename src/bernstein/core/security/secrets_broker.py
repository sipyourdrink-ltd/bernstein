"""Secrets broker: short-lived per-task tokens, no dotfile-in-workspace.

Operator pain solved
====================

Today secrets reach agents via process env vars or dotfiles inside the
workspace. Both surfaces can leak into transcripts, logs, and persisted
state. This module replaces them with a broker that mints short-lived
per-task tokens. The agent process receives only the minted token; the
raw backing secret never appears in the spawned environment, and the
token auto-revokes on task exit.

Lifecycle
=========

::

    broker = build_broker_from_config(cfg)
    token = broker.mint(secret_name="ANTHROPIC_API_KEY", task_id="t-42",
                        ttl_seconds=900)
    # ...spawn agent with env={"ANTHROPIC_API_KEY": token.value}...
    broker.revoke(token.token_id)        # explicit revoke
    # or context manager
    with broker.mint_scoped(secret_name="...", task_id="...") as token:
        ...                              # auto-revoke at scope exit

Token model
===========

A minted token is an opaque random string plus a TTL. The broker keeps an
in-process registry mapping ``token_id -> (secret_name, raw_value,
expires_at, task_id)``. Lookups go through :meth:`SecretsBroker.resolve`
which honours expiry. The minted token value is what the agent process
sees in its env; the broker's own resolver translates it back to the raw
backing secret for adapter calls that need the underlying credential.

Backends
========

Six backends ship in this module: ``vault``, ``aws_secretsmanager``,
``gcp_secret_manager``, ``macos_keychain``, ``linux_keyring``,
``file_encrypted``. All backends implement a single thin API:
``read(secret_name) -> raw_value``. Network backends are imported lazily so
the module loads without optional dependencies installed.

A seventh backend, ``external``, is the operator's own secret store behind
:class:`~bernstein.core.security.external_secret_store.ExternalSecretStore`.
Instead of copying secrets into a store of ours, the broker resolves a named
secret in theirs, asks it to mint a short-lived credential, and refuses to
mint again once that store reports the secret revoked. The credential value
never enters the audit chain: what the chain records is the grant, the
identity of the store that issued the credential, the audience, and the
expiry. Concrete stores are plugins registered through
:mod:`~bernstein.core.security.secret_store_registry`, so a vendor SDK import
never lands in ``bernstein.core``.

Audit log
=========

Every mint and revoke emits a structured event via the module logger and,
when wired, an optional :class:`AuditSink` callback. The sink is
intentionally pluggable so the lineage subsystem or any other audit store
can subscribe without this module importing it.

Redactor coupling
=================

When a token is minted, both the token id and the raw backing value are
registered with :func:`register_secret_for_redaction`. The redactor module
consults that registry when scrubbing agent transcripts so minted values
do not survive into persisted artefacts.
"""

from __future__ import annotations

import json
import logging
import os
import secrets as _secrets
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Generator, MutableMapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from bernstein.core.security.external_secret_store import (
    ExternalCredential,
    ExternalStoreError,
    SecretDescriptor,
    SecretRef,
)

if TYPE_CHECKING:
    from bernstein.core.security.external_secret_store import ExternalSecretStore

logger = logging.getLogger(__name__)

__all__ = [
    "AuditEvent",
    "AuditSink",
    "AwsSecretsManagerBackend",
    "BrokerConfig",
    "ExternalStoreBackend",
    "FileEncryptedBackend",
    "GcpSecretManagerBackend",
    "LinuxKeyringBackend",
    "MacosKeychainBackend",
    "MintedToken",
    "SecretsBackend",
    "SecretsBroker",
    "SecretsBrokerError",
    "VaultBackend",
    "build_broker_from_config",
    "clear_redaction_registry",
    "get_redactable_values",
    "register_secret_for_redaction",
    "unregister_secret_for_redaction",
]

BackendName = Literal[
    "external",
    "vault",
    "aws_secretsmanager",
    "gcp_secret_manager",
    "macos_keychain",
    "linux_keyring",
    "file_encrypted",
]

_DEFAULT_TTL_SECONDS = 900
_TOKEN_PREFIX = "brn-"


class SecretsBrokerError(Exception):
    """Raised when a broker operation fails."""


# ---------------------------------------------------------------------------
# Redaction registry
# ---------------------------------------------------------------------------

_redaction_lock = threading.Lock()
_redaction_values: set[str] = set()


def register_secret_for_redaction(value: str) -> None:
    """Add *value* to the set of strings the redactor will scrub.

    Short or empty values are ignored to avoid pathological matches.
    """
    if not value or len(value) < 8:
        return
    with _redaction_lock:
        _redaction_values.add(value)


def unregister_secret_for_redaction(value: str) -> None:
    """Remove *value* from the redaction registry."""
    with _redaction_lock:
        _redaction_values.discard(value)


def get_redactable_values() -> frozenset[str]:
    """Return a snapshot of currently registered redactable values."""
    with _redaction_lock:
        return frozenset(_redaction_values)


def clear_redaction_registry() -> None:
    """Drop every registered value. Test-only convenience."""
    with _redaction_lock:
        _redaction_values.clear()


# ---------------------------------------------------------------------------
# Audit event + sink
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditEvent:
    """Structured audit record for a broker operation."""

    kind: Literal["mint", "revoke", "resolve", "expire"]
    token_id: str
    secret_name: str
    task_id: str
    ts_ns: int
    ttl_seconds: int = 0
    reason: str = ""


AuditSink = Callable[[AuditEvent], None]


# ---------------------------------------------------------------------------
# Minted token
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MintedToken:
    """Result of a successful :meth:`SecretsBroker.mint` call.

    ``version_id`` is empty for an ordinary mint. A rotation run sets it so the
    token, the stored secret version, and the receipt left on the target all
    name the same version (see
    :mod:`bernstein.core.security.secret_rotation`).
    """

    token_id: str
    value: str
    secret_name: str
    task_id: str
    expires_at: float
    ttl_seconds: int
    audience: str = ""
    version_id: str = ""

    def is_expired(self, *, now: float | None = None) -> bool:
        """Return ``True`` when wall-clock time has passed ``expires_at``."""
        current = now if now is not None else time.time()
        return current >= self.expires_at


# ---------------------------------------------------------------------------
# Backend protocol + implementations
# ---------------------------------------------------------------------------


class SecretsBackend(ABC):
    """Thin read-only interface every backend implements."""

    name: str = ""

    @abstractmethod
    def read(self, secret_name: str) -> str:
        """Return the raw secret value for ``secret_name``.

        Raises:
            SecretsBrokerError: When the backend cannot resolve the name.
        """

    def list_names(self) -> list[str]:
        """Return secret names visible to this backend.

        Backends that cannot enumerate return an empty list; the broker
        treats this as "not supported" without erroring.
        """
        return []


class VaultBackend(SecretsBackend):
    """HashiCorp Vault KV v2 backend.

    Reads ``VAULT_ADDR`` / ``VAULT_TOKEN`` from env. The KV path is
    ``{mount}/{secret_name}``; for a single-mount setup callers can pass
    ``secret_name="my-key"`` and the mount defaults to ``secret``.
    """

    name = "vault"

    def __init__(self, *, mount: str = "secret") -> None:
        self._mount = mount
        self._addr = os.environ.get("VAULT_ADDR", "http://127.0.0.1:8200")
        self._token = os.environ.get("VAULT_TOKEN", "")

    def read(self, secret_name: str) -> str:
        import urllib.error
        import urllib.request

        url = f"{self._addr}/v1/{self._mount}/data/{secret_name}"
        req = urllib.request.Request(url)
        req.add_header("X-Vault-Token", self._token)
        try:
            # VAULT_ADDR is operator-controlled and validated at config time.
            # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
            with urllib.request.urlopen(req, timeout=10) as resp:
                body: object = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            raise SecretsBrokerError(f"vault HTTP {exc.code}: {exc.reason}") from exc
        except Exception as exc:  # pragma: no cover - network paths
            raise SecretsBrokerError(f"vault read failed for {secret_name!r}: {exc}") from exc

        if not isinstance(body, dict):
            raise SecretsBrokerError(f"vault secret {secret_name!r} returned non-object payload")
        outer: object = body.get("data", {})
        if not isinstance(outer, dict):
            raise SecretsBrokerError(f"vault secret {secret_name!r} has unexpected envelope")
        data: object = outer.get("data", {})
        if not isinstance(data, dict):
            raise SecretsBrokerError(f"vault secret {secret_name!r} is not a KV map")
        # Convention: prefer a ``value`` field, otherwise the single field.
        if "value" in data:
            return str(data["value"])
        if len(data) == 1:
            only = next(iter(data.values()))
            return str(only)
        raise SecretsBrokerError(f"vault secret {secret_name!r} has multiple fields; expected a 'value' field")


class AwsSecretsManagerBackend(SecretsBackend):
    """AWS Secrets Manager backend (boto3)."""

    name = "aws_secretsmanager"

    def read(self, secret_name: str) -> str:
        try:
            import boto3  # type: ignore[import-untyped]
        except ImportError as exc:
            raise SecretsBrokerError("boto3 is required for aws_secretsmanager backend") from exc
        try:
            client = boto3.client("secretsmanager")  # type: ignore[reportUnknownMemberType]
            response = client.get_secret_value(SecretId=secret_name)  # type: ignore[reportUnknownMemberType]
        except Exception as exc:  # pragma: no cover - network paths
            raise SecretsBrokerError(f"aws read failed for {secret_name!r}: {exc}") from exc
        if "SecretString" in response:
            raw: object = response["SecretString"]
            if not isinstance(raw, str):
                raise SecretsBrokerError(f"aws secret {secret_name!r} SecretString is not a string")
            try:
                parsed: object = json.loads(raw)
            except json.JSONDecodeError:
                return raw
            if isinstance(parsed, dict) and "value" in parsed:
                return str(parsed["value"])
            if isinstance(parsed, str):
                return parsed
            return raw
        raise SecretsBrokerError(f"aws secret {secret_name!r} has no SecretString")


class GcpSecretManagerBackend(SecretsBackend):
    """GCP Secret Manager backend.

    ``secret_name`` is expected to be the bare secret id; the project is
    read from ``GOOGLE_CLOUD_PROJECT`` and the version defaults to
    ``latest``.
    """

    name = "gcp_secret_manager"

    def __init__(self, *, project: str | None = None, version: str = "latest") -> None:
        self._project = project or os.environ.get("GOOGLE_CLOUD_PROJECT", "")
        self._version = version

    def read(self, secret_name: str) -> str:
        if not self._project:
            raise SecretsBrokerError("GOOGLE_CLOUD_PROJECT is required for gcp_secret_manager backend")
        try:
            from google.cloud import secretmanager  # type: ignore[import-not-found]
        except ImportError as exc:
            raise SecretsBrokerError("google-cloud-secret-manager is required for gcp_secret_manager backend") from exc
        try:
            client = secretmanager.SecretManagerServiceClient()  # type: ignore[reportUnknownMemberType]
            name = f"projects/{self._project}/secrets/{secret_name}/versions/{self._version}"
            response = client.access_secret_version(request={"name": name})  # type: ignore[reportUnknownMemberType]
        except Exception as exc:  # pragma: no cover - network paths
            raise SecretsBrokerError(f"gcp read failed for {secret_name!r}: {exc}") from exc
        payload: object = response.payload.data  # type: ignore[reportUnknownMemberType]
        if isinstance(payload, (bytes, bytearray)):
            return bytes(payload).decode("utf-8")
        return str(payload)


class MacosKeychainBackend(SecretsBackend):
    """macOS Keychain backend via the ``security`` CLI."""

    name = "macos_keychain"

    def __init__(self, *, service: str = "bernstein") -> None:
        self._service = service

    def read(self, secret_name: str) -> str:
        try:
            result = subprocess.run(
                [
                    "security",
                    "find-generic-password",
                    "-s",
                    self._service,
                    "-a",
                    secret_name,
                    "-w",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
        except FileNotFoundError as exc:
            raise SecretsBrokerError("macOS 'security' CLI not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise SecretsBrokerError("macOS keychain lookup timed out") from exc
        if result.returncode != 0:
            raise SecretsBrokerError(
                f"keychain read failed for {secret_name!r}: {result.stderr.strip() or 'unknown error'}"
            )
        return result.stdout.rstrip("\n")


class LinuxKeyringBackend(SecretsBackend):
    """Linux keyring backend via the ``keyring`` Python package.

    The ``keyring`` package brokers between freedesktop Secret Service,
    KWallet, and other backends; using it keeps this code distro-agnostic.
    """

    name = "linux_keyring"

    def __init__(self, *, service: str = "bernstein") -> None:
        self._service = service

    def read(self, secret_name: str) -> str:
        try:
            import keyring  # type: ignore[import-not-found]
        except ImportError as exc:
            raise SecretsBrokerError("'keyring' package is required for linux_keyring backend") from exc
        try:
            value = keyring.get_password(self._service, secret_name)  # type: ignore[reportUnknownMemberType]
        except Exception as exc:
            raise SecretsBrokerError(f"keyring lookup failed for {secret_name!r}: {exc}") from exc
        if value is None:
            raise SecretsBrokerError(f"keyring has no entry for {secret_name!r}")
        return str(value)


class FileEncryptedBackend(SecretsBackend):
    """Encrypted JSON file backend.

    Format: a JSON object mapping secret name to value, encrypted with
    Fernet (symmetric AES-128-CBC + HMAC-SHA256) using a 32-byte key read
    from ``BERNSTEIN_BROKER_KEY`` (urlsafe base64) or the path in the
    ``key_path`` arg. This is the zero-dependency fallback backend for
    operators who cannot run Vault or a cloud secret store; ``cryptography``
    is the only optional import.
    """

    name = "file_encrypted"

    def __init__(self, *, path: str, key_path: str | None = None) -> None:
        self._path = path
        self._key_path = key_path

    def _load_key(self) -> bytes:
        env_key = os.environ.get("BERNSTEIN_BROKER_KEY", "")
        if env_key:
            return env_key.encode("utf-8")
        if self._key_path:
            try:
                with open(self._key_path, "rb") as fp:
                    return fp.read().strip()
            except OSError as exc:
                raise SecretsBrokerError(f"cannot read broker key file: {exc}") from exc
        raise SecretsBrokerError("file_encrypted backend needs BERNSTEIN_BROKER_KEY env or key_path config")

    def _read_all(self) -> dict[str, str]:
        try:
            from cryptography.fernet import Fernet, InvalidToken  # type: ignore[import-not-found]
        except ImportError as exc:
            raise SecretsBrokerError("'cryptography' is required for file_encrypted backend") from exc
        try:
            ciphertext = Path(self._path).read_bytes()
        except OSError as exc:
            raise SecretsBrokerError(f"cannot read secrets file {self._path!r}: {exc}") from exc
        key = self._load_key()
        try:
            plaintext = Fernet(key).decrypt(ciphertext)
        except InvalidToken as exc:
            raise SecretsBrokerError("file_encrypted: decryption failed (wrong key?)") from exc
        try:
            parsed: object = json.loads(plaintext.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SecretsBrokerError("file_encrypted: secrets payload is not valid JSON") from exc
        if not isinstance(parsed, dict):
            raise SecretsBrokerError("file_encrypted: top-level payload must be a JSON object")
        result: dict[str, str] = {}
        for k, v in parsed.items():
            result[str(k)] = str(v)
        return result

    def read(self, secret_name: str) -> str:
        store = self._read_all()
        if secret_name not in store:
            raise SecretsBrokerError(f"file_encrypted has no entry for {secret_name!r}")
        return store[secret_name]

    def list_names(self) -> list[str]:
        try:
            return sorted(self._read_all().keys())
        except SecretsBrokerError:
            return []


class ExternalStoreBackend(SecretsBackend):
    """The operator's own secret store, behind the external-store contract.

    Configure as ``backend: external`` with
    ``backend_settings: {store_name: "<registered name>"}``; any further
    settings are forwarded to the store's factory. Tests and in-process
    wiring may pass an already-constructed ``store`` instead.

    Store construction is deferred to first use, so a name that no plugin
    registered fails at mint time with a message naming what *is* registered,
    rather than minting against some other store or failing at import.

    ``secret_name`` here is an opaque reference -- ``"<store>:<path>"`` -- and
    the ``path`` half is interpreted only by the store that owns it.
    """

    name = "external"

    def __init__(
        self,
        *,
        store: ExternalSecretStore | None = None,
        store_name: str = "",
        **store_settings: Any,
    ) -> None:
        self._store = store
        self._store_name = store_name or (str(getattr(store, "store_id", "")) if store is not None else "")
        self._store_settings = store_settings

    @property
    def store_name(self) -> str:
        """Name this backend answers for, and the store half of a reference."""
        return self._store_name

    def store(self) -> ExternalSecretStore:
        """Return the store, constructing it from the registry on first use."""
        if self._store is None:
            from bernstein.core.security.secret_store_registry import get_registry

            try:
                self._store = get_registry().create(self._store_name, **self._store_settings)
            except KeyError as exc:
                raise SecretsBrokerError(str(exc)) from exc
        return self._store

    def _path(self, secret_name: str) -> str:
        """Split ``secret_name`` into its store-native path, checking the store."""
        ref = SecretRef.parse(secret_name)
        if self._store_name and ref.store != self._store_name:
            raise SecretsBrokerError(
                f"secret reference {secret_name!r} names store {ref.store!r}, "
                f"but this backend serves {self._store_name!r}"
            )
        return ref.path

    def describe(self, secret_name: str) -> SecretDescriptor:
        """Return the store's non-secret facts about ``secret_name``."""
        return self.store().resolve(self._path(secret_name))

    def report_revocation(self, secret_name: str, *, upstream_id: str) -> bool:
        """Ask the store whether the upstream secret or credential is revoked."""
        return self.store().report_revocation(self._path(secret_name), upstream_id=upstream_id)

    def issue(self, secret_name: str, *, audience: str, ttl_seconds: int) -> ExternalCredential:
        """Ask the store to mint a short-lived credential."""
        return self.store().mint_credential(self._path(secret_name), audience=audience, ttl_seconds=ttl_seconds)

    def read(self, secret_name: str) -> str:
        """Return a credential value for callers that only hold the thin API.

        The broker itself goes through :meth:`describe` / :meth:`issue` so the
        revocation check and the store-reported expiry are honoured; this
        method exists because every backend implements ``read``.
        """
        return self.issue(secret_name, audience="", ttl_seconds=_DEFAULT_TTL_SECONDS).value


# ---------------------------------------------------------------------------
# Broker config
# ---------------------------------------------------------------------------


#: Valid identity modes for grant issuance. ``ed25519`` is the default,
#: self-contained manager identity; ``spiffe`` relabels the grant issuer to the
#: workload's SPIFFE ID when the ``spiffe`` extra and a Workload API socket are
#: available (issue #2516).
IdentityMode = Literal["ed25519", "spiffe"]
_IDENTITY_MODES: frozenset[str] = frozenset({"ed25519", "spiffe"})


@dataclass(frozen=True)
class BrokerConfig:
    """Runtime configuration for the broker, mirrored from bernstein.yaml.

    Attributes:
        backend: Which backend to use.
        ttl_seconds_default: Default token lifetime in seconds.
        ttl_overrides: Per-secret-name override map.
        backend_settings: Free-form options forwarded to the backend ctor.
        require_grant: When True the broker refuses to mint without a
            verifiable, chain-anchored grant (issue #2516).
        identity_mode: ``ed25519`` (default) or ``spiffe``; ``spiffe`` binds
            grant issuer identity to the workload SPIFFE ID when available.
    """

    backend: BackendName
    ttl_seconds_default: int = _DEFAULT_TTL_SECONDS
    ttl_overrides: dict[str, int] = field(default_factory=dict)
    backend_settings: dict[str, Any] = field(default_factory=dict)
    require_grant: bool = False
    identity_mode: IdentityMode = "ed25519"

    @classmethod
    def from_raw(cls, raw: dict[str, Any] | None) -> BrokerConfig:
        """Parse a raw mapping out of ``bernstein.yaml``."""
        if not raw:
            raise SecretsBrokerError("security.secrets block is empty or missing")
        backend_raw: object = raw.get("backend")
        if not isinstance(backend_raw, str) or backend_raw not in _BACKEND_REGISTRY:
            valid = ", ".join(sorted(_BACKEND_REGISTRY))
            raise SecretsBrokerError(f"unknown backend {backend_raw!r}; valid: {valid}")
        backend: BackendName = backend_raw  # type: ignore[assignment]
        mint_raw: object = raw.get("mint") or {}
        if not isinstance(mint_raw, dict):
            raise SecretsBrokerError("mint block must be a mapping")
        mint: dict[str, Any] = {str(k): v for k, v in mint_raw.items()}
        ttl_default = int(mint.get("ttl_seconds_default", _DEFAULT_TTL_SECONDS))
        if ttl_default <= 0:
            raise SecretsBrokerError("mint.ttl_seconds_default must be positive")
        overrides_raw: object = mint.get("ttl_overrides")
        if overrides_raw is None:
            overrides_raw = {}
        if not isinstance(overrides_raw, dict):
            raise SecretsBrokerError("mint.ttl_overrides must be a mapping")
        overrides: dict[str, int] = {str(k): int(v) for k, v in overrides_raw.items()}
        backend_settings_raw: object = raw.get("backend_settings") or {}
        if not isinstance(backend_settings_raw, dict):
            raise SecretsBrokerError("backend_settings must be a mapping")
        backend_settings: dict[str, Any] = {str(k): v for k, v in backend_settings_raw.items()}

        # Grant / identity block (issue #2516). Both default to the legacy,
        # grant-free, self-contained Ed25519 behaviour when the block is absent.
        grants_raw: object = raw.get("grants")
        if grants_raw is None:
            grants_raw = {}
        if not isinstance(grants_raw, dict):
            raise SecretsBrokerError("grants block must be a mapping")
        require_grant = bool(grants_raw.get("require_grant", False))
        identity_mode_raw: object = grants_raw.get("identity_mode", "ed25519")
        if not isinstance(identity_mode_raw, str) or identity_mode_raw not in _IDENTITY_MODES:
            valid = ", ".join(sorted(_IDENTITY_MODES))
            raise SecretsBrokerError(f"unknown grants.identity_mode {identity_mode_raw!r}; valid: {valid}")
        identity_mode: IdentityMode = identity_mode_raw  # type: ignore[assignment]

        return cls(
            backend=backend,
            ttl_seconds_default=ttl_default,
            ttl_overrides=overrides,
            backend_settings=backend_settings,
            require_grant=require_grant,
            identity_mode=identity_mode,
        )


# ---------------------------------------------------------------------------
# Broker itself
# ---------------------------------------------------------------------------


@dataclass
class _Registration:
    """Internal record for a minted token."""

    token: MintedToken
    raw_value: str
    revoked: bool = False
    # Grant lineage (issue #2516). Empty in legacy (grant-free) mode.
    grant_id: str = ""
    run_id: str = ""
    audience: str = ""


class SecretsBroker:
    """Mint short-lived tokens that stand in for backing secrets.

    Thread-safety: a single :class:`threading.Lock` guards the registry.
    The broker is designed to be created once at orchestrator startup and
    shared across tasks. Backends do their own connectivity per call;
    operators wanting caching should wrap the backend.

    Grant-enforcing mode (issue #2516)
    ==================================

    When constructed with a ``grant_ledger`` and ``require_grant=True``, the
    broker refuses to mint a token unless a verifiable, chain-anchored grant
    exists for the ``(task_id, secret_name)`` pair. The refusal is itself a
    chain event. A minted token inherits the grant's task id, audience, and
    expiry; the token id is recorded in the grant lifecycle. ``resolve`` then
    refuses a token presented outside its granted audience, and audience,
    expiry, and revocation refusals are recorded as chain-anchored records
    rather than only as in-process callbacks. Left unset, the broker keeps its
    legacy grant-free behaviour unchanged.
    """

    def __init__(
        self,
        backend: SecretsBackend,
        *,
        config: BrokerConfig,
        audit_sink: AuditSink | None = None,
        clock: Callable[[], float] = time.time,
        grant_ledger: Any = None,
        require_grant: bool = False,
    ) -> None:
        self._backend = backend
        self._config = config
        self._audit_sink = audit_sink
        self._clock = clock
        self._grant_ledger = grant_ledger
        self._require_grant = require_grant
        self._lock = threading.Lock()
        self._registry: dict[str, _Registration] = {}
        # Secondary index keyed by token value so ``resolve`` is O(1).
        self._by_value: dict[str, _Registration] = {}

    # -- public API ---------------------------------------------------------

    @property
    def backend_name(self) -> str:
        return self._backend.name

    def mint(
        self,
        *,
        secret_name: str,
        task_id: str,
        ttl_seconds: int | None = None,
        grant: Any = None,
        run_id: str | None = None,
        version_id: str = "",
    ) -> MintedToken:
        """Mint a short-lived token for ``secret_name`` scoped to ``task_id``.

        Args:
            secret_name: Backing-store name (Vault path, AWS ARN, keychain
                account, etc., per backend convention).
            task_id: Bernstein task id that owns this token. Auto-revoke is
                keyed off this id.
            ttl_seconds: Lifetime override. ``None`` uses the per-secret
                override, then the config default.
            grant: A :class:`~bernstein.core.identity.grants.GrantReceipt` the
                orchestrator issued for this task. Required in grant-enforcing
                mode; the minted token inherits the grant's audience and expiry.
            run_id: Run scope for chain-anchored refusal records when no grant
                is presented. Defaults to the grant's run id, else ``task_id``.
            version_id: Secret-version identifier a rotation run assigns to the
                material being minted. Empty for an ordinary mint.

        Returns:
            A :class:`MintedToken`. The ``value`` field is what the agent
            process should see in its env.

        Raises:
            SecretsBrokerError: In grant-enforcing mode, when no verifiable
                grant exists for the ``(task_id, secret_name)`` pair. The
                refusal is recorded as a chain event before the error is raised.
        """
        if not secret_name:
            raise SecretsBrokerError("secret_name must not be empty")
        if not task_id:
            raise SecretsBrokerError("task_id must not be empty")

        audience = ""
        grant_id = ""
        grant_run_id = run_id or ""
        expires_override: float | None = None
        if self._require_grant:
            grant_run_id, grant_id, audience, expires_override = self._authorize_mint(
                secret_name=secret_name, task_id=task_id, grant=grant, run_id=run_id
            )
        elif grant is not None:
            # Grant supplied without enforcement: still honour its scope so the
            # token carries the audience and expiry the grant authorized.
            grant_run_id = str(getattr(grant, "run_id", "") or run_id or "")
            grant_id = str(getattr(grant, "grant_id", ""))
            audience = str(getattr(grant, "audience", ""))
            grant_expiry = int(getattr(grant, "expiry", 0) or 0)
            if grant_expiry:
                expires_override = float(grant_expiry)

        ttl = self._resolve_ttl(secret_name, ttl_seconds)
        store_id = ""
        store_expiry: float | None = None
        if isinstance(self._backend, ExternalStoreBackend):
            raw_value, store_id, store_expiry = self._issue_external(
                self._backend,
                secret_name=secret_name,
                task_id=task_id,
                audience=audience,
                ttl_seconds=ttl,
                run_id=grant_run_id or task_id,
                grant_id=grant_id,
            )
        else:
            raw_value = self._backend.read(secret_name)
        token_id = _new_token_id()
        token_value = _new_token_value()
        now = self._clock()
        expires_at = expires_override if expires_override is not None else now + ttl
        # A store that issues a shorter-lived credential than we asked for caps
        # the token: the broker must never outlive the credential behind it.
        if store_expiry is not None and store_expiry < expires_at:
            expires_at = store_expiry
        unbounded = expires_override is None and store_expiry is None
        effective_ttl = ttl if unbounded else int(expires_at - now)
        token = MintedToken(
            token_id=token_id,
            value=token_value,
            secret_name=secret_name,
            task_id=task_id,
            expires_at=expires_at,
            ttl_seconds=effective_ttl,
            audience=audience,
            version_id=version_id,
        )
        registration = _Registration(
            token=token,
            raw_value=raw_value,
            grant_id=grant_id,
            run_id=grant_run_id,
            audience=audience,
        )
        with self._lock:
            self._registry[token_id] = registration
            self._by_value[token_value] = registration
        register_secret_for_redaction(raw_value)
        register_secret_for_redaction(token_value)
        # Record the broker exchange in the grant lifecycle so token-to-grant
        # resolution works offline from the chain alone (issue #2516).
        if grant_id and grant_run_id and self._grant_ledger is not None:
            self._record_grant_exchange(
                run_id=grant_run_id,
                grant_id=grant_id,
                token_id=token_id,
                task_id=task_id,
                secret_name=secret_name,
                audience=audience,
                store=store_id,
            )
        self._emit(
            AuditEvent(
                kind="mint",
                token_id=token_id,
                secret_name=secret_name,
                task_id=task_id,
                ts_ns=time.time_ns(),
                ttl_seconds=effective_ttl,
            )
        )
        return token

    @contextmanager
    def mint_scoped(
        self,
        *,
        secret_name: str,
        task_id: str,
        ttl_seconds: int | None = None,
        grant: Any = None,
        run_id: str | None = None,
        version_id: str = "",
    ) -> Generator[MintedToken, None, None]:
        """Mint a token; auto-revoke on context-manager exit."""
        token = self.mint(
            secret_name=secret_name,
            task_id=task_id,
            ttl_seconds=ttl_seconds,
            grant=grant,
            run_id=run_id,
            version_id=version_id,
        )
        try:
            yield token
        finally:
            self.revoke(token.token_id, reason="scope-exit")

    @contextmanager
    def bind_scoped(
        self,
        *,
        secret_name: str,
        task_id: str,
        env_var: str,
        ttl_seconds: int | None = None,
        grant: Any = None,
        run_id: str | None = None,
        env: MutableMapping[str, str] | None = None,
    ) -> Generator[MintedToken, None, None]:
        """Bind a minted token into ``env`` for the duration of one step.

        A spec names a secret by opaque reference; the value is bound into the
        environment only for the block. On the way out the variable is removed
        -- or restored to whatever the operator had set under that name -- and
        the token is revoked, which also drops the backing credential from the
        broker registry. An environment dump before the block and after it
        finds the bound value in neither.

        Args:
            secret_name: Backing-store name or, for the ``external`` backend,
                a ``"<store>:<path>"`` reference.
            task_id: Task that owns the token.
            env_var: Variable name to bind under.
            ttl_seconds: Lifetime override, as for :meth:`mint`.
            grant: Grant authorizing the mint, as for :meth:`mint`.
            run_id: Run scope for chain-anchored refusals, as for :meth:`mint`.
            env: Mapping to bind into. Defaults to :data:`os.environ`.

        Yields:
            The :class:`MintedToken` bound under ``env_var``.
        """
        if not env_var:
            raise SecretsBrokerError("env_var must not be empty")
        target: MutableMapping[str, str] = os.environ if env is None else env
        token = self.mint(
            secret_name=secret_name,
            task_id=task_id,
            ttl_seconds=ttl_seconds,
            grant=grant,
            run_id=run_id,
        )
        had_previous = env_var in target
        previous = target.get(env_var, "")
        target[env_var] = token.value
        try:
            yield token
        finally:
            if had_previous:
                target[env_var] = previous
            else:
                target.pop(env_var, None)
            self.revoke(token.token_id, reason="scope-exit")

    def resolve(self, token_value: str, *, audience: str | None = None) -> str:
        """Return the raw backing value for a minted token value.

        Args:
            token_value: The opaque token the agent process holds.
            audience: When set, the audience the caller is presenting the token
                to. A token minted from a grant refuses to resolve outside its
                granted audience, and the refusal is recorded as a chain event.

        Raises:
            SecretsBrokerError: If the token is unknown, revoked, expired, or
                presented outside its granted audience.
        """
        if not token_value:
            raise SecretsBrokerError("empty token value")
        now = self._clock()
        # Stage the audit event inside the lock, dispatch it outside so a
        # slow audit sink cannot stall every other broker operation.
        deferred: AuditEvent | None = None
        raw_value: str | None = None
        # Chain-anchored refusal descriptor: (reason, run_id, task_id, secret,
        # grant_id, error_message). Recorded and raised after the lock.
        refusal: tuple[str, str, str, str, str, str] | None = None
        with self._lock:
            reg = self._by_value.get(token_value)
            if reg is None:
                raise SecretsBrokerError("unknown token")
            if reg.revoked:
                refusal = (
                    "revoked",
                    reg.run_id,
                    reg.token.task_id,
                    reg.token.secret_name,
                    reg.grant_id,
                    "token has been revoked",
                )
            elif now >= reg.token.expires_at:
                reg.revoked = True
                deferred = AuditEvent(
                    kind="expire",
                    token_id=reg.token.token_id,
                    secret_name=reg.token.secret_name,
                    task_id=reg.token.task_id,
                    ts_ns=time.time_ns(),
                    ttl_seconds=reg.token.ttl_seconds,
                    reason="ttl",
                )
                refusal = (
                    "expired",
                    reg.run_id,
                    reg.token.task_id,
                    reg.token.secret_name,
                    reg.grant_id,
                    "token has expired",
                )
            elif reg.audience and audience is not None and audience != reg.audience:
                refusal = (
                    f"audience_mismatch:{audience}",
                    reg.run_id,
                    reg.token.task_id,
                    reg.token.secret_name,
                    reg.grant_id,
                    f"token presented outside its granted audience {reg.audience!r}",
                )
            else:
                raw_value = reg.raw_value
                deferred = AuditEvent(
                    kind="resolve",
                    token_id=reg.token.token_id,
                    secret_name=reg.token.secret_name,
                    task_id=reg.token.task_id,
                    ts_ns=time.time_ns(),
                )
        if deferred is not None:
            self._emit(deferred)
        if refusal is not None:
            reason, run_id, task_id, secret_name, grant_id, message = refusal
            self._record_grant_refusal(
                run_id=run_id,
                task_id=task_id,
                secret_name=secret_name,
                grant_id=grant_id,
                reason=f"resolve_{reason}",
            )
            raise SecretsBrokerError(message)
        if raw_value is None:  # pragma: no cover - defensive; branch above ensures non-None
            raise SecretsBrokerError("broker internal state corrupt")
        return raw_value

    def revoke(self, token_id: str, *, reason: str = "explicit") -> bool:
        """Revoke a single token by id. Returns ``True`` when it existed."""
        deferred: AuditEvent | None = None
        token_value_to_drop: str | None = None
        grant_ref: tuple[str, str, str, str] | None = None
        with self._lock:
            reg = self._registry.get(token_id)
            if reg is None or reg.revoked:
                return False
            reg.revoked = True
            # A revoked token's backing credential has no further use; dropping
            # it keeps a bound value from outliving the step that bound it.
            reg.raw_value = ""
            deferred = AuditEvent(
                kind="revoke",
                token_id=token_id,
                secret_name=reg.token.secret_name,
                task_id=reg.token.task_id,
                ts_ns=time.time_ns(),
                ttl_seconds=reg.token.ttl_seconds,
                reason=reason,
            )
            token_value_to_drop = reg.token.value
            if reg.grant_id and reg.run_id:
                grant_ref = (reg.run_id, reg.grant_id, reg.token.task_id, reg.token.secret_name)
        if token_value_to_drop is not None:
            unregister_secret_for_redaction(token_value_to_drop)
        if grant_ref is not None:
            self._record_grant_revocation(*grant_ref, reason=reason)
        if deferred is not None:
            self._emit(deferred)
        return True

    def revoke_task(self, task_id: str, *, reason: str = "task-exit") -> int:
        """Revoke every live token owned by ``task_id``. Returns count."""
        deferred: list[AuditEvent] = []
        token_values_to_drop: list[str] = []
        grant_refs: list[tuple[str, str, str, str]] = []
        with self._lock:
            for reg in self._registry.values():
                if reg.revoked or reg.token.task_id != task_id:
                    continue
                reg.revoked = True
                reg.raw_value = ""
                deferred.append(
                    AuditEvent(
                        kind="revoke",
                        token_id=reg.token.token_id,
                        secret_name=reg.token.secret_name,
                        task_id=task_id,
                        ts_ns=time.time_ns(),
                        ttl_seconds=reg.token.ttl_seconds,
                        reason=reason,
                    )
                )
                token_values_to_drop.append(reg.token.value)
                if reg.grant_id and reg.run_id:
                    grant_refs.append((reg.run_id, reg.grant_id, task_id, reg.token.secret_name))
        for value in token_values_to_drop:
            unregister_secret_for_redaction(value)
        for ref in grant_refs:
            self._record_grant_revocation(*ref, reason=reason)
        for event in deferred:
            self._emit(event)
        return len(deferred)

    def list_live(self) -> list[MintedToken]:
        """Return every token that is neither revoked nor expired."""
        now = self._clock()
        out: list[MintedToken] = []
        with self._lock:
            for reg in self._registry.values():
                if reg.revoked:
                    continue
                if now >= reg.token.expires_at:
                    continue
                out.append(reg.token)
        return out

    def list_backend_secrets(self) -> list[str]:
        """Proxy to the backend's enumeration support."""
        return self._backend.list_names()

    # -- internals ----------------------------------------------------------

    def _resolve_ttl(self, secret_name: str, override: int | None) -> int:
        if override is not None:
            if override <= 0:
                raise SecretsBrokerError("ttl_seconds must be positive")
            return int(override)
        per_secret = self._config.ttl_overrides.get(secret_name)
        if per_secret is not None:
            return int(per_secret)
        return self._config.ttl_seconds_default

    # -- grant enforcement (issue #2516) ------------------------------------

    def _authorize_mint(
        self,
        *,
        secret_name: str,
        task_id: str,
        grant: Any,
        run_id: str | None,
    ) -> tuple[str, str, str, float | None]:
        """Refuse to mint without a verifiable grant; record refusals on-chain.

        Returns ``(run_id, grant_id, audience, expires_override)`` for a grant
        that verifies against the chain and matches ``(task_id, secret_name)``.
        Any refusal is appended to the grant ledger as a ``grant_refused``
        record before the raising :class:`SecretsBrokerError` propagates.
        """
        refusal_run = run_id or (str(getattr(grant, "run_id", "")) if grant is not None else "") or task_id
        if grant is None:
            self._record_grant_refusal(
                run_id=refusal_run,
                task_id=task_id,
                secret_name=secret_name,
                grant_id="",
                reason="no_grant_presented",
            )
            raise SecretsBrokerError(f"no verifiable grant for task {task_id!r} secret {secret_name!r}")

        grant_run = str(getattr(grant, "run_id", "") or refusal_run)
        grant_id = str(getattr(grant, "grant_id", ""))
        ok, reason = self._verify_grant(grant, task_id=task_id, secret_name=secret_name)
        if not ok:
            self._record_grant_refusal(
                run_id=grant_run,
                task_id=task_id,
                secret_name=secret_name,
                grant_id=grant_id,
                reason=f"grant_{reason}",
            )
            raise SecretsBrokerError(f"grant refused for task {task_id!r} secret {secret_name!r}: {reason}")

        audience = str(getattr(grant, "audience", ""))
        expiry = int(getattr(grant, "expiry", 0) or 0)
        expires_override = float(expiry) if expiry else None
        return grant_run, grant_id, audience, expires_override

    def _verify_grant(self, grant: Any, *, task_id: str, secret_name: str) -> tuple[bool, str]:
        """Confirm ``grant`` verifies against the chain and matches the request."""
        from bernstein.core.identity import grants as _grants

        if getattr(grant, "task_id", None) != task_id:
            return False, "task_mismatch"
        # Grant records only ever carry a salted reference to secret_name
        # (never the raw backend key/env var name -- see
        # grants.hash_secret_name), so the comparison re-derives the requested
        # name under the salt carried in the record.
        if not _grants.secret_name_matches(str(getattr(grant, "secret_name", "") or ""), secret_name):
            return False, "secret_mismatch"
        if self._grant_ledger is None:
            return False, "no_ledger"

        run_id = str(getattr(grant, "run_id", ""))
        grant_id = str(getattr(grant, "grant_id", ""))
        result = _grants.verify_grant_chain(
            root=self._grant_ledger.root, run_id=run_id, key=self._grant_ledger.hmac_key
        )
        if not result.valid:
            return False, "chain_unverified"
        active = _grants.find_active_grant(result, task_id=task_id, secret_name=secret_name, now=self._clock())
        if active is None or active.grant_id != grant_id:
            return False, "not_active"
        return True, "ok"

    # -- external stores (issue #4984) --------------------------------------

    def _issue_external(
        self,
        backend: ExternalStoreBackend,
        *,
        secret_name: str,
        task_id: str,
        audience: str,
        ttl_seconds: int,
        run_id: str,
        grant_id: str,
    ) -> tuple[str, str, float | None]:
        """Resolve, revocation-check, then mint against the operator's store.

        Returns ``(credential value, store identity, store expiry or None)``.
        The value is held in the broker registry for the token's lifetime and
        goes nowhere else; only the store identity reaches the chain.

        Raises:
            SecretsBrokerError: When the store cannot serve the reference, or
                when it reports the upstream secret revoked. An upstream
                revocation is recorded as a chain-anchored refusal before the
                error propagates, so "the operator revoked it upstream" and
                "we refused to mint" are the same record.
        """
        try:
            descriptor = backend.describe(secret_name)
            revoked = descriptor.revoked or backend.report_revocation(secret_name, upstream_id=descriptor.upstream_id)
        except ExternalStoreError as exc:
            raise SecretsBrokerError(f"external store failed for {secret_name!r}: {exc}") from exc
        store_id = descriptor.store_id or backend.store_name
        if revoked:
            self._record_grant_refusal(
                run_id=run_id,
                task_id=task_id,
                secret_name=secret_name,
                grant_id=grant_id,
                reason="upstream_revoked",
            )
            raise SecretsBrokerError(f"upstream secret {secret_name!r} is revoked in store {store_id!r}")
        try:
            credential = backend.issue(secret_name, audience=audience, ttl_seconds=ttl_seconds)
        except ExternalStoreError as exc:
            raise SecretsBrokerError(f"external store refused to mint {secret_name!r}: {exc}") from exc
        expiry = credential.expires_at if credential.expires_at > 0 else None
        return credential.value, store_id, expiry

    def _record_grant_exchange(
        self,
        *,
        run_id: str,
        grant_id: str,
        token_id: str,
        task_id: str,
        secret_name: str,
        audience: str,
        store: str = "",
    ) -> None:
        if self._grant_ledger is None:
            return
        try:
            self._grant_ledger.record_exchange(
                run_id=run_id,
                grant_id=grant_id,
                token_id=token_id,
                task_id=task_id,
                secret_name=secret_name,
                audience=audience,
                store=store,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("grant exchange record failed: %s", type(exc).__name__)

    def _record_grant_revocation(
        self, run_id: str, grant_id: str, task_id: str, secret_name: str, *, reason: str
    ) -> None:
        if self._grant_ledger is None:
            return
        try:
            self._grant_ledger.revoke_grant(
                run_id=run_id,
                grant_id=grant_id,
                task_id=task_id,
                secret_name=secret_name,
                reason=reason,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("grant revocation record failed: %s", type(exc).__name__)

    def _record_grant_refusal(self, *, run_id: str, task_id: str, secret_name: str, grant_id: str, reason: str) -> None:
        if self._grant_ledger is None or not run_id:
            return
        try:
            self._grant_ledger.record_refusal(
                run_id=run_id,
                task_id=task_id,
                secret_name=secret_name,
                grant_id=grant_id,
                reason=reason,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("grant refusal record failed: %s", type(exc).__name__)

    def _emit(self, event: AuditEvent) -> None:
        """Dispatch *event* to the logger and the optional audit sink.

        Callers must release the broker lock before invoking this helper so
        that a slow or misbehaving sink cannot stall other broker operations.
        token id and secret name are non-secret identifiers; raw values are
        never logged.
        """
        logger.info(
            "broker.%s token_id=%s secret_name=%s task_id=%s ttl=%ss reason=%s",
            event.kind,
            event.token_id,
            event.secret_name,
            event.task_id,
            event.ttl_seconds,
            event.reason or "-",
        )
        if self._audit_sink is None:
            return
        try:
            self._audit_sink(event)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("audit sink raised %s: %s", type(exc).__name__, exc)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


_BACKEND_REGISTRY: dict[BackendName, Callable[..., SecretsBackend]] = {
    "external": ExternalStoreBackend,
    "vault": VaultBackend,
    "aws_secretsmanager": AwsSecretsManagerBackend,
    "gcp_secret_manager": GcpSecretManagerBackend,
    "macos_keychain": MacosKeychainBackend,
    "linux_keyring": LinuxKeyringBackend,
    "file_encrypted": FileEncryptedBackend,
}


def _new_token_id() -> str:
    """Return a short, url-safe token identifier (non-secret)."""
    return _secrets.token_urlsafe(8)


def _new_token_value() -> str:
    """Return the actual minted token string handed to the agent."""
    return f"{_TOKEN_PREFIX}{_secrets.token_urlsafe(32)}"


def build_broker_from_config(
    raw: dict[str, Any] | None,
    *,
    audit_sink: AuditSink | None = None,
    grant_ledger: Any = None,
) -> SecretsBroker:
    """Build a broker from a raw ``security.secrets`` mapping.

    When ``grants.require_grant`` is set in config, pass a ``grant_ledger``
    (a :class:`~bernstein.core.identity.grants.GrantLedger`) so the broker can
    enforce and record chain-anchored grants (issue #2516). Grant enforcement
    is a no-op without a ledger even when ``require_grant`` is set, so a
    misconfigured install fails closed at mint time rather than silently
    minting unscoped tokens.
    """
    config = BrokerConfig.from_raw(raw)
    factory = _BACKEND_REGISTRY[config.backend]
    backend = factory(**config.backend_settings)
    return SecretsBroker(
        backend,
        config=config,
        audit_sink=audit_sink,
        grant_ledger=grant_ledger,
        require_grant=config.require_grant,
    )
