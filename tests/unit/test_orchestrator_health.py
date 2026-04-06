"""Tests for orchestrator health score (ORCH-012)."""

from __future__ import annotations

from bernstein.core.orchestrator_health import (
    ComponentHealth,
    HealthGrade,
    HealthScoreResult,
    OrchestratorHealthScorer,
    _classify_grade,
    _memory_score,
    _server_score,
)


class TestOrchestratorHealthScorer:
    def test_all_healthy(self) -> None:
        scorer = OrchestratorHealthScorer()
        result = scorer.evaluate(
            heartbeat_ok=True,
            circuit_breaker_open=False,
            memory_used_pct=40.0,
            wal_healthy=True,
            server_reachable=True,
        )
        assert result.score == 100
        assert result.grade == HealthGrade.HEALTHY
        assert "All systems operational" in result.message

    def test_server_down(self) -> None:
        scorer = OrchestratorHealthScorer()
        result = scorer.evaluate(
            heartbeat_ok=True,
            circuit_breaker_open=False,
            memory_used_pct=40.0,
            wal_healthy=True,
            server_reachable=False,
            consecutive_server_failures=3,
        )
        assert result.score < 100
        assert "server" in result.message.lower()

    def test_circuit_breaker_open(self) -> None:
        scorer = OrchestratorHealthScorer()
        result = scorer.evaluate(
            heartbeat_ok=True,
            circuit_breaker_open=True,
            memory_used_pct=40.0,
            wal_healthy=True,
            server_reachable=True,
        )
        assert result.score < 100
        # Circuit breaker should be flagged
        cb = [c for c in result.components if c.name == "circuit_breaker"][0]
        assert not cb.healthy

    def test_high_memory(self) -> None:
        scorer = OrchestratorHealthScorer()
        result = scorer.evaluate(
            heartbeat_ok=True,
            circuit_breaker_open=False,
            memory_used_pct=92.0,
            wal_healthy=True,
            server_reachable=True,
        )
        assert result.score < 100
        # Memory component should be marked unhealthy
        mem = [c for c in result.components if c.name == "memory"][0]
        assert not mem.healthy

    def test_everything_broken(self) -> None:
        scorer = OrchestratorHealthScorer()
        result = scorer.evaluate(
            heartbeat_ok=False,
            circuit_breaker_open=True,
            memory_used_pct=96.0,
            wal_healthy=False,
            server_reachable=False,
            consecutive_server_failures=10,
        )
        assert result.score < 20
        assert result.grade == HealthGrade.CRITICAL

    def test_components_present(self) -> None:
        scorer = OrchestratorHealthScorer()
        result = scorer.evaluate()
        component_names = {c.name for c in result.components}
        assert "heartbeat" in component_names
        assert "circuit_breaker" in component_names
        assert "memory" in component_names
        assert "wal" in component_names
        assert "server" in component_names

    def test_to_dict(self) -> None:
        scorer = OrchestratorHealthScorer()
        result = scorer.evaluate()
        d = result.to_dict()
        assert "score" in d
        assert "grade" in d
        assert "components" in d
        assert isinstance(d["components"], list)


class TestMemoryScore:
    def test_low_usage(self) -> None:
        assert _memory_score(30.0) == 100

    def test_moderate_usage(self) -> None:
        score = _memory_score(70.0)
        assert 60 <= score <= 100

    def test_high_usage(self) -> None:
        score = _memory_score(85.0)
        assert 20 <= score <= 60

    def test_critical_usage(self) -> None:
        score = _memory_score(93.0)
        assert 0 <= score <= 20

    def test_extreme_usage(self) -> None:
        assert _memory_score(96.0) == 0


class TestServerScore:
    def test_reachable(self) -> None:
        assert _server_score(True, 0) == 100

    def test_unreachable_first_failure(self) -> None:
        assert _server_score(False, 1) == 60

    def test_unreachable_many_failures(self) -> None:
        assert _server_score(False, 10) == 0


class TestClassifyGrade:
    def test_healthy(self) -> None:
        assert _classify_grade(80) == HealthGrade.HEALTHY
        assert _classify_grade(100) == HealthGrade.HEALTHY

    def test_degraded(self) -> None:
        assert _classify_grade(50) == HealthGrade.DEGRADED
        assert _classify_grade(79) == HealthGrade.DEGRADED

    def test_unhealthy(self) -> None:
        assert _classify_grade(20) == HealthGrade.UNHEALTHY
        assert _classify_grade(49) == HealthGrade.UNHEALTHY

    def test_critical(self) -> None:
        assert _classify_grade(0) == HealthGrade.CRITICAL
        assert _classify_grade(19) == HealthGrade.CRITICAL
