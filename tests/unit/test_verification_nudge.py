"""Tests for the verification nudge system.

Covers VerificationRecord, VerificationNudgeTracker, NudgeSummary,
JSONL persistence, threshold evaluation, and the load_nudge_summary helper.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.verification_nudge import (
    NudgeSummary,
    VerificationNudgeTracker,
    VerificationRecord,
    load_nudge_summary,
)

# ---------------------------------------------------------------------------
# VerificationRecord
# ---------------------------------------------------------------------------


class TestVerificationRecord:
    """Tests for VerificationRecord dataclass."""

    def test_verified_when_tests_run(self) -> None:
        rec = VerificationRecord(
            task_id="t1",
            session_id="s1",
            timestamp=1000.0,
            tests_run=True,
            quality_gates_run=False,
            completion_signals_checked=False,
            verified=True,
        )
        assert rec.verified is True

    def test_verified_when_quality_gates_run(self) -> None:
        rec = VerificationRecord(
            task_id="t2",
            session_id="s2",
            timestamp=1000.0,
            tests_run=False,
            quality_gates_run=True,
            completion_signals_checked=False,
            verified=True,
        )
        assert rec.verified is True

    def test_verified_when_completion_signals_checked(self) -> None:
        rec = VerificationRecord(
            task_id="t3",
            session_id="s3",
            timestamp=1000.0,
            tests_run=False,
            quality_gates_run=False,
            completion_signals_checked=True,
            verified=True,
        )
        assert rec.verified is True

    def test_unverified_when_nothing_run(self) -> None:
        rec = VerificationRecord(
            task_id="t4",
            session_id="s4",
            timestamp=1000.0,
            tests_run=False,
            quality_gates_run=False,
            completion_signals_checked=False,
            verified=False,
        )
        assert rec.verified is False

    def test_to_dict_roundtrip(self) -> None:
        rec = VerificationRecord(
            task_id="t5",
            session_id="s5",
            timestamp=1234.5,
            tests_run=True,
            quality_gates_run=False,
            completion_signals_checked=True,
            verified=True,
        )
        data = rec.to_dict()
        restored = VerificationRecord.from_dict(data)
        assert restored.task_id == rec.task_id
        assert restored.session_id == rec.session_id
        assert restored.timestamp == rec.timestamp
        assert restored.tests_run == rec.tests_run
        assert restored.quality_gates_run == rec.quality_gates_run
        assert restored.completion_signals_checked == rec.completion_signals_checked
        assert restored.verified == rec.verified

    def test_from_dict_defaults(self) -> None:
        rec = VerificationRecord.from_dict({})
        assert rec.task_id == ""
        assert rec.session_id == ""
        assert rec.timestamp == 0.0
        assert rec.tests_run is False
        assert rec.quality_gates_run is False
        assert rec.verified is False


# ---------------------------------------------------------------------------
# VerificationNudgeTracker
# ---------------------------------------------------------------------------


class TestVerificationNudgeTracker:
    """Tests for VerificationNudgeTracker."""

    def test_record_verified_task(self, tmp_path: Path) -> None:
        tracker = VerificationNudgeTracker(metrics_dir=tmp_path)
        rec = tracker.record(
            task_id="t1",
            session_id="s1",
            tests_run=True,
        )
        assert rec.verified is True
        assert rec.tests_run is True
        assert len(tracker.records) == 1

    def test_record_unverified_task(self, tmp_path: Path) -> None:
        tracker = VerificationNudgeTracker(metrics_dir=tmp_path)
        rec = tracker.record(
            task_id="t2",
            session_id="s2",
            tests_run=False,
            quality_gates_run=False,
            completion_signals_checked=False,
        )
        assert rec.verified is False
        assert len(tracker.records) == 1

    def test_is_task_recorded(self, tmp_path: Path) -> None:
        tracker = VerificationNudgeTracker(metrics_dir=tmp_path)
        assert tracker.is_task_recorded("t1") is False
        tracker.record(task_id="t1", session_id="s1", tests_run=True)
        assert tracker.is_task_recorded("t1") is True
        assert tracker.is_task_recorded("t2") is False

    def test_reset_clears_records(self, tmp_path: Path) -> None:
        tracker = VerificationNudgeTracker(metrics_dir=tmp_path)
        tracker.record(task_id="t1", session_id="s1", tests_run=True)
        tracker.record(task_id="t2", session_id="s2", tests_run=False)
        assert len(tracker.records) == 2
        tracker.reset()
        assert len(tracker.records) == 0

    def test_summary_empty(self, tmp_path: Path) -> None:
        tracker = VerificationNudgeTracker(metrics_dir=tmp_path)
        summary = tracker.summary()
        assert summary.total_completions == 0
        assert summary.verified_count == 0
        assert summary.unverified_count == 0
        assert summary.unverified_ratio == 0.0
        assert summary.threshold_exceeded is False
        assert summary.recent_unverified == []

    def test_summary_all_verified(self, tmp_path: Path) -> None:
        tracker = VerificationNudgeTracker(metrics_dir=tmp_path)
        for i in range(5):
            tracker.record(task_id=f"t{i}", session_id=f"s{i}", tests_run=True)
        summary = tracker.summary()
        assert summary.total_completions == 5
        assert summary.verified_count == 5
        assert summary.unverified_count == 0
        assert summary.unverified_ratio == 0.0
        assert summary.threshold_exceeded is False

    def test_summary_all_unverified(self, tmp_path: Path) -> None:
        tracker = VerificationNudgeTracker(metrics_dir=tmp_path)
        for i in range(5):
            tracker.record(task_id=f"t{i}", session_id=f"s{i}")
        summary = tracker.summary()
        assert summary.total_completions == 5
        assert summary.verified_count == 0
        assert summary.unverified_count == 5
        assert summary.unverified_ratio == 1.0
        assert summary.threshold_exceeded is True

    def test_summary_mixed(self, tmp_path: Path) -> None:
        tracker = VerificationNudgeTracker(metrics_dir=tmp_path)
        # 2 verified, 1 unverified -> ratio 0.333..., above default 0.3 threshold
        tracker.record(task_id="t1", session_id="s1", tests_run=True)
        tracker.record(task_id="t2", session_id="s2", quality_gates_run=True)
        tracker.record(task_id="t3", session_id="s3")
        summary = tracker.summary()
        assert summary.total_completions == 3
        assert summary.verified_count == 2
        assert summary.unverified_count == 1
        assert summary.unverified_ratio == pytest.approx(1 / 3, rel=0.01)
        # At exactly MIN_COMPLETIONS_FOR_NUDGE, ratio > 0.3 -> exceeded
        assert summary.threshold_exceeded is True

    def test_threshold_not_exceeded_below_minimum(self, tmp_path: Path) -> None:
        """Threshold evaluation requires MIN_COMPLETIONS_FOR_NUDGE completions."""
        tracker = VerificationNudgeTracker(metrics_dir=tmp_path)
        # 1 unverified out of 1 total (100%), but below min completions
        tracker.record(task_id="t1", session_id="s1")
        summary = tracker.summary()
        assert summary.unverified_ratio == 1.0
        assert summary.threshold_exceeded is False  # below min completions

    def test_threshold_not_exceeded_at_boundary(self, tmp_path: Path) -> None:
        """Exactly at threshold should not trigger (strictly greater-than)."""
        # With threshold 0.3: 3 verified + 1 unverified = 0.25 ratio < 0.3
        tracker = VerificationNudgeTracker(metrics_dir=tmp_path, nudge_threshold=0.25)
        tracker.record(task_id="t1", session_id="s1", tests_run=True)
        tracker.record(task_id="t2", session_id="s2", tests_run=True)
        tracker.record(task_id="t3", session_id="s3", tests_run=True)
        tracker.record(task_id="t4", session_id="s4")  # unverified
        summary = tracker.summary()
        # 1/4 = 0.25, not strictly > 0.25
        assert summary.threshold_exceeded is False

    def test_custom_threshold(self, tmp_path: Path) -> None:
        tracker = VerificationNudgeTracker(metrics_dir=tmp_path, nudge_threshold=0.5)
        # 2 verified, 1 unverified -> ratio 0.333, below 0.5
        tracker.record(task_id="t1", session_id="s1", tests_run=True)
        tracker.record(task_id="t2", session_id="s2", tests_run=True)
        tracker.record(task_id="t3", session_id="s3")
        summary = tracker.summary()
        assert summary.threshold_exceeded is False

    def test_recent_unverified_order(self, tmp_path: Path) -> None:
        tracker = VerificationNudgeTracker(metrics_dir=tmp_path)
        tracker.record(task_id="t1", session_id="s1")  # unverified
        tracker.record(task_id="t2", session_id="s2", tests_run=True)  # verified
        tracker.record(task_id="t3", session_id="s3")  # unverified
        tracker.record(task_id="t4", session_id="s4")  # unverified
        summary = tracker.summary()
        # Most recent unverified first
        assert summary.recent_unverified == ["t4", "t3", "t1"]

    def test_recent_unverified_capped_at_5(self, tmp_path: Path) -> None:
        tracker = VerificationNudgeTracker(metrics_dir=tmp_path)
        for i in range(10):
            tracker.record(task_id=f"t{i}", session_id=f"s{i}")
        summary = tracker.summary()
        assert len(summary.recent_unverified) == 5
        assert summary.recent_unverified[0] == "t9"  # most recent first


# ---------------------------------------------------------------------------
# JSONL persistence
# ---------------------------------------------------------------------------


class TestVerificationNudgePersistence:
    """Tests for JSONL ledger read/write."""

    def test_ledger_written_on_record(self, tmp_path: Path) -> None:
        tracker = VerificationNudgeTracker(metrics_dir=tmp_path)
        tracker.record(task_id="t1", session_id="s1", tests_run=True)
        ledger = tmp_path / "verification_nudges.jsonl"
        assert ledger.exists()
        lines = ledger.read_text().strip().splitlines()
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["task_id"] == "t1"
        assert data["verified"] is True

    def test_ledger_appended_on_multiple_records(self, tmp_path: Path) -> None:
        tracker = VerificationNudgeTracker(metrics_dir=tmp_path)
        tracker.record(task_id="t1", session_id="s1", tests_run=True)
        tracker.record(task_id="t2", session_id="s2")
        ledger = tmp_path / "verification_nudges.jsonl"
        lines = ledger.read_text().strip().splitlines()
        assert len(lines) == 2

    def test_ledger_loaded_on_init(self, tmp_path: Path) -> None:
        # Create a ledger with existing data
        ledger = tmp_path / "verification_nudges.jsonl"
        rec_data = {
            "task_id": "existing",
            "session_id": "s_existing",
            "timestamp": 999.0,
            "tests_run": False,
            "quality_gates_run": False,
            "completion_signals_checked": False,
            "verified": False,
        }
        ledger.write_text(json.dumps(rec_data) + "\n")

        tracker = VerificationNudgeTracker(metrics_dir=tmp_path)
        assert len(tracker.records) == 1
        assert tracker.records[0].task_id == "existing"
        assert tracker.is_task_recorded("existing") is True

    def test_malformed_ledger_lines_skipped(self, tmp_path: Path) -> None:
        ledger = tmp_path / "verification_nudges.jsonl"
        good_line = json.dumps({"task_id": "good", "session_id": "s", "timestamp": 1.0, "verified": True})
        ledger.write_text(f"{good_line}\nnot-json\n{{}}\n")

        tracker = VerificationNudgeTracker(metrics_dir=tmp_path)
        # Only the first valid line should be loaded (empty dict also parses OK)
        assert len(tracker.records) >= 1
        assert tracker.records[0].task_id == "good"

    def test_missing_ledger_dir_created(self, tmp_path: Path) -> None:
        nested = tmp_path / "deep" / "metrics"
        tracker = VerificationNudgeTracker(metrics_dir=nested)
        tracker.record(task_id="t1", session_id="s1")
        assert (nested / "verification_nudges.jsonl").exists()

    def test_state_survives_restart(self, tmp_path: Path) -> None:
        """Simulate process restart: create tracker, record, destroy, re-create."""
        tracker1 = VerificationNudgeTracker(metrics_dir=tmp_path)
        tracker1.record(task_id="t1", session_id="s1")
        tracker1.record(task_id="t2", session_id="s2", tests_run=True)

        # Simulate restart
        tracker2 = VerificationNudgeTracker(metrics_dir=tmp_path)
        assert len(tracker2.records) == 2
        summary = tracker2.summary()
        assert summary.total_completions == 2
        assert summary.verified_count == 1
        assert summary.unverified_count == 1


# ---------------------------------------------------------------------------
# NudgeSummary
# ---------------------------------------------------------------------------


class TestNudgeSummary:
    """Tests for NudgeSummary serialization."""

    def test_to_dict(self) -> None:
        summary = NudgeSummary(
            total_completions=10,
            verified_count=7,
            unverified_count=3,
            unverified_ratio=0.3,
            threshold_exceeded=False,
            nudge_threshold=0.3,
            recent_unverified=["t1", "t2", "t3"],
        )
        d = summary.to_dict()
        assert d["total_completions"] == 10
        assert d["verified_count"] == 7
        assert d["unverified_count"] == 3
        assert d["unverified_ratio"] == 0.3
        assert d["threshold_exceeded"] is False
        assert d["recent_unverified"] == ["t1", "t2", "t3"]

    def test_to_dict_ratio_rounded(self) -> None:
        summary = NudgeSummary(
            total_completions=3,
            verified_count=2,
            unverified_count=1,
            unverified_ratio=0.33333333,
            threshold_exceeded=True,
            nudge_threshold=0.3,
            recent_unverified=["t1"],
        )
        d = summary.to_dict()
        assert d["unverified_ratio"] == 0.333


# ---------------------------------------------------------------------------
# load_nudge_summary helper
# ---------------------------------------------------------------------------


class TestLoadNudgeSummary:
    """Tests for the module-level load_nudge_summary helper."""

    def test_empty_dir(self, tmp_path: Path) -> None:
        summary = load_nudge_summary(tmp_path)
        assert summary.total_completions == 0
        assert summary.threshold_exceeded is False

    def test_loads_from_existing_ledger(self, tmp_path: Path) -> None:
        ledger = tmp_path / "verification_nudges.jsonl"
        records = [
            {"task_id": f"t{i}", "session_id": f"s{i}", "timestamp": float(i), "verified": i % 2 == 0}
            for i in range(6)
        ]
        ledger.write_text("\n".join(json.dumps(r) for r in records) + "\n")

        summary = load_nudge_summary(tmp_path)
        assert summary.total_completions == 6
        # i=0,2,4 are verified; i=1,3,5 are unverified
        assert summary.verified_count == 3
        assert summary.unverified_count == 3

    def test_custom_threshold(self, tmp_path: Path) -> None:
        ledger = tmp_path / "verification_nudges.jsonl"
        # 3 verified, 1 unverified -> ratio 0.25
        records = [
            {"task_id": "t1", "session_id": "s1", "timestamp": 1.0, "verified": True},
            {"task_id": "t2", "session_id": "s2", "timestamp": 2.0, "verified": True},
            {"task_id": "t3", "session_id": "s3", "timestamp": 3.0, "verified": True},
            {"task_id": "t4", "session_id": "s4", "timestamp": 4.0, "verified": False},
        ]
        ledger.write_text("\n".join(json.dumps(r) for r in records) + "\n")

        # Default threshold 0.3: 0.25 < 0.3 -> not exceeded
        summary_default = load_nudge_summary(tmp_path)
        assert summary_default.threshold_exceeded is False

        # Custom threshold 0.2: 0.25 > 0.2 -> exceeded
        summary_custom = load_nudge_summary(tmp_path, threshold=0.2)
        assert summary_custom.threshold_exceeded is True


# ---------------------------------------------------------------------------
# Verification evidence combinations
# ---------------------------------------------------------------------------


class TestVerificationEvidence:
    """Test that different combinations of evidence correctly mark verified."""

    @pytest.mark.parametrize(
        ("tests_run", "qg_run", "signals_checked", "expected_verified"),
        [
            (False, False, False, False),
            (True, False, False, True),
            (False, True, False, True),
            (False, False, True, True),
            (True, True, False, True),
            (True, False, True, True),
            (False, True, True, True),
            (True, True, True, True),
        ],
        ids=[
            "nothing",
            "tests_only",
            "qg_only",
            "signals_only",
            "tests+qg",
            "tests+signals",
            "qg+signals",
            "all_three",
        ],
    )
    def test_verification_combinations(
        self,
        tmp_path: Path,
        tests_run: bool,
        qg_run: bool,
        signals_checked: bool,
        expected_verified: bool,
    ) -> None:
        tracker = VerificationNudgeTracker(metrics_dir=tmp_path)
        rec = tracker.record(
            task_id="t1",
            session_id="s1",
            tests_run=tests_run,
            quality_gates_run=qg_run,
            completion_signals_checked=signals_checked,
        )
        assert rec.verified is expected_verified
