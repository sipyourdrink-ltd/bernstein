"""Unit tests for the token growth monitor."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from bernstein.core.token_monitor import (
    TokenGrowthMonitor,
    TokenSample,
    check_token_growth,
    get_monitor,
    reset_monitor,
)

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.token_monitor import AgentTokenHistory

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_tokens(path: Path, records: list[tuple[float, int, int]]) -> None:
    """Write token records to a sidecar file.

    Args:
        path: Path to the ``.tokens`` file.
        records: List of ``(timestamp, input_tokens, output_tokens)``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        for ts, inp, out in records:
            fh.write(json.dumps({"ts": ts, "in": inp, "out": out}) + "\n")


# ---------------------------------------------------------------------------
# TokenGrowthMonitor.read_tokens
# ---------------------------------------------------------------------------


class TestReadTokens:
    def test_returns_zero_when_no_sidecar(self, tmp_path: Path) -> None:
        monitor = TokenGrowthMonitor()
        assert monitor.read_tokens("sess-001", tmp_path) == 0

    def test_reads_cumulative_total(self, tmp_path: Path) -> None:
        tokens_path = tmp_path / ".sdd" / "runtime" / "sess-001.tokens"
        _write_tokens(tokens_path, [(1000.0, 500, 50), (1030.0, 800, 80)])
        monitor = TokenGrowthMonitor()
        total = monitor.read_tokens("sess-001", tmp_path)
        assert total == 500 + 50 + 800 + 80  # 1430

    def test_incremental_read_does_not_double_count(self, tmp_path: Path) -> None:
        tokens_path = tmp_path / ".sdd" / "runtime" / "sess-abc.tokens"
        _write_tokens(tokens_path, [(1000.0, 100, 10)])
        monitor = TokenGrowthMonitor()
        first = monitor.read_tokens("sess-abc", tmp_path)
        assert first == 110

        # Append a second record
        with tokens_path.open("a") as fh:
            fh.write(json.dumps({"ts": 1030.0, "in": 200, "out": 20}) + "\n")

        second = monitor.read_tokens("sess-abc", tmp_path)
        assert second == 110 + 220  # 330 cumulative

    def test_tolerates_malformed_lines(self, tmp_path: Path) -> None:
        tokens_path = tmp_path / ".sdd" / "runtime" / "sess-bad.tokens"
        tokens_path.parent.mkdir(parents=True, exist_ok=True)
        tokens_path.write_text('not json\n{"ts": 1.0, "in": 100, "out": 10}\n')
        monitor = TokenGrowthMonitor()
        total = monitor.read_tokens("sess-bad", tmp_path)
        assert total == 110  # malformed line skipped


# ---------------------------------------------------------------------------
# TokenGrowthMonitor.is_quadratic_growth
# ---------------------------------------------------------------------------


class TestQuadraticGrowth:
    def test_returns_false_with_insufficient_samples(self) -> None:
        monitor = TokenGrowthMonitor()
        monitor._history["s1"] = _make_history("s1", [100, 200])
        assert not monitor.is_quadratic_growth("s1")

    def test_returns_false_for_linear_growth(self) -> None:
        monitor = TokenGrowthMonitor()
        # Constant deltas of 100 — linear
        monitor._history["s1"] = _make_history("s1", [100, 200, 300, 400])
        assert not monitor.is_quadratic_growth("s1")

    def test_detects_quadratic_growth(self) -> None:
        monitor = TokenGrowthMonitor(quadratic_ratio=2.0)
        # Deltas: 100, 300 — second delta is 3x first → quadratic
        monitor._history["s1"] = _make_history("s1", [100, 200, 500])
        assert monitor.is_quadratic_growth("s1")

    def test_no_false_positive_when_prev_delta_zero(self) -> None:
        monitor = TokenGrowthMonitor()
        # Flat start then jump — prev delta zero, should not divide by zero
        monitor._history["s1"] = _make_history("s1", [100, 100, 500])
        # prev delta = 0, function should return False
        assert not monitor.is_quadratic_growth("s1")


def _make_history(session_id: str, totals: list[int]) -> AgentTokenHistory:
    from bernstein.core.token_monitor import AgentTokenHistory

    h = AgentTokenHistory(session_id=session_id)
    for i, total in enumerate(totals):
        h.samples.append(TokenSample(timestamp=float(i * 30), total_tokens=total))
    return h


# ---------------------------------------------------------------------------
# TokenGrowthMonitor.should_kill
# ---------------------------------------------------------------------------


class TestShouldKill:
    def test_no_kill_below_threshold(self) -> None:
        monitor = TokenGrowthMonitor(kill_threshold=50_000)
        monitor._history["s1"] = _make_history("s1", [10_000])
        assert not monitor.should_kill("s1", files_changed=0)

    def test_no_kill_when_files_changed(self) -> None:
        monitor = TokenGrowthMonitor(kill_threshold=50_000)
        monitor._history["s1"] = _make_history("s1", [60_000])
        assert not monitor.should_kill("s1", files_changed=5)

    def test_kills_when_threshold_exceeded_and_no_files(self) -> None:
        monitor = TokenGrowthMonitor(kill_threshold=50_000)
        monitor._history["s1"] = _make_history("s1", [50_001])
        assert monitor.should_kill("s1", files_changed=0)

    def test_no_double_kill(self) -> None:
        monitor = TokenGrowthMonitor(kill_threshold=50_000)
        monitor._history["s1"] = _make_history("s1", [60_000])
        monitor.mark_killed("s1")
        assert not monitor.should_kill("s1", files_changed=0)

    def test_no_kill_when_no_snapshot_data(self) -> None:
        """files_changed=-1 means no snapshot data yet; should not kill."""
        monitor = TokenGrowthMonitor(kill_threshold=50_000)
        monitor._history["s1"] = _make_history("s1", [60_000])
        assert not monitor.should_kill("s1", files_changed=-1)


# ---------------------------------------------------------------------------
# Singleton helpers
# ---------------------------------------------------------------------------


def test_get_monitor_returns_singleton() -> None:
    reset_monitor()
    m1 = get_monitor()
    m2 = get_monitor()
    assert m1 is m2


def test_reset_monitor_creates_fresh_instance() -> None:
    m1 = get_monitor()
    reset_monitor()
    m2 = get_monitor()
    assert m1 is not m2


# ---------------------------------------------------------------------------
# check_token_growth integration (orchestrator hook)
# ---------------------------------------------------------------------------


class TestCheckTokenGrowth:
    def _make_orch(self, tmp_path: Path, sessions: dict) -> MagicMock:
        orch = MagicMock()
        orch._workdir = tmp_path
        orch._config.server_url = "http://localhost:8052"
        orch._agents = sessions
        orch._spawn_ts = time.time()
        orch._router.get_provider_max_context_tokens.return_value = 200_000
        return orch

    def _make_session(
        self,
        session_id: str,
        status: str = "working",
        *,
        provider: str | None = "anthropic",
    ) -> MagicMock:
        s = MagicMock()
        s.id = session_id
        s.status = status
        s.task_ids = ["task-001"]
        s.spawn_ts = time.time()
        s.provider = provider
        s.tokens_used = 0
        s.token_budget = 0  # unlimited — no nudge interference
        s.context_window_tokens = 0
        s.context_utilization_pct = 0.0
        s.context_utilization_alert = False
        return s

    def test_updates_tokens_used_on_session(self, tmp_path: Path) -> None:
        reset_monitor()
        sess = self._make_session("sess-live")
        tokens_path = tmp_path / ".sdd" / "runtime" / "sess-live.tokens"
        _write_tokens(tokens_path, [(time.time(), 1000, 100)])

        # Snapshots return empty (no file change data)
        resp = MagicMock()
        resp.json.return_value = []
        resp.raise_for_status = MagicMock()

        orch = self._make_orch(tmp_path, {"sess-live": sess})
        orch._client.get.return_value = resp

        check_token_growth(orch)
        assert sess.tokens_used == 1100
        assert sess.context_window_tokens == 200_000
        assert sess.context_utilization_pct == pytest.approx(0.55)
        assert sess.context_utilization_alert is False

    def test_marks_context_window_alert_above_threshold(self, tmp_path: Path) -> None:
        reset_monitor()
        sess = self._make_session("sess-context")
        tokens_path = tmp_path / ".sdd" / "runtime" / "sess-context.tokens"
        _write_tokens(tokens_path, [(time.time(), 170_000, 0)])

        resp = MagicMock()
        resp.json.return_value = []
        resp.raise_for_status = MagicMock()

        orch = self._make_orch(tmp_path, {"sess-context": sess})
        orch._client.get.return_value = resp

        check_token_growth(orch)

        assert sess.context_window_tokens == 200_000
        assert sess.context_utilization_pct == pytest.approx(85.0)
        assert sess.context_utilization_alert is True

    def test_kills_runaway_agent(self, tmp_path: Path) -> None:
        reset_monitor()
        sess = self._make_session("sess-runaway")
        # Write a token record far above the kill threshold
        tokens_path = tmp_path / ".sdd" / "runtime" / "sess-runaway.tokens"
        _write_tokens(tokens_path, [(time.time(), 51_000, 1_000)])

        # Snapshots exist and show zero file changes
        snap_resp = MagicMock()
        snap_resp.raise_for_status = MagicMock()
        snap_resp.json.return_value = [{"timestamp": time.time(), "files_changed": 0}]

        orch = self._make_orch(tmp_path, {"sess-runaway": sess})
        orch._client.get.return_value = snap_resp

        check_token_growth(orch)

        orch._spawner.kill.assert_called_once_with(sess)
        assert sess.status == "dead"

    def test_does_not_kill_when_files_changed(self, tmp_path: Path) -> None:
        reset_monitor()
        sess = self._make_session("sess-productive")
        tokens_path = tmp_path / ".sdd" / "runtime" / "sess-productive.tokens"
        _write_tokens(tokens_path, [(time.time(), 51_000, 1_000)])

        snap_resp = MagicMock()
        snap_resp.raise_for_status = MagicMock()
        snap_resp.json.return_value = [{"timestamp": time.time(), "files_changed": 3}]

        orch = self._make_orch(tmp_path, {"sess-productive": sess})
        orch._client.get.return_value = snap_resp

        check_token_growth(orch)

        orch._spawner.kill.assert_not_called()
        assert sess.status == "working"

    def test_skips_dead_sessions(self, tmp_path: Path) -> None:
        reset_monitor()
        sess = self._make_session("sess-dead", status="dead")
        orch = self._make_orch(tmp_path, {"sess-dead": sess})

        check_token_growth(orch)
        orch._spawner.kill.assert_not_called()

    def test_emits_wakeup_on_quadratic_growth(self, tmp_path: Path) -> None:
        reset_monitor()
        monitor = get_monitor()
        # Pre-load quadratic history (deltas: 100, 300)
        from bernstein.core.token_monitor import AgentTokenHistory

        h = AgentTokenHistory(session_id="sess-quad")
        h.samples = [
            TokenSample(timestamp=0.0, total_tokens=100),
            TokenSample(timestamp=30.0, total_tokens=200),
            TokenSample(timestamp=60.0, total_tokens=500),
        ]
        monitor._history["sess-quad"] = h

        sess = self._make_session("sess-quad")
        tokens_path = tmp_path / ".sdd" / "runtime" / "sess-quad.tokens"
        # No new tokens this tick
        tokens_path.parent.mkdir(parents=True, exist_ok=True)
        tokens_path.write_text("")

        snap_resp = MagicMock()
        snap_resp.raise_for_status = MagicMock()
        snap_resp.json.return_value = [{"timestamp": time.time(), "files_changed": 2}]

        orch = self._make_orch(tmp_path, {"sess-quad": sess})
        orch._client.get.return_value = snap_resp

        check_token_growth(orch)

        orch._signal_mgr.write_wakeup.assert_called_once()
        assert monitor.was_warned("sess-quad")

    def test_wakeup_only_once_for_quadratic(self, tmp_path: Path) -> None:
        reset_monitor()
        monitor = get_monitor()
        from bernstein.core.token_monitor import AgentTokenHistory

        h = AgentTokenHistory(session_id="sess-quad2")
        h.samples = [
            TokenSample(timestamp=0.0, total_tokens=100),
            TokenSample(timestamp=30.0, total_tokens=200),
            TokenSample(timestamp=60.0, total_tokens=500),
        ]
        monitor._history["sess-quad2"] = h

        sess = self._make_session("sess-quad2")
        tokens_path = tmp_path / ".sdd" / "runtime" / "sess-quad2.tokens"
        tokens_path.parent.mkdir(parents=True, exist_ok=True)
        tokens_path.write_text("")

        snap_resp = MagicMock()
        snap_resp.raise_for_status = MagicMock()
        snap_resp.json.return_value = [{"timestamp": time.time(), "files_changed": 2}]

        orch = self._make_orch(tmp_path, {"sess-quad2": sess})
        orch._client.get.return_value = snap_resp

        check_token_growth(orch)
        check_token_growth(orch)

        # write_wakeup should only be called once
        assert orch._signal_mgr.write_wakeup.call_count == 1


# ---------------------------------------------------------------------------
# audit-070: per-tenant kill threshold + warn reset
# ---------------------------------------------------------------------------


class TestPerTenantKillThreshold:
    """Two tenants with different kill thresholds resolve independently."""

    def test_kill_threshold_for_uses_instance_map(self) -> None:
        monitor = TokenGrowthMonitor(
            kill_threshold=50_000,
            tenant_kill_thresholds={"enterprise": 200_000, "free": 20_000},
        )
        assert monitor.kill_threshold_for("enterprise") == 200_000
        assert monitor.kill_threshold_for("free") == 20_000

    def test_kill_threshold_for_unknown_tenant_falls_back(self) -> None:
        monitor = TokenGrowthMonitor(
            kill_threshold=50_000,
            tenant_kill_thresholds={"enterprise": 200_000},
        )
        # Unknown tenant with no explicit "default" key → module-wide default.
        assert monitor.kill_threshold_for("unknown") == 50_000

    def test_kill_threshold_for_uses_default_key(self) -> None:
        monitor = TokenGrowthMonitor(
            kill_threshold=50_000,
            tenant_kill_thresholds={"default": 10_000, "enterprise": 200_000},
        )
        assert monitor.kill_threshold_for("whoever") == 10_000
        assert monitor.kill_threshold_for(None) == 10_000
        assert monitor.kill_threshold_for("enterprise") == 200_000

    def test_token_cfg_module_override_takes_priority(self) -> None:
        import bernstein.core.token_monitor as tm

        original = dict(tm.TOKEN_CFG)
        try:
            tm.TOKEN_CFG.clear()
            tm.TOKEN_CFG.update({"enterprise": 300_000, "free": 15_000})
            monitor = TokenGrowthMonitor(
                kill_threshold=50_000,
                tenant_kill_thresholds={"enterprise": 200_000},  # overridden by TOKEN_CFG
            )
            assert monitor.kill_threshold_for("enterprise") == 300_000
            assert monitor.kill_threshold_for("free") == 15_000
        finally:
            tm.TOKEN_CFG.clear()
            tm.TOKEN_CFG.update(original)

    def test_should_kill_respects_tenant_threshold(self) -> None:
        monitor = TokenGrowthMonitor(
            kill_threshold=50_000,
            tenant_kill_thresholds={"enterprise": 200_000, "free": 20_000},
        )
        # 60k tokens: over "free" (20k) but under "enterprise" (200k).
        monitor._history["sess-free"] = _make_history("sess-free", [60_000])
        monitor._history["sess-ent"] = _make_history("sess-ent", [60_000])

        assert monitor.should_kill("sess-free", files_changed=0, tenant_id="free")
        assert not monitor.should_kill("sess-ent", files_changed=0, tenant_id="enterprise")

    def test_should_kill_without_tenant_uses_constructor_default(self) -> None:
        monitor = TokenGrowthMonitor(
            kill_threshold=50_000,
            tenant_kill_thresholds={"enterprise": 200_000},
        )
        monitor._history["s1"] = _make_history("s1", [51_000])
        # No tenant_id → falls back through TOKEN_CFG default → monitor default.
        assert monitor.should_kill("s1", files_changed=0)


class TestWarnQuadraticReset:
    """``warned_quadratic`` clears after enough consecutive clean samples."""

    def test_note_clean_sample_resets_after_threshold(self) -> None:
        monitor = TokenGrowthMonitor(warn_reset_clean_samples=10)
        monitor.mark_warned("s1")
        assert monitor.was_warned("s1")

        for _ in range(9):
            monitor.note_clean_sample("s1")
            assert monitor.was_warned("s1"), "should still be warned before reset threshold"

        # 10th clean sample clears the flag.
        monitor.note_clean_sample("s1")
        assert not monitor.was_warned("s1")

    def test_clean_sample_counter_resets_on_new_warning(self) -> None:
        monitor = TokenGrowthMonitor(warn_reset_clean_samples=10)
        monitor.mark_warned("s1")
        for _ in range(5):
            monitor.note_clean_sample("s1")
        # New warning fires → counter resets.
        monitor.mark_warned("s1")
        # Only 9 clean samples; still warned.
        for _ in range(9):
            monitor.note_clean_sample("s1")
        assert monitor.was_warned("s1")
        # 10th clean sample finally clears it.
        monitor.note_clean_sample("s1")
        assert not monitor.was_warned("s1")

    def test_warning_refires_after_reset(self, tmp_path: Path) -> None:
        """End-to-end: quadratic warning fires, clears, then fires again."""
        reset_monitor()
        monitor = get_monitor()
        from bernstein.core.token_monitor import AgentTokenHistory

        # Pre-load quadratic history so the first tick fires the warning.
        h = AgentTokenHistory(session_id="sess-refire")
        h.samples = [
            TokenSample(timestamp=0.0, total_tokens=100),
            TokenSample(timestamp=30.0, total_tokens=200),
            TokenSample(timestamp=60.0, total_tokens=500),
        ]
        monitor._history["sess-refire"] = h

        orch = MagicMock()
        orch._workdir = tmp_path
        orch._config.server_url = "http://localhost:8052"
        orch._router.get_provider_max_context_tokens.return_value = 200_000

        sess = MagicMock()
        sess.id = "sess-refire"
        sess.status = "working"
        sess.task_ids = ["t1"]
        sess.spawn_ts = time.time()
        sess.provider = "anthropic"
        sess.tokens_used = 0
        sess.token_budget = 0
        sess.context_window_tokens = 0
        sess.context_utilization_pct = 0.0
        sess.context_utilization_alert = False
        sess.tenant_id = "default"
        orch._agents = {"sess-refire": sess}

        snap_resp = MagicMock()
        snap_resp.raise_for_status = MagicMock()
        snap_resp.json.return_value = [{"timestamp": time.time(), "files_changed": 2}]
        orch._client.get.return_value = snap_resp

        # Empty sidecar so no new samples are appended during ticks.
        tokens_path = tmp_path / ".sdd" / "runtime" / "sess-refire.tokens"
        tokens_path.parent.mkdir(parents=True, exist_ok=True)
        tokens_path.write_text("")

        # Tick 1: quadratic growth still visible → warning fires.
        check_token_growth(orch)
        assert monitor.was_warned("sess-refire")
        assert orch._signal_mgr.write_wakeup.call_count == 1

        # Flatten history so is_quadratic_growth returns False.
        h.samples = [
            TokenSample(timestamp=90.0, total_tokens=510),
            TokenSample(timestamp=120.0, total_tokens=520),
            TokenSample(timestamp=150.0, total_tokens=530),
        ]

        # Drive 10 clean ticks → warning clears.
        for _ in range(10):
            check_token_growth(orch)
        assert not monitor.was_warned("sess-refire")

        # Re-introduce quadratic growth → warning fires again.
        h.samples = [
            TokenSample(timestamp=200.0, total_tokens=1_000),
            TokenSample(timestamp=230.0, total_tokens=1_100),
            TokenSample(timestamp=260.0, total_tokens=1_500),
        ]
        check_token_growth(orch)
        assert monitor.was_warned("sess-refire")
        assert orch._signal_mgr.write_wakeup.call_count == 2


class TestCheckTokenGrowthTenantResolution:
    """``check_token_growth`` uses the session's tenant_id for threshold resolution."""

    def _make_orch(self, tmp_path: Path, sessions: dict) -> MagicMock:
        orch = MagicMock()
        orch._workdir = tmp_path
        orch._config.server_url = "http://localhost:8052"
        orch._agents = sessions
        orch._router.get_provider_max_context_tokens.return_value = 200_000
        return orch

    def _make_session(self, session_id: str, tenant_id: str) -> MagicMock:
        s = MagicMock()
        s.id = session_id
        s.status = "working"
        s.task_ids = ["t1"]
        s.spawn_ts = time.time()
        s.provider = "anthropic"
        s.tokens_used = 0
        s.token_budget = 0
        s.context_window_tokens = 0
        s.context_utilization_pct = 0.0
        s.context_utilization_alert = False
        s.tenant_id = tenant_id
        return s

    def test_two_tenants_resolve_independent_thresholds(self, tmp_path: Path) -> None:
        reset_monitor()
        monitor = get_monitor()
        monitor.tenant_kill_thresholds = {"enterprise": 200_000, "free": 20_000}

        free_sess = self._make_session("sess-free", "free")
        ent_sess = self._make_session("sess-ent", "enterprise")

        # Both agents consume ~60k tokens with zero files changed.
        for sid in ("sess-free", "sess-ent"):
            tokens_path = tmp_path / ".sdd" / "runtime" / f"{sid}.tokens"
            _write_tokens(tokens_path, [(time.time(), 55_000, 5_000)])

        snap_resp = MagicMock()
        snap_resp.raise_for_status = MagicMock()
        snap_resp.json.return_value = [{"timestamp": time.time(), "files_changed": 0}]

        orch = self._make_orch(
            tmp_path,
            {"sess-free": free_sess, "sess-ent": ent_sess},
        )
        orch._client.get.return_value = snap_resp

        check_token_growth(orch)

        # Free tier (20k threshold) is killed; enterprise (200k) survives.
        kill_calls = [c.args[0] for c in orch._spawner.kill.call_args_list]
        assert free_sess in kill_calls
        assert ent_sess not in kill_calls
        assert free_sess.status == "dead"
        assert ent_sess.status == "working"
