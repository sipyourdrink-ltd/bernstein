"""Unit tests for flaky test detection and quarantine."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bernstein.core.flaky_detector import FlakyDetector, parse_pytest_output
from bernstein.core.flaky_detector import TestRun as FlakyRunRecord


def test_parse_pytest_output_captures_terminal_results() -> None:
    output = "\n".join(
        [
            "tests/unit/test_demo.py::test_ok PASSED",
            "tests/unit/test_demo.py::test_fail FAILED",
            "tests/unit/test_demo.py::test_skip SKIPPED",
        ]
    )

    results = parse_pytest_output(output, run_id="run-1", timestamp="2026-03-31T10:00:00+00:00")

    assert [(result.test_id, result.passed) for result in results] == [
        ("tests/unit/test_demo.py::test_ok", True),
        ("tests/unit/test_demo.py::test_fail", False),
    ]


def test_analyze_quarantines_new_flaky_test(tmp_path: Path) -> None:
    detector = FlakyDetector(tmp_path, min_runs=4, flaky_threshold=0.20)
    now = datetime.now(UTC)
    detector.record_run(
        [
            FlakyRunRecord(
                "tests/unit/test_demo.py::test_flaky",
                True,
                10,
                (now - timedelta(minutes=4)).isoformat(),
                "1",
            ),
            FlakyRunRecord("tests/unit/test_demo.py::test_flaky", False, 11, (now - timedelta(minutes=3)).isoformat(), "2"),
            FlakyRunRecord("tests/unit/test_demo.py::test_flaky", True, 12, (now - timedelta(minutes=2)).isoformat(), "3"),
            FlakyRunRecord("tests/unit/test_demo.py::test_flaky", False, 13, (now - timedelta(minutes=1)).isoformat(), "4"),
        ]
    )

    result = detector.analyze()

    assert result.newly_detected == ["tests/unit/test_demo.py::test_flaky"]
    assert result.quarantined_count == 1
    assert result.flaky_tests[0].is_flaky
    quarantine_path = tmp_path / ".sdd" / "runtime" / "flaky_quarantine.json"
    assert json.loads(quarantine_path.read_text(encoding="utf-8")) == ["tests/unit/test_demo.py::test_flaky"]


def test_analyze_resolves_stable_quarantined_test(tmp_path: Path) -> None:
    detector = FlakyDetector(tmp_path, min_runs=4, flaky_threshold=0.20)
    quarantine_path = tmp_path / ".sdd" / "runtime" / "flaky_quarantine.json"
    quarantine_path.parent.mkdir(parents=True, exist_ok=True)
    quarantine_path.write_text(json.dumps(["tests/unit/test_demo.py::test_stable"]), encoding="utf-8")
    now = datetime.now(UTC)
    detector.record_run(
        [
            FlakyRunRecord(
                "tests/unit/test_demo.py::test_stable",
                True,
                10,
                (now - timedelta(minutes=3)).isoformat(),
                "1",
            ),
            FlakyRunRecord("tests/unit/test_demo.py::test_stable", True, 11, (now - timedelta(minutes=2)).isoformat(), "2"),
            FlakyRunRecord("tests/unit/test_demo.py::test_stable", True, 12, (now - timedelta(minutes=1)).isoformat(), "3"),
        ]
    )

    result = detector.analyze()

    assert result.resolved == ["tests/unit/test_demo.py::test_stable"]
    assert result.quarantined_count == 0
    assert detector.get_quarantined() == []


def test_get_quarantined_returns_empty_on_invalid_json(tmp_path: Path) -> None:
    quarantine_path = tmp_path / ".sdd" / "runtime" / "flaky_quarantine.json"
    quarantine_path.parent.mkdir(parents=True, exist_ok=True)
    quarantine_path.write_text("{invalid", encoding="utf-8")

    detector = FlakyDetector(tmp_path)

    assert detector.get_quarantined() == []


def test_pytest_deselect_args_uses_quarantine_entries(tmp_path: Path) -> None:
    quarantine_path = tmp_path / ".sdd" / "runtime" / "flaky_quarantine.json"
    quarantine_path.parent.mkdir(parents=True, exist_ok=True)
    quarantine_path.write_text(
        json.dumps(["tests/unit/test_a.py::test_one", "tests/unit/test_b.py::test_two"]),
        encoding="utf-8",
    )

    detector = FlakyDetector(tmp_path)

    deselect_args = detector.pytest_deselect_args()
    assert "--deselect tests/unit/test_a.py::test_one" in deselect_args
    assert "--deselect tests/unit/test_b.py::test_two" in deselect_args


def test_malformed_history_lines_are_skipped(tmp_path: Path) -> None:
    history_path = tmp_path / ".sdd" / "metrics" / "test_runs.jsonl"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.write_text(
        "\n".join(
            [
                "{invalid-json",
                json.dumps(
                    {
                        "test_id": "tests/unit/test_demo.py::test_ok",
                        "passed": True,
                        "duration_ms": 7,
                        "timestamp": "2026-03-31T10:00:00+00:00",
                        "run_id": "run-1",
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    detector = FlakyDetector(tmp_path)
    result = detector.analyze()

    assert len(result.flaky_tests) == 1
    assert result.flaky_tests[0].test_id == "tests/unit/test_demo.py::test_ok"
