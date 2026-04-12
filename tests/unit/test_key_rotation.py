"""Unit tests for automatic API key rotation."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from bernstein.core.key_rotation import (
    KeyRotationConfig,
    KeyRotationManager,
    KeyRotationScheduler,
    KeyState,
    ManagedKey,
    _fingerprint,
    _parse_interval,
)
from bernstein.core.secrets import SecretsConfig, SecretsError

# ---------------------------------------------------------------------------
# _parse_interval
# ---------------------------------------------------------------------------


class TestParseInterval:
    def test_integer_passthrough(self) -> None:
        assert _parse_interval(3600) == 3600

    def test_string_digits(self) -> None:
        assert _parse_interval("3600") == 3600

    def test_seconds_suffix(self) -> None:
        assert _parse_interval("120s") == 120

    def test_minutes_suffix(self) -> None:
        assert _parse_interval("60m") == 3600

    def test_hours_suffix(self) -> None:
        assert _parse_interval("24h") == 86400

    def test_days_suffix(self) -> None:
        assert _parse_interval("30d") == 2592000

    def test_invalid_format(self) -> None:
        with pytest.raises(ValueError, match="Invalid interval"):
            _parse_interval("30x")

    def test_whitespace_stripped(self) -> None:
        assert _parse_interval("  7d  ") == 604800


# ---------------------------------------------------------------------------
# _fingerprint
# ---------------------------------------------------------------------------


class TestFingerprint:
    def test_deterministic(self) -> None:
        fp1 = _fingerprint("sk-test-key-123")
        fp2 = _fingerprint("sk-test-key-123")
        assert fp1 == fp2

    def test_different_values(self) -> None:
        fp1 = _fingerprint("sk-key-a")
        fp2 = _fingerprint("sk-key-b")
        assert fp1 != fp2

    def test_length(self) -> None:
        fp = _fingerprint("anything")
        assert len(fp) == 16


# ---------------------------------------------------------------------------
# KeyRotationConfig
# ---------------------------------------------------------------------------


class TestKeyRotationConfig:
    def test_defaults(self) -> None:
        cfg = KeyRotationConfig()
        assert cfg.interval_seconds == 2592000
        assert cfg.on_leak == "revoke_immediately"
        assert cfg.secrets_provider is None
        assert cfg.leak_patterns == []

    def test_custom(self) -> None:
        cfg = KeyRotationConfig(
            interval_seconds=86400,
            on_leak="alert_only",
            secrets_provider="vault",
            secrets_path="secret/keys",
        )
        assert cfg.interval_seconds == 86400
        assert cfg.on_leak == "alert_only"
        assert cfg.secrets_provider == "vault"


# ---------------------------------------------------------------------------
# ManagedKey
# ---------------------------------------------------------------------------


class TestManagedKey:
    def test_serialization_roundtrip(self) -> None:
        key = ManagedKey(
            key_id="ANTHROPIC_API_KEY:abc12345",
            env_var="ANTHROPIC_API_KEY",
            state=KeyState.ACTIVE,
            created_at=1000.0,
            fingerprint="abc1234567890123",
        )
        d = key.to_dict()
        restored = ManagedKey.from_dict(d)
        assert restored.key_id == key.key_id
        assert restored.env_var == key.env_var
        assert restored.state == KeyState.ACTIVE
        assert restored.fingerprint == key.fingerprint

    def test_revoked_serialization(self) -> None:
        key = ManagedKey(
            key_id="test:abc",
            env_var="TEST_KEY",
            state=KeyState.REVOKED,
            created_at=1000.0,
            revoked_at=2000.0,
            revoke_reason="leak detected",
        )
        d = key.to_dict()
        assert d["state"] == "revoked"
        assert d["revoke_reason"] == "leak detected"

        restored = ManagedKey.from_dict(d)
        assert restored.state == KeyState.REVOKED
        assert restored.revoke_reason == "leak detected"


# ---------------------------------------------------------------------------
# KeyRotationManager — registration and queries
# ---------------------------------------------------------------------------


class TestKeyRotationManagerBasic:
    def _make_manager(self, tmp_path: Any) -> KeyRotationManager:
        cfg = KeyRotationConfig(state_dir=str(tmp_path / "kr"))
        return KeyRotationManager(cfg)

    def test_register_key(self, tmp_path: Any) -> None:
        mgr = self._make_manager(tmp_path)
        key = mgr.register_key("ANTHROPIC_API_KEY", "sk-ant-test-value")
        assert key.state == KeyState.ACTIVE
        assert key.env_var == "ANTHROPIC_API_KEY"
        assert key.fingerprint == _fingerprint("sk-ant-test-value")

    def test_get_active_keys(self, tmp_path: Any) -> None:
        mgr = self._make_manager(tmp_path)
        mgr.register_key("KEY_A", "val-a")
        mgr.register_key("KEY_B", "val-b")
        active = mgr.get_active_keys()
        assert len(active) == 2

    def test_get_all_keys(self, tmp_path: Any) -> None:
        mgr = self._make_manager(tmp_path)
        key = mgr.register_key("KEY_A", "val-a")
        mgr.revoke_key(key, reason="test")
        mgr.register_key("KEY_B", "val-b")
        all_keys = mgr.get_all_keys()
        assert len(all_keys) == 2
        states = {k.state for k in all_keys}
        assert states == {KeyState.ACTIVE, KeyState.REVOKED}


# ---------------------------------------------------------------------------
# KeyRotationManager — state persistence
# ---------------------------------------------------------------------------


class TestKeyRotationManagerPersistence:
    def test_state_persisted_and_loaded(self, tmp_path: Any) -> None:
        cfg = KeyRotationConfig(state_dir=str(tmp_path / "kr"))

        mgr1 = KeyRotationManager(cfg)
        mgr1.register_key("MY_KEY", "sk-persist-test")

        # Create a new manager that loads from disk
        mgr2 = KeyRotationManager(cfg)
        keys = mgr2.get_all_keys()
        assert len(keys) == 1
        assert keys[0].env_var == "MY_KEY"

    def test_corrupt_state_handled(self, tmp_path: Any) -> None:
        state_dir = tmp_path / "kr"
        state_dir.mkdir(parents=True)
        (state_dir / "state.json").write_text("NOT JSON!!!")

        cfg = KeyRotationConfig(state_dir=str(state_dir))
        mgr = KeyRotationManager(cfg)
        assert mgr.get_all_keys() == []


# ---------------------------------------------------------------------------
# KeyRotationManager — rotation
# ---------------------------------------------------------------------------


class TestKeyRotationManagerRotation:
    def test_check_rotation_needed_fresh(self, tmp_path: Any) -> None:
        cfg = KeyRotationConfig(
            interval_seconds=86400,
            state_dir=str(tmp_path / "kr"),
        )
        mgr = KeyRotationManager(cfg)
        mgr.register_key("KEY", "val")
        assert mgr.check_rotation_needed() == []

    def test_check_rotation_needed_expired(self, tmp_path: Any) -> None:
        cfg = KeyRotationConfig(
            interval_seconds=60,
            state_dir=str(tmp_path / "kr"),
        )
        mgr = KeyRotationManager(cfg)
        key = mgr.register_key("KEY", "val")
        # Simulate age by backdating
        key.created_at = time.time() - 120
        due = mgr.check_rotation_needed()
        assert len(due) == 1
        assert due[0].key_id == key.key_id

    def test_rotate_key_success(self, tmp_path: Any) -> None:
        secrets_cfg = SecretsConfig(provider="vault", path="secret/test")
        cfg = KeyRotationConfig(
            interval_seconds=60,
            secrets_provider="vault",
            secrets_path="secret/test",
            state_dir=str(tmp_path / "kr"),
        )
        mgr = KeyRotationManager(cfg, secrets_config=secrets_cfg)
        key = mgr.register_key("MY_KEY", "old-value")

        mock_provider = MagicMock()
        mock_provider.fetch.return_value = {"MY_KEY": "new-value-rotated"}

        with patch("bernstein.core.security.key_rotation._create_provider", return_value=mock_provider):
            new_key = mgr.rotate_key(key)

        assert new_key.state == KeyState.ACTIVE
        assert new_key.fingerprint == _fingerprint("new-value-rotated")
        assert key.state == KeyState.EXPIRED
        assert key.rotated_at is not None

    def test_rotate_key_no_provider(self, tmp_path: Any) -> None:
        cfg = KeyRotationConfig(state_dir=str(tmp_path / "kr"))
        mgr = KeyRotationManager(cfg)
        key = mgr.register_key("KEY", "val")

        with pytest.raises(SecretsError, match="no secrets provider"):
            mgr.rotate_key(key)

    def test_rotate_key_fetch_failure(self, tmp_path: Any) -> None:
        secrets_cfg = SecretsConfig(provider="vault", path="secret/test")
        cfg = KeyRotationConfig(state_dir=str(tmp_path / "kr"))
        mgr = KeyRotationManager(cfg, secrets_config=secrets_cfg)
        key = mgr.register_key("KEY", "val")

        mock_provider = MagicMock()
        mock_provider.fetch.side_effect = Exception("connection refused")

        with (
            patch("bernstein.core.security.key_rotation._create_provider", return_value=mock_provider),
            pytest.raises(SecretsError, match="Rotation fetch failed"),
        ):
            mgr.rotate_key(key)

        # Key should revert to ACTIVE on failure
        assert key.state == KeyState.ACTIVE

    def test_rotate_key_missing_in_response(self, tmp_path: Any) -> None:
        secrets_cfg = SecretsConfig(provider="vault", path="secret/test")
        cfg = KeyRotationConfig(state_dir=str(tmp_path / "kr"))
        mgr = KeyRotationManager(cfg, secrets_config=secrets_cfg)
        key = mgr.register_key("MY_KEY", "val")

        mock_provider = MagicMock()
        mock_provider.fetch.return_value = {"OTHER_KEY": "other-val"}

        with (
            patch("bernstein.core.security.key_rotation._create_provider", return_value=mock_provider),
            pytest.raises(SecretsError, match="not found"),
        ):
            mgr.rotate_key(key)

    def test_rotate_uses_field_map(self, tmp_path: Any) -> None:
        secrets_cfg = SecretsConfig(
            provider="vault",
            path="secret/test",
            field_map={"api_key": "MY_KEY"},
        )
        cfg = KeyRotationConfig(state_dir=str(tmp_path / "kr"))
        mgr = KeyRotationManager(cfg, secrets_config=secrets_cfg)
        key = mgr.register_key("MY_KEY", "old-val")

        mock_provider = MagicMock()
        mock_provider.fetch.return_value = {"api_key": "new-val-mapped"}

        with patch("bernstein.core.security.key_rotation._create_provider", return_value=mock_provider):
            new_key = mgr.rotate_key(key)

        assert new_key.state == KeyState.ACTIVE
        assert new_key.fingerprint == _fingerprint("new-val-mapped")


# ---------------------------------------------------------------------------
# KeyRotationManager — leak detection
# ---------------------------------------------------------------------------


class TestKeyRotationManagerLeakDetection:
    def test_detect_leak_no_match(self, tmp_path: Any) -> None:
        cfg = KeyRotationConfig(state_dir=str(tmp_path / "kr"))
        mgr = KeyRotationManager(cfg)
        mgr.register_key("KEY", "not-a-standard-key-format")
        leaked = mgr.detect_leak("some random log output")
        assert leaked == []

    def test_detect_leak_finds_matching_fingerprint(self, tmp_path: Any) -> None:
        key_value = "sk-ant-" + "a" * 30
        cfg = KeyRotationConfig(
            leak_patterns=[r"sk-ant-[a-zA-Z0-9]{20,}"],
            state_dir=str(tmp_path / "kr"),
        )
        mgr = KeyRotationManager(cfg)
        mgr.register_key("KEY", key_value)

        leaked = mgr.detect_leak(f"Error log: token was {key_value} oops")
        assert len(leaked) == 1
        assert leaked[0].env_var == "KEY"

    def test_detect_leak_ignores_revoked(self, tmp_path: Any) -> None:
        key_value = "sk-ant-" + "b" * 30
        cfg = KeyRotationConfig(
            leak_patterns=[r"sk-ant-[a-zA-Z0-9]{20,}"],
            state_dir=str(tmp_path / "kr"),
        )
        mgr = KeyRotationManager(cfg)
        key = mgr.register_key("KEY", key_value)
        mgr.revoke_key(key, "test")

        leaked = mgr.detect_leak(f"log: {key_value}")
        assert leaked == []

    def test_handle_leak_revoke_immediately(self, tmp_path: Any) -> None:
        key_value = "sk-ant-" + "c" * 30
        cfg = KeyRotationConfig(
            on_leak="revoke_immediately",
            leak_patterns=[r"sk-ant-[a-zA-Z0-9]{20,}"],
            state_dir=str(tmp_path / "kr"),
        )
        mgr = KeyRotationManager(cfg)
        key = mgr.register_key("KEY", key_value)

        with patch.dict("os.environ", {"KEY": key_value}, clear=False):
            leaked = mgr.handle_leak(f"output: {key_value}")

        assert len(leaked) == 1
        assert key.state == KeyState.REVOKED
        assert key.revoke_reason == "Leaked key detected in output"

    def test_handle_leak_alert_only(self, tmp_path: Any) -> None:
        key_value = "sk-ant-" + "d" * 30
        cfg = KeyRotationConfig(
            on_leak="alert_only",
            leak_patterns=[r"sk-ant-[a-zA-Z0-9]{20,}"],
            state_dir=str(tmp_path / "kr"),
        )
        mgr = KeyRotationManager(cfg)
        key = mgr.register_key("KEY", key_value)

        leaked = mgr.handle_leak(f"output: {key_value}")
        assert len(leaked) == 1
        # Key stays active under alert_only
        assert key.state == KeyState.ACTIVE


# ---------------------------------------------------------------------------
# KeyRotationManager — revocation
# ---------------------------------------------------------------------------


class TestKeyRotationManagerRevocation:
    def test_revoke_key(self, tmp_path: Any) -> None:
        import os

        cfg = KeyRotationConfig(state_dir=str(tmp_path / "kr"))
        mgr = KeyRotationManager(cfg)
        key = mgr.register_key("MY_KEY", "val")

        os.environ["MY_KEY"] = "val"
        try:
            mgr.revoke_key(key, reason="compromised")

            assert key.state == KeyState.REVOKED
            assert key.revoked_at is not None
            assert key.revoke_reason == "compromised"
            # Env var should be removed
            assert "MY_KEY" not in os.environ
        finally:
            os.environ.pop("MY_KEY", None)

    def test_revoke_removes_from_active(self, tmp_path: Any) -> None:
        cfg = KeyRotationConfig(state_dir=str(tmp_path / "kr"))
        mgr = KeyRotationManager(cfg)
        key = mgr.register_key("KEY", "val")
        mgr.revoke_key(key, "test")
        assert mgr.get_active_keys() == []


# ---------------------------------------------------------------------------
# KeyRotationScheduler
# ---------------------------------------------------------------------------


class TestKeyRotationScheduler:
    def test_start_stop(self, tmp_path: Any) -> None:
        cfg = KeyRotationConfig(state_dir=str(tmp_path / "kr"))
        mgr = KeyRotationManager(cfg)
        scheduler = KeyRotationScheduler(mgr, check_interval=0.1)

        scheduler.start()
        assert scheduler._thread is not None
        assert scheduler._thread.is_alive()

        scheduler.stop()
        assert not scheduler._thread.is_alive()

    def test_double_start_noop(self, tmp_path: Any) -> None:
        cfg = KeyRotationConfig(state_dir=str(tmp_path / "kr"))
        mgr = KeyRotationManager(cfg)
        scheduler = KeyRotationScheduler(mgr, check_interval=0.1)

        scheduler.start()
        thread1 = scheduler._thread
        scheduler.start()  # Should not create a new thread
        assert scheduler._thread is thread1
        scheduler.stop()


# ---------------------------------------------------------------------------
# Seed config parsing for key_rotation
# ---------------------------------------------------------------------------


class TestSeedKeyRotationParsing:
    def test_parse_key_rotation_full(self, tmp_path: Any) -> None:
        from bernstein.core.seed import parse_seed

        seed_yaml = tmp_path / "bernstein.yaml"
        seed_yaml.write_text(
            "goal: test\n"
            "key_rotation:\n"
            "  interval: 30d\n"
            "  on_leak: revoke_immediately\n"
            "  secrets_provider: vault\n"
            "  secrets_path: secret/bernstein\n"
            "  leak_patterns:\n"
            "    - 'sk-ant-[a-zA-Z0-9]{20,}'\n"
        )
        cfg = parse_seed(seed_yaml)
        assert cfg.key_rotation is not None
        assert cfg.key_rotation.interval_seconds == 2592000
        assert cfg.key_rotation.on_leak == "revoke_immediately"
        assert cfg.key_rotation.secrets_provider == "vault"
        assert cfg.key_rotation.secrets_path == "secret/bernstein"
        assert len(cfg.key_rotation.leak_patterns) == 1

    def test_parse_key_rotation_minimal(self, tmp_path: Any) -> None:
        from bernstein.core.seed import parse_seed

        seed_yaml = tmp_path / "bernstein.yaml"
        seed_yaml.write_text("goal: test\nkey_rotation:\n  interval: 7d\n")
        cfg = parse_seed(seed_yaml)
        assert cfg.key_rotation is not None
        assert cfg.key_rotation.interval_seconds == 604800
        assert cfg.key_rotation.on_leak == "revoke_immediately"

    def test_parse_no_key_rotation(self, tmp_path: Any) -> None:
        from bernstein.core.seed import parse_seed

        seed_yaml = tmp_path / "bernstein.yaml"
        seed_yaml.write_text("goal: test\n")
        cfg = parse_seed(seed_yaml)
        assert cfg.key_rotation is None

    def test_parse_invalid_interval(self, tmp_path: Any) -> None:
        from bernstein.core.seed import SeedError, parse_seed

        seed_yaml = tmp_path / "bernstein.yaml"
        seed_yaml.write_text("goal: test\nkey_rotation:\n  interval: 30x\n")
        with pytest.raises(SeedError, match="key_rotation.interval"):
            parse_seed(seed_yaml)

    def test_parse_invalid_on_leak(self, tmp_path: Any) -> None:
        from bernstein.core.seed import SeedError, parse_seed

        seed_yaml = tmp_path / "bernstein.yaml"
        seed_yaml.write_text("goal: test\nkey_rotation:\n  on_leak: nuke_everything\n")
        with pytest.raises(SeedError, match="key_rotation.on_leak"):
            parse_seed(seed_yaml)

    def test_parse_integer_interval(self, tmp_path: Any) -> None:
        from bernstein.core.seed import parse_seed

        seed_yaml = tmp_path / "bernstein.yaml"
        seed_yaml.write_text("goal: test\nkey_rotation:\n  interval: 3600\n")
        cfg = parse_seed(seed_yaml)
        assert cfg.key_rotation is not None
        assert cfg.key_rotation.interval_seconds == 3600
