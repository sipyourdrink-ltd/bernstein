"""Unit tests for weighted quality scoring."""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.core.gate_runner import GateReport, GateResult
from bernstein.core.quality_score import QualityScore, QualityScorer


def _report(*results: GateResult) -> GateReport:
    return GateReport(
        task_id="T-score-1",
        overall_pass=all(not result.blocked for result in results),
        total_duration_ms=25,
        gates_run=[result.name for result in results],
        results=list(results),
        changed_files=["src/demo.py"],
        cache_hits=0,
    )


def test_score_weights_warn_and_excludes_skipped(tmp_path: Path) -> None:
    scorer = QualityScorer(tmp_path)
    report = _report(
        GateResult(
            name="lint",
            status="pass",
            required=True,
            blocked=False,
            cached=False,
            duration_ms=1,
            details="ok",
        ),
        GateResult(
            name="tests",
            status="warn",
            required=True,
            blocked=False,
            cached=False,
            duration_ms=1,
            details="flaky",
        ),
        GateResult(
            name="dead_code",
            status="fail",
            required=False,
            blocked=False,
            cached=False,
            duration_ms=1,
            details="unused symbol",
        ),
        GateResult(
            name="merge_conflict",
            status="bypassed",
            required=True,
            blocked=False,
            cached=False,
            duration_ms=0,
            details="manual override",
        ),
    )

    score = scorer.score(report)

    assert score == QualityScore(
        total=61,
        breakdown={"lint": 100, "tests": 50, "dead_code": 0},
        grade="D",
        trend="stable",
    )


def test_record_writes_jsonl_event(tmp_path: Path) -> None:
    scorer = QualityScorer(tmp_path)
    score = QualityScore(total=88, breakdown={"lint": 100}, grade="B", trend="stable")

    scorer.record("T-score-2", score)

    history_path = tmp_path / ".sdd" / "metrics" / "quality_scores.jsonl"
    payload = json.loads(history_path.read_text(encoding="utf-8").strip())
    assert payload["task_id"] == "T-score-2"
    assert payload["total"] == 88
    assert payload["grade"] == "B"


def test_trend_reads_historic_totals(tmp_path: Path) -> None:
    history_path = tmp_path / ".sdd" / "metrics" / "quality_scores.jsonl"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.write_text(
        "\n".join(
            json.dumps({"total": total})
            for total in (50, 58, 67, 76, 87)
        )
        + "\n",
        encoding="utf-8",
    )

    scorer = QualityScorer(tmp_path)

    assert scorer.trend() == "improving"


def test_score_defaults_to_100_when_no_gates_are_included(tmp_path: Path) -> None:
    scorer = QualityScorer(tmp_path)

    score = scorer.score(_report())

    assert score.total == 100
    assert score.breakdown == {}
    assert score.grade == "A"


def test_timeout_status_scores_as_half_credit(tmp_path: Path) -> None:
    scorer = QualityScorer(tmp_path)
    report = _report(
        GateResult(
            name="tests",
            status="timeout",
            required=True,
            blocked=False,
            cached=False,
            duration_ms=10,
            details="Timed out",
        )
    )

    score = scorer.score(report)

    assert score.total == 50
    assert score.breakdown == {"tests": 50}
