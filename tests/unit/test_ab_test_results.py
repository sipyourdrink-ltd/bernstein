"""Unit tests for A/B model test recording and reporting."""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.ab_test_results import (
    ABTestRecord,
    ABTestStore,
    generate_ab_report,
    model_for_task,
    record_ab_outcome,
)

# ---------------------------------------------------------------------------
# model_for_task — deterministic 50/50 routing
# ---------------------------------------------------------------------------


class TestModelForTask:
    def test_returns_one_of_two_models(self) -> None:
        result = model_for_task("task-abc", "sonnet", "opus")
        assert result in ("sonnet", "opus")

    def test_deterministic_same_input(self) -> None:
        a = model_for_task("task-xyz", "sonnet", "opus")
        b = model_for_task("task-xyz", "sonnet", "opus")
        assert a == b

    def test_different_tasks_may_differ(self) -> None:
        # With enough tasks, both models should appear
        models = {model_for_task(f"task-{i:04d}", "sonnet", "opus") for i in range(50)}
        assert models == {"sonnet", "opus"}

    def test_approx_50_50_split(self) -> None:
        results = [model_for_task(f"task-{i:06d}", "sonnet", "opus") for i in range(200)]
        sonnet_count = results.count("sonnet")
        # Expect between 40% and 60%
        assert 80 <= sonnet_count <= 120

    def test_uses_model_a_and_model_b_labels(self) -> None:
        result = model_for_task("task-aaa", "gpt-5.4", "o3")
        assert result in ("gpt-5.4", "o3")


# ---------------------------------------------------------------------------
# ABTestRecord — serialisation round-trip
# ---------------------------------------------------------------------------


class TestABTestRecord:
    def test_to_dict_and_from_dict(self) -> None:
        rec = ABTestRecord(
            task_id="t1",
            task_title="Add feature X",
            model="opus",
            session_id="sess-001",
            tokens_used=5000,
            files_changed=3,
            status="completed",
            duration_s=120.5,
            recorded_at=1_700_000_000.0,
        )
        d = rec.to_dict()
        restored = ABTestRecord.from_dict(d)
        assert restored.task_id == "t1"
        assert restored.model == "opus"
        assert restored.tokens_used == 5000
        assert restored.files_changed == 3
        assert restored.status == "completed"
        assert restored.duration_s == pytest.approx(120.5)

    def test_from_dict_handles_missing_recorded_at(self) -> None:
        d = {
            "task_id": "t2",
            "task_title": "Fix bug",
            "model": "sonnet",
            "session_id": "sess-002",
            "tokens_used": 1000,
            "files_changed": 1,
            "status": "failed",
            "duration_s": 60.0,
        }
        rec = ABTestRecord.from_dict(d)
        assert rec.recorded_at == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# ABTestStore — append / load
# ---------------------------------------------------------------------------


class TestABTestStore:
    def test_empty_when_no_file(self, tmp_path: Path) -> None:
        store = ABTestStore(tmp_path)
        assert store.load() == []

    def test_append_and_load_round_trip(self, tmp_path: Path) -> None:
        store = ABTestStore(tmp_path)
        rec = ABTestRecord(
            task_id="t1",
            task_title="Task one",
            model="sonnet",
            session_id="s1",
            tokens_used=100,
            files_changed=2,
            status="completed",
            duration_s=30.0,
        )
        store.append(rec)
        loaded = store.load()
        assert len(loaded) == 1
        assert loaded[0].task_id == "t1"
        assert loaded[0].model == "sonnet"

    def test_multiple_records_preserved_in_order(self, tmp_path: Path) -> None:
        store = ABTestStore(tmp_path)
        for i in range(5):
            store.append(
                ABTestRecord(
                    task_id=f"t{i}",
                    task_title=f"Task {i}",
                    model="opus" if i % 2 == 0 else "sonnet",
                    session_id=f"s{i}",
                    tokens_used=i * 100,
                    files_changed=i,
                    status="completed",
                    duration_s=float(i * 10),
                )
            )
        records = store.load()
        assert len(records) == 5
        assert records[0].task_id == "t0"
        assert records[4].task_id == "t4"

    def test_skips_malformed_lines(self, tmp_path: Path) -> None:
        store = ABTestStore(tmp_path)
        results_file = tmp_path / ".sdd" / "metrics" / "ab_test_results.jsonl"
        results_file.parent.mkdir(parents=True, exist_ok=True)
        results_file.write_text(
            "not valid json\n"
            '{"task_id":"t1","task_title":"T","model":"sonnet","session_id":"s1",'
            '"tokens_used":100,"files_changed":1,"status":"completed","duration_s":10.0}\n'
        )
        records = store.load()
        assert len(records) == 1
        assert records[0].task_id == "t1"


# ---------------------------------------------------------------------------
# record_ab_outcome — convenience helper
# ---------------------------------------------------------------------------


class TestRecordAbOutcome:
    def test_creates_file_and_record(self, tmp_path: Path) -> None:
        record_ab_outcome(
            tmp_path,
            task_id="task-99",
            task_title="Test task",
            model="opus",
            session_id="sess-99",
            tokens_used=2000,
            files_changed=5,
            status="completed",
            duration_s=45.0,
        )
        store = ABTestStore(tmp_path)
        records = store.load()
        assert len(records) == 1
        assert records[0].task_id == "task-99"
        assert records[0].model == "opus"
        assert records[0].files_changed == 5


# ---------------------------------------------------------------------------
# generate_ab_report
# ---------------------------------------------------------------------------


def _write_records(tmp_path: Path, records: list[ABTestRecord]) -> None:
    store = ABTestStore(tmp_path)
    for r in records:
        store.append(r)


class TestGenerateAbReport:
    def test_empty_store_returns_insufficient_data(self, tmp_path: Path) -> None:
        report = generate_ab_report(tmp_path)
        assert report.winner == "insufficient_data"
        assert "No A/B test records found" in report.summary

    def test_single_model_returns_insufficient_data(self, tmp_path: Path) -> None:
        _write_records(
            tmp_path,
            [
                ABTestRecord("t1", "T1", "sonnet", "s1", 500, 2, "completed", 30.0),
                ABTestRecord("t2", "T2", "sonnet", "s2", 600, 3, "completed", 40.0),
            ],
        )
        report = generate_ab_report(tmp_path)
        assert report.winner == "insufficient_data"

    def test_fewer_than_two_tasks_per_model_is_insufficient(self, tmp_path: Path) -> None:
        _write_records(
            tmp_path,
            [
                ABTestRecord("t1", "T1", "sonnet", "s1", 500, 2, "completed", 30.0),
                ABTestRecord("t2", "T2", "opus", "s2", 700, 1, "completed", 50.0),
            ],
        )
        report = generate_ab_report(tmp_path)
        assert report.winner == "insufficient_data"

    def test_higher_completion_rate_wins(self, tmp_path: Path) -> None:
        _write_records(
            tmp_path,
            [
                # sonnet: 3 completed out of 3 → 100%
                ABTestRecord("t1", "T1", "sonnet", "s1", 500, 2, "completed", 30.0),
                ABTestRecord("t2", "T2", "sonnet", "s2", 600, 3, "completed", 35.0),
                ABTestRecord("t3", "T3", "sonnet", "s3", 550, 2, "completed", 32.0),
                # opus: 2 completed out of 3 → 66%
                ABTestRecord("t4", "T4", "opus", "s4", 700, 1, "completed", 50.0),
                ABTestRecord("t5", "T5", "opus", "s5", 800, 0, "failed", 55.0),
                ABTestRecord("t6", "T6", "opus", "s6", 750, 2, "completed", 48.0),
            ],
        )
        report = generate_ab_report(tmp_path)
        assert report.winner == "sonnet"

    def test_on_equal_completion_rate_fewer_tokens_wins(self, tmp_path: Path) -> None:
        _write_records(
            tmp_path,
            [
                # Both 100% completion rate; sonnet uses fewer tokens
                ABTestRecord("t1", "T1", "sonnet", "s1", 300, 2, "completed", 20.0),
                ABTestRecord("t2", "T2", "sonnet", "s2", 350, 2, "completed", 22.0),
                ABTestRecord("t3", "T3", "opus", "s3", 900, 3, "completed", 60.0),
                ABTestRecord("t4", "T4", "opus", "s4", 950, 3, "completed", 65.0),
            ],
        )
        report = generate_ab_report(tmp_path)
        assert report.winner == "sonnet"

    def test_report_contains_both_models(self, tmp_path: Path) -> None:
        _write_records(
            tmp_path,
            [
                ABTestRecord("t1", "T1", "sonnet", "s1", 500, 2, "completed", 30.0),
                ABTestRecord("t2", "T2", "sonnet", "s2", 600, 3, "completed", 35.0),
                ABTestRecord("t3", "T3", "opus", "s3", 800, 1, "completed", 50.0),
                ABTestRecord("t4", "T4", "opus", "s4", 700, 2, "completed", 45.0),
            ],
        )
        report = generate_ab_report(tmp_path)
        assert report.model_a.model in ("opus", "sonnet")
        assert report.model_b.model in ("opus", "sonnet")
        assert report.model_a.model != report.model_b.model
        assert "sonnet" in report.summary or "opus" in report.summary

    def test_model_stats_totals(self, tmp_path: Path) -> None:
        _write_records(
            tmp_path,
            [
                ABTestRecord("t1", "T1", "sonnet", "s1", 400, 2, "completed", 20.0),
                ABTestRecord("t2", "T2", "sonnet", "s2", 600, 4, "completed", 40.0),
                ABTestRecord("t3", "T3", "opus", "s3", 800, 1, "completed", 50.0),
                ABTestRecord("t4", "T4", "opus", "s4", 1200, 3, "completed", 60.0),
            ],
        )
        report = generate_ab_report(tmp_path)
        sonnet_stats = report.model_a if report.model_a.model == "sonnet" else report.model_b
        assert sonnet_stats.total_tokens == 1000
        assert sonnet_stats.avg_tokens == pytest.approx(500.0)
        assert sonnet_stats.completed == 2
        assert sonnet_stats.failed == 0
