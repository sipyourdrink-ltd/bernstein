"""Tests for RealtimeBehaviorMonitor — real-time agent session anomaly detection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.behavior_anomaly import (
    BehaviorAnomalyAction,
    RealtimeBehaviorMonitor,
    _is_suspicious_file,
)


# ---------------------------------------------------------------------------
# _is_suspicious_file helper
# ---------------------------------------------------------------------------


def test_suspicious_file_detects_env_file() -> None:
    assert _is_suspicious_file(".env") is True


def test_suspicious_file_detects_key_file() -> None:
    assert _is_suspicious_file("deploy.key") is True


def test_suspicious_file_detects_pem_file() -> None:
    assert _is_suspicious_file("cert.pem") is True


def test_suspicious_file_detects_aws_credentials() -> None:
    assert _is_suspicious_file("/home/user/.aws/credentials") is True


def test_suspicious_file_detects_ssh_private_key() -> None:
    assert _is_suspicious_file("/home/user/.ssh/id_rsa") is True


def test_suspicious_file_detects_etc_passwd() -> None:
    assert _is_suspicious_file("/etc/passwd") is True


def test_suspicious_file_detects_git_config() -> None:
    assert _is_suspicious_file(".git/config") is True


def test_suspicious_file_allows_env_example() -> None:
    assert _is_suspicious_file(".env.example") is False


def test_suspicious_file_allows_normal_python_file() -> None:
    assert _is_suspicious_file("src/bernstein/core/server.py") is False


def test_suspicious_file_allows_test_fixture_key() -> None:
    # test fixtures may have .key extension but are inside tests/
    assert _is_suspicious_file("tests/fixtures/test.key") is False


# ---------------------------------------------------------------------------
# RealtimeBehaviorMonitor — suspicious file access
# ---------------------------------------------------------------------------


def test_monitor_kills_agent_on_suspicious_file_access(tmp_path: Path) -> None:
    monitor = RealtimeBehaviorMonitor(tmp_path)

    signals = monitor.record_progress(
        "session-1",
        "task-1",
        files_changed=1,
        last_file=".env",
        message="reading config",
    )

    assert len(signals) == 1
    assert signals[0].rule == "suspicious_file_access"
    assert signals[0].action == BehaviorAnomalyAction.KILL_AGENT.value
    assert signals[0].severity == "critical"


def test_monitor_writes_kill_signal_file_on_suspicious_access(tmp_path: Path) -> None:
    monitor = RealtimeBehaviorMonitor(tmp_path)

    monitor.record_progress(
        "session-99",
        "task-99",
        files_changed=1,
        last_file="id_rsa",
        message="",
    )

    kill_file = tmp_path / ".sdd" / "runtime" / "session-99.kill"
    assert kill_file.exists(), "Kill signal file should be written"
    payload = json.loads(kill_file.read_text())
    assert payload["reason"] == "behavior_anomaly"
    assert payload["rule"] == "suspicious_file_access"
    assert payload["requester"] == "realtime_behavior_monitor"


def test_monitor_no_signal_for_normal_file(tmp_path: Path) -> None:
    monitor = RealtimeBehaviorMonitor(tmp_path)

    signals = monitor.record_progress(
        "session-2",
        "task-2",
        files_changed=3,
        last_file="src/bernstein/core/server.py",
        message="edited server.py",
    )

    assert signals == []


# ---------------------------------------------------------------------------
# Output size explosion
# ---------------------------------------------------------------------------


def test_monitor_kills_agent_on_output_size_explosion(tmp_path: Path) -> None:
    """Cumulative output size exceeding the limit triggers a kill signal."""
    monitor = RealtimeBehaviorMonitor(tmp_path, max_output_bytes=100)

    # First update: under limit
    signals = monitor.record_progress(
        "session-3", "task-3", files_changed=0, last_file="", message="x" * 50
    )
    assert signals == []

    # Second update: pushes cumulative total over 100 bytes
    signals = monitor.record_progress(
        "session-3", "task-3", files_changed=0, last_file="", message="x" * 60
    )
    assert len(signals) == 1
    assert signals[0].rule == "output_size_explosion"
    assert signals[0].action == BehaviorAnomalyAction.KILL_AGENT.value


def test_monitor_writes_kill_signal_on_output_explosion(tmp_path: Path) -> None:
    monitor = RealtimeBehaviorMonitor(tmp_path, max_output_bytes=10)

    monitor.record_progress(
        "session-out",
        "task-out",
        files_changed=0,
        last_file="",
        message="x" * 100,
    )

    kill_file = tmp_path / ".sdd" / "runtime" / "session-out.kill"
    assert kill_file.exists()
    payload = json.loads(kill_file.read_text())
    assert payload["rule"] == "output_size_explosion"


# ---------------------------------------------------------------------------
# File-change velocity (statistical)
# ---------------------------------------------------------------------------


def _write_history(metrics_dir: Path, rows: list[dict]) -> None:
    metrics_dir.mkdir(parents=True, exist_ok=True)
    tasks_path = metrics_dir / "tasks.jsonl"
    with tasks_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def test_monitor_logs_signal_for_file_change_velocity_outlier(tmp_path: Path) -> None:
    """Statistically anomalous file-change count emits a log-level signal."""
    _write_history(
        tmp_path / ".sdd" / "metrics",
        [
            {
                "tokens_prompt": 100 + i,
                "tokens_completion": 20,
                "files_modified": 2 + (i % 3),  # varies 2-4 so stddev > 0
                "duration_seconds": 30.0 + i,
            }
            for i in range(15)
        ],
    )
    monitor = RealtimeBehaviorMonitor(tmp_path, sigma_threshold=2.0, min_samples=10)

    signals = monitor.record_progress(
        "session-v",
        "task-v",
        files_changed=100,  # far above baseline mean=2
        last_file="src/app.py",
        message="",
    )

    velocity_signals = [s for s in signals if s.rule == "file_change_velocity"]
    assert len(velocity_signals) == 1
    assert velocity_signals[0].action == BehaviorAnomalyAction.LOG.value


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------


def test_monitor_evict_removes_session(tmp_path: Path) -> None:
    monitor = RealtimeBehaviorMonitor(tmp_path)

    monitor.record_progress("session-x", "task-x", files_changed=1, last_file="a.py", message="")
    assert "session-x" in monitor.active_session_ids()

    monitor.evict_session("session-x")
    assert "session-x" not in monitor.active_session_ids()


def test_monitor_evict_is_idempotent(tmp_path: Path) -> None:
    monitor = RealtimeBehaviorMonitor(tmp_path)
    # Evicting a session that was never tracked should not raise
    monitor.evict_session("nonexistent-session")


def test_monitor_accumulates_output_across_multiple_updates(tmp_path: Path) -> None:
    monitor = RealtimeBehaviorMonitor(tmp_path, max_output_bytes=50)

    # Three small updates each 20 bytes — third one pushes past limit
    for i in range(3):
        signals = monitor.record_progress(
            "session-acc", "task-acc", files_changed=0, last_file="", message="x" * 20
        )
        if i < 2:
            assert all(s.rule != "output_size_explosion" for s in signals)
        else:
            assert any(s.rule == "output_size_explosion" for s in signals)


def test_monitor_tracks_multiple_sessions_independently(tmp_path: Path) -> None:
    monitor = RealtimeBehaviorMonitor(tmp_path, max_output_bytes=100)

    monitor.record_progress("s-a", "t-1", files_changed=0, last_file="", message="a" * 80)
    monitor.record_progress("s-b", "t-2", files_changed=0, last_file="", message="b" * 10)

    # s-a: 80 bytes, s-b: 10 bytes — only s-a is close to limit
    signals_a = monitor.record_progress("s-a", "t-1", files_changed=0, last_file="", message="a" * 30)
    signals_b = monitor.record_progress("s-b", "t-2", files_changed=0, last_file="", message="b" * 5)

    assert any(s.rule == "output_size_explosion" for s in signals_a)
    assert not any(s.rule == "output_size_explosion" for s in signals_b)
