"""Unit tests for flaky test detection and quarantine."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bernstein.core.flaky_detector import FlakyDetector, parse_pytest_output
from bernstein.core.flaky_detector import TestRun as FlakyRunRecord


def _observe_pytest(tmp_path: Path, *args: str) -> dict[str, object]:
    target = tmp_path / "observation.json"
    src = Path(__file__).resolve().parents[2] / "src"
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"PATH", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR"}
    }
    env.update(PYTHONPATH=str(src), PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from dataclasses import asdict; from pathlib import Path; import json, sys; "
                "from bernstein.core.quality.flaky_detector import capture_pytest_observation; "
                "observation = capture_pytest_observation(sys.argv[2:]); "
                "Path(sys.argv[1]).write_text(json.dumps(asdict(observation)), encoding='utf-8')"
            ),
            str(target),
            *args,
        ],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        timeout=30,
    )
    return json.loads(target.read_text(encoding="utf-8"))


def test_structured_pytest_capture_distinguishes_call_errors_and_setup_assertions(tmp_path: Path) -> None:
    (tmp_path / "test_sample.py").write_text(
        """import pytest

@pytest.fixture
def broken_setup():
    assert False

def test_pass():
    assert True

def test_assert():
    assert 1 == 2

def test_call_error():
    raise RuntimeError("test fixture error")

def test_setup_error(broken_setup):
    assert True

@pytest.mark.xfail
def test_xfail():
    assert False

@pytest.mark.xfail
def test_xpass():
    assert True

@pytest.mark.xfail(strict=True)
def test_strict_xpass():
    assert True
""",
        encoding="utf-8",
    )
    result = _observe_pytest(tmp_path, "-q", "test_sample.py")
    expected = [
        ("test_pass", "call", "passed"),
        ("test_assert", "call", "assertion_failed"),
        ("test_call_error", "call", "error"),
        ("test_setup_error", "setup", "error"),
        ("test_xfail", "call", "xfailed"),
        ("test_xpass", "call", "xpassed"),
        ("test_strict_xpass", "call", "xpassed"),
    ]
    assert result["exit_code"] == 1
    assert result["selected_node_ids"] == [f"test_sample.py::{name}" for name, _, _ in expected]
    phases = result["phases"]
    assert isinstance(phases, list)
    for name, phase, outcome in expected:
        assert {"node_id": f"test_sample.py::{name}", "phase": phase, "outcome": outcome} in phases


def test_structured_pytest_capture_records_collection_failure(tmp_path: Path) -> None:
    (tmp_path / "test_import_error.py").write_text(
        "raise ImportError('test fixture cannot import')\n", encoding="utf-8"
    )
    result = _observe_pytest(tmp_path, "--collect-only", "-q", "test_import_error.py")
    assert result["exit_code"] != 0
    assert result["selected_node_ids"] == []
    assert result["phases"] == [{"node_id": "test_import_error.py", "phase": "collect", "outcome": "error"}]


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
            FlakyRunRecord(
                "tests/unit/test_demo.py::test_flaky", False, 11, (now - timedelta(minutes=3)).isoformat(), "2"
            ),
            FlakyRunRecord(
                "tests/unit/test_demo.py::test_flaky", True, 12, (now - timedelta(minutes=2)).isoformat(), "3"
            ),
            FlakyRunRecord(
                "tests/unit/test_demo.py::test_flaky", False, 13, (now - timedelta(minutes=1)).isoformat(), "4"
            ),
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
            FlakyRunRecord(
                "tests/unit/test_demo.py::test_stable", True, 11, (now - timedelta(minutes=2)).isoformat(), "2"
            ),
            FlakyRunRecord(
                "tests/unit/test_demo.py::test_stable", True, 12, (now - timedelta(minutes=1)).isoformat(), "3"
            ),
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


def test_pytest_outcomes_distinguish_assertion_failures_and_execution_errors() -> None:
    from bernstein.core.quality.flaky_detector import iter_pytest_outcomes

    output = (
        "tests/test_demo.py::test_pass PASSED [ 25%]\n"
        "tests/test_demo.py::test_bug FAILED [ 50%]\n"
        "tests/test_demo.py::test_fixture ERROR [ 75%]\n"
        "tests/test_demo.py::test_optional[param] SKIPPED (dependency [database] FAILED\n"
        "setup) [100%]\n"
        "tests/test_demo.py::test_case[with space: value] PASSED [1/2]\r\n"
        "tests/test_demo.py::test_timed PASSED 1m 3s\n"
    )
    assert list(iter_pytest_outcomes(output)) == [
        ("tests/test_demo.py::test_pass", "PASSED"),
        ("tests/test_demo.py::test_bug", "FAILED"),
        ("tests/test_demo.py::test_fixture", "ERROR"),
        ("tests/test_demo.py::test_optional[param]", "SKIPPED"),
        ("tests/test_demo.py::test_case[with space: value]", "PASSED"),
        ("tests/test_demo.py::test_timed", "PASSED"),
    ]
    assert [record.passed for record in parse_pytest_output(output, run_id="fixture")] == [
        True,
        False,
        False,
        True,
        True,
    ]
