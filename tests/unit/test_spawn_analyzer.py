"""Unit tests for spawn failure analysis."""

from __future__ import annotations

from typing import Any

import pytest
from bernstein.core.container import ContainerError
from bernstein.core.spawn_analyzer import SpawnAnalyzer, SpawnFailureAnalysis
from bernstein.core.worktree import WorktreeError

from bernstein.adapters.base import RateLimitError, SpawnError


def test_rate_limit_error(make_task: Any) -> None:
    analysis = SpawnAnalyzer().analyze(RateLimitError("429"), make_task())

    assert analysis == SpawnFailureAnalysis("rate_limit", True, 60.0, "wait", "429")


def test_adapter_missing(make_task: Any) -> None:
    analysis = SpawnAnalyzer().analyze(SpawnError("adapter not found"), make_task())

    assert analysis.is_transient is False
    assert analysis.recommended_action == "skip"


def test_worktree_error(make_task: Any) -> None:
    analysis = SpawnAnalyzer().analyze(WorktreeError("lock held"), make_task())

    assert analysis.error_type == "worktree_error"
    assert analysis.recommended_delay_s == pytest.approx(10.0)


def test_container_error(make_task: Any) -> None:
    analysis = SpawnAnalyzer().analyze(ContainerError("docker unavailable"), make_task())

    assert analysis.is_transient is False
    assert analysis.recommended_action == "reconfigure"


def test_generic_exception(make_task: Any) -> None:
    analysis = SpawnAnalyzer().analyze(RuntimeError("network down"), make_task(role="qa"))

    assert analysis.error_type == "network_error"
    assert analysis.recommended_delay_s == pytest.approx(30.0)


def test_should_retry_all_transient() -> None:
    should_retry, delay = SpawnAnalyzer().should_retry(
        [
            SpawnFailureAnalysis("rate_limit", True, 60.0, "wait", "429"),
            SpawnFailureAnalysis("network_error", True, 30.0, "wait", "down"),
        ]
    )

    assert should_retry is True
    assert delay == pytest.approx(60.0)


def test_should_retry_has_permanent() -> None:
    should_retry, delay = SpawnAnalyzer().should_retry(
        [
            SpawnFailureAnalysis("adapter_missing", False, 0.0, "skip", "missing"),
            SpawnFailureAnalysis("rate_limit", True, 60.0, "wait", "429"),
        ]
    )

    assert should_retry is False
    assert delay == pytest.approx(0.0)


def test_should_retry_repeated_transient() -> None:
    should_retry, delay = SpawnAnalyzer().should_retry(
        [SpawnFailureAnalysis("rate_limit", True, 60.0, "wait", "429")] * 3
    )

    assert should_retry is False
    assert delay == pytest.approx(60.0)
