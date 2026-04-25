"""Unit tests for the vault protocol primitives and resolver fallback logic.

Covers:

* :func:`bernstein.core.security.vault.resolver.fingerprint` and
  :func:`mask_secret` formatting invariants.
* :func:`resolve_secret` vault-hit, env-fallback (with deprecation
  warning), and missing paths.
* The deprecation warning is only emitted once per process for a given
  provider/env-var pair.
"""

from __future__ import annotations

import warnings
from datetime import UTC, datetime

import pytest

from bernstein.core.security.vault.protocol import (
    CredentialRecord,
    StoredSecret,
    VaultNotFoundError,
)
from bernstein.core.security.vault.resolver import (
    fingerprint,
    mask_secret,
    resolve_secret,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeVault:
    """Minimal in-memory :class:`CredentialVault` used by resolver tests."""

    backend_id = "fake"

    def __init__(self) -> None:
        self._store: dict[str, StoredSecret] = {}
        self.touch_calls: list[tuple[str, str]] = []

    def put(self, provider_id: str, secret: StoredSecret) -> None:
        self._store[provider_id] = secret

    def get(self, provider_id: str) -> StoredSecret:
        if provider_id not in self._store:
            raise VaultNotFoundError(provider_id)
        return self._store[provider_id]

    def delete(self, provider_id: str) -> bool:
        return self._store.pop(provider_id, None) is not None

    def list(self) -> list[CredentialRecord]:
        return [
            CredentialRecord(
                provider_id=pid,
                account=stored.account,
                fingerprint=stored.fingerprint,
                created_at=stored.created_at,
                last_used_at=stored.last_used_at,
                metadata=stored.metadata,
            )
            for pid, stored in self._store.items()
        ]

    def touch(self, provider_id: str, last_used_at: str) -> None:
        self.touch_calls.append((provider_id, last_used_at))


def _stored(secret: str = "abc-token") -> StoredSecret:
    """Build a freshly-timestamped :class:`StoredSecret` for tests."""
    return StoredSecret(
        secret=secret,
        account="alex@example.com",
        fingerprint=fingerprint(secret),
        created_at=datetime.now(tz=UTC).isoformat(),
    )


# ---------------------------------------------------------------------------
# fingerprint / mask_secret
# ---------------------------------------------------------------------------


def test_fingerprint_is_deterministic_and_short() -> None:
    secret = "ghp_1234567890abcdef"
    fp1 = fingerprint(secret)
    fp2 = fingerprint(secret)
    assert fp1 == fp2
    assert len(fp1) == 12
    # Different secrets produce different fingerprints (collision-free for
    # the simple cases we exercise).
    assert fingerprint("ghp_other") != fp1


def test_mask_secret_keeps_prefix_for_long_strings() -> None:
    masked = mask_secret("ghp_1234567890")
    assert masked.startswith("ghp_")
    assert masked.endswith("**********")
    # Length is preserved so users can spot truncation.
    assert len(masked) == len("ghp_1234567890")


def test_mask_secret_fully_masks_short_strings() -> None:
    masked = mask_secret("xoxb")
    assert masked == "****"
    assert mask_secret("") == ""


# ---------------------------------------------------------------------------
# resolve_secret
# ---------------------------------------------------------------------------


def test_resolve_secret_prefers_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    vault = _FakeVault()
    vault.put("github", _stored("vault-token"))

    # An env var also exists, but the vault should win and no
    # DeprecationWarning should fire for this resolution.
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        resolution = resolve_secret(
            "github",
            vault=vault,
            environ={"GITHUB_TOKEN": "env-token"},
            audit=False,
        )

    assert resolution.found is True
    assert resolution.source == "vault"
    assert resolution.secret == "vault-token"
    assert resolution.account == "alex@example.com"
    assert vault.touch_calls and vault.touch_calls[0][0] == "github"


def test_resolve_secret_falls_back_to_env_with_deprecation_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Reset the module-level "already warned" set so this test sees the
    # warning even after other tests in the module have run.
    from bernstein.core.security.vault import resolver as mod

    mod._WARNED.clear()
    vault = _FakeVault()  # empty
    with pytest.warns(DeprecationWarning, match="GITHUB_TOKEN"):
        resolution = resolve_secret(
            "github",
            vault=vault,
            environ={"GITHUB_TOKEN": "env-token"},
            audit=False,
        )
    assert resolution.found is True
    assert resolution.source == "env"
    assert resolution.env_var_used == "GITHUB_TOKEN"
    assert resolution.secret == "env-token"


def test_resolve_secret_missing_returns_not_found() -> None:
    resolution = resolve_secret(
        "linear",
        vault=_FakeVault(),
        environ={},
        audit=False,
    )
    assert resolution.found is False
    assert resolution.source == "missing"
    assert resolution.secret == ""


def test_resolve_secret_unknown_provider_raises() -> None:
    with pytest.raises(KeyError):
        resolve_secret("github-enterprise", vault=None, environ={}, audit=False)
