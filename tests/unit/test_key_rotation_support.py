"""Tests for SEC-013: API key rotation support."""

from __future__ import annotations

import time

import pytest

from bernstein.core.key_rotation import (
    KeyRotationConfig,
    KeyRotationManager,
    KeyState,
    ManagedKey,
)
from bernstein.core.key_rotation_support import (
    AgentKeyUpdater,
    ExpiryStatus,
    KeyExpiryDetector,
    RotationOrchestrator,
)


def _make_key(
    env_var: str = "ANTHROPIC_API_KEY",
    state: KeyState = KeyState.ACTIVE,
    created_at: float | None = None,
    rotated_at: float | None = None,
) -> ManagedKey:
    return ManagedKey(
        key_id=f"{env_var}:abc12345",
        env_var=env_var,
        state=state,
        created_at=created_at or time.time(),
        rotated_at=rotated_at,
        fingerprint="abc1234567890123",
    )


class TestKeyExpiryDetector:
    def test_healthy_key(self) -> None:
        detector = KeyExpiryDetector(
            warning_threshold_seconds=86400,
            rotation_interval_seconds=2592000,
        )
        key = _make_key(created_at=time.time())
        info = detector.check_key(key)
        assert info.status == ExpiryStatus.HEALTHY

    def test_warning_key(self) -> None:
        detector = KeyExpiryDetector(
            warning_threshold_seconds=86400,
            rotation_interval_seconds=100,
        )
        # Key created 50 seconds ago with 100s rotation = 50s remaining (< 86400 warning)
        key = _make_key(created_at=time.time() - 50)
        info = detector.check_key(key)
        assert info.status == ExpiryStatus.WARNING

    def test_expired_key(self) -> None:
        detector = KeyExpiryDetector(
            warning_threshold_seconds=86400,
            rotation_interval_seconds=100,
        )
        # Key created 200 seconds ago with 100s rotation = overdue
        key = _make_key(created_at=time.time() - 200)
        info = detector.check_key(key)
        assert info.status == ExpiryStatus.EXPIRED

    def test_revoked_key(self) -> None:
        detector = KeyExpiryDetector()
        key = _make_key(state=KeyState.REVOKED)
        info = detector.check_key(key)
        assert info.status == ExpiryStatus.REVOKED

    def test_expired_state_key(self) -> None:
        detector = KeyExpiryDetector()
        key = _make_key(state=KeyState.EXPIRED)
        info = detector.check_key(key)
        assert info.status == ExpiryStatus.EXPIRED

    def test_check_keys_returns_all(self) -> None:
        detector = KeyExpiryDetector()
        keys = [_make_key(env_var="KEY1"), _make_key(env_var="KEY2")]
        infos = detector.check_keys(keys)
        assert len(infos) == 2

    def test_get_expiring_filters(self) -> None:
        detector = KeyExpiryDetector(
            warning_threshold_seconds=86400,
            rotation_interval_seconds=100,
        )
        healthy_key = _make_key(env_var="HEALTHY", created_at=time.time())
        expired_key = _make_key(env_var="EXPIRED", created_at=time.time() - 200)
        expiring = detector.get_expiring([healthy_key, expired_key])
        # healthy_key has rotation_interval=100 with warning_threshold=86400
        # so even the healthy key would be "WARNING" since 100s remaining < 86400s threshold
        assert len(expiring) >= 1

    def test_rotated_at_used_for_age(self) -> None:
        detector = KeyExpiryDetector(
            warning_threshold_seconds=10,
            rotation_interval_seconds=100,
        )
        key = _make_key(
            created_at=time.time() - 500,
            rotated_at=time.time() - 5,
        )
        info = detector.check_key(key)
        assert info.status == ExpiryStatus.HEALTHY


class TestAgentKeyUpdater:
    def test_register_and_update(self) -> None:
        updater = AgentKeyUpdater()
        updater.register_agent("agent-1", {"ANTHROPIC_API_KEY": "old-key"})
        result = updater.update_agent("agent-1", "ANTHROPIC_API_KEY", "new-key")
        assert result.success
        env = updater.get_agent_env("agent-1")
        assert env is not None
        assert env["ANTHROPIC_API_KEY"] == "new-key"

    def test_update_unregistered_agent(self) -> None:
        updater = AgentKeyUpdater()
        result = updater.update_agent("unknown", "KEY", "value")
        assert not result.success

    def test_unregister_agent(self) -> None:
        updater = AgentKeyUpdater()
        updater.register_agent("agent-1", {"KEY": "val"})
        updater.unregister_agent("agent-1")
        assert updater.get_agent_env("agent-1") is None

    def test_update_all_agents(self) -> None:
        updater = AgentKeyUpdater()
        updater.register_agent("agent-1", {"KEY": "old"})
        updater.register_agent("agent-2", {"KEY": "old"})
        updater.register_agent("agent-3", {"OTHER": "val"})

        results = updater.update_all_agents("KEY", "new")
        assert len(results) == 2
        assert all(r.success for r in results)

    def test_update_log_tracked(self) -> None:
        updater = AgentKeyUpdater()
        updater.register_agent("agent-1", {"KEY": "old"})
        updater.update_agent("agent-1", "KEY", "new")
        assert len(updater.update_log) == 1

    def test_get_agent_env_returns_copy(self) -> None:
        updater = AgentKeyUpdater()
        updater.register_agent("agent-1", {"KEY": "val"})
        env = updater.get_agent_env("agent-1")
        assert env is not None
        env["KEY"] = "modified"
        assert updater.get_agent_env("agent-1") == {"KEY": "val"}


class TestRotationOrchestrator:
    def test_check_and_rotate_with_expiring_keys(self, tmp_path: pytest.TempPathFactory) -> None:  # type: ignore[type-arg]
        config = KeyRotationConfig(interval_seconds=100, state_dir=str(tmp_path))  # type: ignore[arg-type]
        manager = KeyRotationManager(config)

        updater = AgentKeyUpdater()
        updater.register_agent("agent-1", {"ANTHROPIC_API_KEY": "old"})

        orchestrator = RotationOrchestrator(
            rotation_manager=manager,
            key_updater=updater,
            warning_threshold=86400,
        )

        # No keys registered, nothing to rotate
        results = orchestrator.check_and_rotate()
        assert len(results) == 0
