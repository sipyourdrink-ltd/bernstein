"""Config-schema additions for grant enforcement (issue #2516, Phase 5)."""

from __future__ import annotations

import pytest

from bernstein.core.security.secrets_broker import BrokerConfig, SecretsBrokerError


class TestGrantConfig:
    def test_defaults_are_legacy_grant_free(self) -> None:
        cfg = BrokerConfig.from_raw({"backend": "file_encrypted"})
        assert cfg.require_grant is False
        assert cfg.identity_mode == "ed25519"

    def test_require_grant_parsed(self) -> None:
        cfg = BrokerConfig.from_raw({"backend": "file_encrypted", "grants": {"require_grant": True}})
        assert cfg.require_grant is True

    def test_identity_mode_spiffe_parsed(self) -> None:
        cfg = BrokerConfig.from_raw({"backend": "file_encrypted", "grants": {"identity_mode": "spiffe"}})
        assert cfg.identity_mode == "spiffe"

    def test_unknown_identity_mode_rejected(self) -> None:
        with pytest.raises(SecretsBrokerError, match="identity_mode"):
            BrokerConfig.from_raw({"backend": "file_encrypted", "grants": {"identity_mode": "kerberos"}})

    def test_grants_block_must_be_mapping(self) -> None:
        with pytest.raises(SecretsBrokerError, match="grants block"):
            BrokerConfig.from_raw({"backend": "file_encrypted", "grants": []})
