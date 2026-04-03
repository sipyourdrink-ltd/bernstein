"""Tests for unattended retry policy and mode detection."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bernstein.adapters.base import SpawnError, SpawnResult
from bernstein.core.models import Task
from bernstein.core.rate_limit_tracker import (
    UnattendedRetryPolicy,
    is_unattended_mode,
)
from bernstein.core.spawner import AgentSpawner

# ---------------------------------------------------------------------------
# is_unattended_mode tests
# ---------------------------------------------------------------------------


class TestIsUnattendedMode:
    def test_false_by_default(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            assert is_unattended_mode() is False

    def test_true_with_1(self) -> None:
        with patch.dict("os.environ", {"BERNSTEIN_UNATTENDED": "1"}):
            assert is_unattended_mode() is True

    def test_true_with_true(self) -> None:
        with patch.dict("os.environ", {"BERNSTEIN_UNATTENDED": "true"}):
            assert is_unattended_mode() is True

    def test_true_with_yes(self) -> None:
        with patch.dict("os.environ", {"BERNSTEIN_UNATTENDED": "yes"}):
            assert is_unattended_mode() is True

    def test_true_with_uppercase(self) -> None:
        with patch.dict("os.environ", {"BERNSTEIN_UNATTENDED": "TRUE"}):
            assert is_unattended_mode() is True

    def test_false_with_0(self) -> None:
        with patch.dict("os.environ", {"BERNSTEIN_UNATTENDED": "0"}):
            assert is_unattended_mode() is False

    def test_false_with_random_value(self) -> None:
        with patch.dict("os.environ", {"BERNSTEIN_UNATTENDED": "something"}):
            assert is_unattended_mode() is False


# ---------------------------------------------------------------------------
# UnattendedRetryPolicy tests
# ---------------------------------------------------------------------------


class TestUnattendedRetryPolicyShouldRetry:
    def test_429_is_retriable(self) -> None:
        policy = UnattendedRetryPolicy()
        assert policy.should_retry(1, 429) is True

    def test_529_is_retriable(self) -> None:
        policy = UnattendedRetryPolicy()
        assert policy.should_retry(1, 529) is True

    def test_500_is_not_retriable(self) -> None:
        policy = UnattendedRetryPolicy()
        assert policy.should_retry(1, 500) is False

    def test_404_is_not_retriable(self) -> None:
        policy = UnattendedRetryPolicy()
        assert policy.should_retry(1, 404) is False

    def test_exceeds_max_retries(self) -> None:
        policy = UnattendedRetryPolicy(max_retries=3)
        assert policy.should_retry(1, 429) is True
        assert policy.should_retry(2, 429) is True
        assert policy.should_retry(3, 429) is False

    def test_at_max_retries_boundary(self) -> None:
        policy = UnattendedRetryPolicy(max_retries=2)
        assert policy.should_retry(1, 429) is True
        assert policy.should_retry(2, 429) is False


class TestUnattendedRetryPolicyNextDelay:
    def test_first_attempt(self) -> None:
        policy = UnattendedRetryPolicy(base_delay=5.0, max_delay=300.0)
        assert policy.next_delay(1) == 5.0

    def test_exponential_backoff(self) -> None:
        policy = UnattendedRetryPolicy(base_delay=5.0, max_delay=300.0)
        assert policy.next_delay(1) == 5.0
        assert policy.next_delay(2) == 10.0
        assert policy.next_delay(3) == 20.0
        assert policy.next_delay(4) == 40.0

    def test_capped_at_max_delay(self) -> None:
        policy = UnattendedRetryPolicy(base_delay=5.0, max_delay=30.0)
        assert policy.next_delay(1) == 5.0
        assert policy.next_delay(2) == 10.0
        assert policy.next_delay(3) == 20.0
        assert policy.next_delay(4) == 30.0  # would be 40, capped
        assert policy.next_delay(5) == 30.0

    def test_custom_delays(self) -> None:
        policy = UnattendedRetryPolicy(base_delay=10.0, max_delay=1000.0)
        assert policy.next_delay(1) == 10.0
        assert policy.next_delay(2) == 20.0
        assert policy.next_delay(3) == 40.0


class TestUnattendedRetryPolicyEmitHeartbeat:
    def test_writes_heartbeat_file(self, tmp_path: Path) -> None:
        policy = UnattendedRetryPolicy()
        signals_dir = tmp_path / ".sdd" / "runtime" / "signals"
        policy.emit_heartbeat("sess-001", 1, "429 rate limit", signals_dir=signals_dir)
        hb_file = signals_dir / "sess-001" / "HEARTBEAT"
        assert hb_file.exists()
        content = hb_file.read_text()
        assert "Attempt: 1" in content
        assert "429 rate limit" in content
        assert "Timestamp:" in content

    def test_logs_heartbeat(self, caplog: pytest.LogCaptureFixture) -> None:
        policy = UnattendedRetryPolicy()
        with patch("pathlib.Path.mkdir"), patch("pathlib.Path.write_text"):
            with caplog.at_level(logging.INFO):
                policy.emit_heartbeat("sess-002", 2, "529 overload")
        assert any("Unattended retry heartbeat" in r.message for r in caplog.records)

    def test_handles_os_error_gracefully(self) -> None:
        policy = UnattendedRetryPolicy()
        signals_dir = Path("/nonexistent/dir/signals")
        # Should not raise
        policy.emit_heartbeat("sess-004", 1, "test", signals_dir=signals_dir)


class TestUnattendedRetryPolicyWaitWithHeartbeats:
    def test_emits_heartbeats_during_wait(self, tmp_path: Path) -> None:
        policy = UnattendedRetryPolicy(
            base_delay=0.1,
            max_delay=0.5,
            heartbeat_interval=0.05,
        )
        signals_dir = tmp_path / "signals"
        policy.wait_with_heartbeats("sess-wait", 1, "test", signals_dir=signals_dir)
        hb_file = signals_dir / "sess-wait" / "HEARTBEAT"
        assert hb_file.exists()

    def test_respects_max_delay_cap(self, tmp_path: Path) -> None:
        policy = UnattendedRetryPolicy(
            base_delay=100.0,
            max_delay=0.05,
            heartbeat_interval=0.02,
        )
        signals_dir = tmp_path / "signals2"
        start = time.monotonic()
        policy.wait_with_heartbeats("sess-cap", 1, "test", signals_dir=signals_dir)
        elapsed = time.monotonic() - start
        # Should complete in roughly 0.05s (the max_delay), not 100s
        assert elapsed < 2.0


class TestRetryableStatusCodes:
    def test_429_is_tracked(self) -> None:
        policy = UnattendedRetryPolicy()
        assert policy.should_retry(1, 429) is True

    def test_529_is_tracked(self) -> None:
        policy = UnattendedRetryPolicy()
        assert policy.should_retry(1, 529) is True

    def test_500_not_tracked(self) -> None:
        policy = UnattendedRetryPolicy()
        assert policy.should_retry(1, 500) is False

    def test_404_not_tracked(self) -> None:
        policy = UnattendedRetryPolicy()
        assert policy.should_retry(1, 404) is False


class TestUnattendedSpawnerRetry:
    """Integration-style tests: spawner retries 429 rate-limit errors in unattended mode."""

    def test_spawner_retries_in_unattended_mode_on_429(self, tmp_path: Path) -> None:
        """When BERNSTEIN_UNATTENDED=1 and all providers return 429, the spawner retries with backoff."""
        with patch.dict("os.environ", {"BERNSTEIN_UNATTENDED": "1"}):
            adapter = MagicMock()
            adapter.name.return_value = "test-adapter"
            # First two calls raise SpawnError, third succeeds
            success_result = SpawnResult(pid=42, log_path=tmp_path / "test.log")
            adapter.spawn.side_effect = [
                SpawnError("429 Too Many Requests"),
                SpawnError("429 Too Many Requests"),
                success_result,
            ]
            adapter.supports_auth_refresh.return_value = False

            templates_dir = tmp_path / "templates"
            backend_dir = templates_dir / "backend"
            backend_dir.mkdir(parents=True)
            (backend_dir / "system_prompt.md").write_text("You are a backend agent.")

            # Use a short delay policy for fast tests
            mock_policy = MagicMock()
            mock_policy.max_retries = 5
            with patch(
                "bernstein.core.rate_limit_tracker.UnattendedRetryPolicy",
                return_value=mock_policy,
            ):
                spawner = _make_minimal_spawner(adapter, templates_dir, tmp_path)
                tasks = [Task(id="T1", title="test", role="backend", description="test")]

                with _mock_path_exists(tmp_path):
                    session = spawner.spawn_for_tasks(tasks)

                assert session.pid == 42
                assert adapter.spawn.call_count == 3

    def test_spawner_raises_after_max_retries_in_unattended_mode(self, tmp_path: Path) -> None:
        """When all retry attempts are exhausted, the spawner raises."""
        with patch.dict("os.environ", {"BERNSTEIN_UNATTENDED": "1"}):
            adapter = MagicMock()
            adapter.name.return_value = "test-adapter"
            adapter.spawn.side_effect = SpawnError("429 rate limited")
            adapter.supports_auth_refresh.return_value = False

            templates_dir = tmp_path / "templates"
            backend_dir = templates_dir / "backend"
            backend_dir.mkdir(parents=True)
            (backend_dir / "system_prompt.md").write_text("You are a backend agent.")

            mock_policy = MagicMock()
            mock_policy.max_retries = 2
            with patch(
                "bernstein.core.rate_limit_tracker.UnattendedRetryPolicy",
                return_value=mock_policy,
            ):
                spawner = _make_minimal_spawner(adapter, templates_dir, tmp_path)
                tasks = [Task(id="T1", title="test", role="backend", description="test")]

                with _mock_path_exists(tmp_path):
                    with pytest.raises(RuntimeError, match="All spawn attempts failed"):
                        spawner.spawn_for_tasks(tasks)

    def test_no_retry_when_not_in_unattended_mode(self, tmp_path: Path) -> None:
        """When unattended mode is OFF, rate-limit errors raise immediately."""
        with patch.dict("os.environ", {}, clear=True):
            adapter = MagicMock()
            adapter.name.return_value = "test-adapter"
            adapter.spawn.side_effect = SpawnError("429 rate limited")
            adapter.supports_auth_refresh.return_value = False

            templates_dir = tmp_path / "templates"
            backend_dir = templates_dir / "backend"
            backend_dir.mkdir(parents=True)
            (backend_dir / "system_prompt.md").write_text("You are a backend agent.")

            spawner = _make_minimal_spawner(adapter, templates_dir, tmp_path)
            tasks = [Task(id="T1", title="test", role="backend", description="test")]

            with _mock_path_exists(tmp_path):
                with pytest.raises(RuntimeError, match="All spawn attempts failed"):
                    spawner.spawn_for_tasks(tasks)

            assert adapter.spawn.call_count == 1


def _make_minimal_spawner(
    adapter: MagicMock,
    templates_dir: Path,
    tmp_path: Path,
) -> AgentSpawner:
    spawner = AgentSpawner(adapter, templates_dir, tmp_path)
    spawner._get_adapter_by_name = MagicMock(return_value=adapter)  # pyright: ignore[reportPrivateUsage]
    spawner._infer_adapter_name_for_provider = MagicMock(return_value="test-adapter")  # pyright: ignore[reportPrivateUsage]
    spawner._rate_limit_tracker = None  # pyright: ignore[reportAttributeAccessIssue]
    return spawner


def _mock_path_exists(workdir: Path):
    orig_exists = Path.exists

    def mock_exists(self: Path) -> bool:
        if ".log" in str(self) or "signals" in str(self):
            return True
        return orig_exists(self)

    return patch("pathlib.Path.exists", mock_exists)
