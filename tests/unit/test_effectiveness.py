"""Unit tests for effectiveness scoring."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from bernstein.core.effectiveness import EffectivenessScorer
from bernstein.core.models import AgentSession, ModelConfig, Scope, TaskStatus


def _session(
    *,
    session_id: str,
    role: str = "backend",
    model: str = "sonnet",
    effort: str = "high",
    spawn_offset_s: float = 60.0,
    tokens_used: int = 1000,
) -> AgentSession:
    now = time.time()
    return AgentSession(
        id=session_id,
        role=role,
        model_config=ModelConfig(model=model, effort=effort),
        spawn_ts=now - spawn_offset_s,
        heartbeat_ts=now,
        tokens_used=tokens_used,
    )


def test_score_perfect_session(tmp_path: Path, make_task: Any) -> None:
    scorer = EffectivenessScorer(tmp_path)
    task = make_task(status=TaskStatus.DONE, scope=Scope.SMALL)
    score = scorer.score(
        _session(session_id="A-1", model="opus", effort="max", spawn_offset_s=30.0),
        task,
        gate_report=SimpleNamespace(overall_pass=True),
        log_summary=None,
    )

    assert score.quality_score == 100
    assert score.total >= 90
    assert score.grade in {"A", "B"}


def test_score_slow_session_reduces_time_component(tmp_path: Path, make_task: Any) -> None:
    scorer = EffectivenessScorer(tmp_path)
    task = make_task(status=TaskStatus.DONE, scope=Scope.MEDIUM)
    # default estimated_minutes=30 => 1800s; this is 5400s (3x)
    score = scorer.score(
        _session(session_id="A-2", spawn_offset_s=5400.0),
        task,
        gate_report=SimpleNamespace(overall_pass=True),
        log_summary=None,
    )

    assert score.time_score == 50
    assert score.total < 100


def test_record_writes_jsonl(tmp_path: Path, make_task: Any) -> None:
    scorer = EffectivenessScorer(tmp_path)
    task = make_task(status=TaskStatus.DONE)
    score = scorer.score(
        _session(session_id="A-3"),
        task,
        gate_report=SimpleNamespace(overall_pass=True),
        log_summary=None,
    )
    scorer.record(score)

    history_file = tmp_path / ".sdd" / "metrics" / "effectiveness.jsonl"
    raw = history_file.read_text(encoding="utf-8")
    assert "A-3" in raw
    assert task.id in raw


def test_trends_returns_direction_by_role(tmp_path: Path, make_task: Any) -> None:
    scorer = EffectivenessScorer(tmp_path)
    task = make_task(status=TaskStatus.DONE)
    for idx in range(6):
        session = _session(
            session_id=f"A-{idx}",
            model="sonnet",
            effort="high",
            spawn_offset_s=60.0 + (idx * 10.0),
            tokens_used=2000 + (idx * 100),
        )
        score = scorer.score(session, task, gate_report=SimpleNamespace(overall_pass=True), log_summary=None)
        scorer.record(score)

    trends = scorer.trends(window=6)
    assert "backend" in trends
    assert trends["backend"] in {"improving", "declining", "stable"}
