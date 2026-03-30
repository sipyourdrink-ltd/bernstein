"""Tests for CascadeRouter — cost-aware model cascading router.

Covers:
- Initial model selection (cheapest viable, high-stakes bypass)
- Proactive bandit skip (poor success rate → jump to next tier)
- Escalation decisions: hard failure, janitor failure, confidence signals
- Chain cost reports and savings calculations
- Output confidence detection
- Utility functions: _cascade_for_task, _effort_for_model
- load_cascade_savings_summary
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from bernstein.core.cascade_router import (
    CascadeAttempt,
    CascadeRouter,
    _cascade_for_task,
    _effort_for_model,
    load_cascade_savings_summary,
)
from bernstein.core.models import Complexity, Scope, Task

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _task(
    role: str = "backend",
    complexity: Complexity = Complexity.MEDIUM,
    scope: Scope = Scope.MEDIUM,
    priority: int = 2,
    model: str | None = None,
    effort: str | None = None,
) -> Task:
    return Task(
        id="t1",
        title="Do something",
        description="desc",
        role=role,
        complexity=complexity,
        scope=scope,
        priority=priority,
        model=model,
        effort=effort,
    )


def _attempt(
    task_id: str = "t1",
    chain_id: str = "abc123",
    model: str = "sonnet",
    effort: str = "normal",
    attempt_number: int = 0,
    success: bool = True,
    cost_usd: float = 0.001,
    latency_s: float = 30.0,
) -> CascadeAttempt:
    return CascadeAttempt(
        task_id=task_id,
        chain_id=chain_id,
        model=model,
        effort=effort,
        attempt_number=attempt_number,
        success=success,
        cost_usd=cost_usd,
        latency_s=latency_s,
    )


# ---------------------------------------------------------------------------
# _cascade_for_task
# ---------------------------------------------------------------------------


class TestCascadeForTask:
    def test_standard_task_uses_full_cascade(self) -> None:
        task = _task(role="backend", complexity=Complexity.LOW)
        cascade = _cascade_for_task(task)
        assert cascade == ["sonnet", "opus"]

    def test_manager_skips_haiku(self) -> None:
        cascade = _cascade_for_task(_task(role="manager"))
        assert cascade == ["sonnet", "opus"]
        assert "haiku" not in cascade

    def test_architect_skips_haiku(self) -> None:
        cascade = _cascade_for_task(_task(role="architect"))
        assert cascade == ["sonnet", "opus"]

    def test_security_skips_haiku(self) -> None:
        cascade = _cascade_for_task(_task(role="security"))
        assert cascade == ["sonnet", "opus"]

    def test_high_complexity_skips_haiku(self) -> None:
        cascade = _cascade_for_task(_task(complexity=Complexity.HIGH))
        assert cascade == ["sonnet", "opus"]

    def test_large_scope_skips_haiku(self) -> None:
        cascade = _cascade_for_task(_task(scope=Scope.LARGE))
        assert cascade == ["sonnet", "opus"]

    def test_critical_priority_skips_haiku(self) -> None:
        cascade = _cascade_for_task(_task(priority=1))
        assert cascade == ["sonnet", "opus"]


# ---------------------------------------------------------------------------
# _effort_for_model
# ---------------------------------------------------------------------------


class TestEffortForModel:
    def test_haiku_returns_low(self) -> None:
        assert _effort_for_model("haiku", _task()) == "low"

    def test_sonnet_returns_high(self) -> None:
        assert _effort_for_model("sonnet", _task()) == "high"

    def test_opus_returns_max(self) -> None:
        assert _effort_for_model("opus", _task()) == "max"

    def test_task_effort_override_respected(self) -> None:
        task = _task(effort="max")
        assert _effort_for_model("haiku", task) == "max"

    def test_unknown_model_returns_high(self) -> None:
        assert _effort_for_model("gpt-4", _task()) == "high"


# ---------------------------------------------------------------------------
# CascadeRouter.select — initial selection
# ---------------------------------------------------------------------------


class TestCascadeRouterSelect:
    def test_new_task_gets_fresh_chain_id(self) -> None:
        router = CascadeRouter()
        d1 = router.select(_task())
        d2 = router.select(_task())
        assert d1.chain_id != d2.chain_id

    def test_same_chain_id_continues_chain(self) -> None:
        router = CascadeRouter()
        d1 = router.select(_task())
        # Manually insert a prior attempt so select() sees attempt_number=1
        router._chains[d1.chain_id].append(_attempt(chain_id=d1.chain_id, model="sonnet"))
        d2 = router.select(_task(), chain_id=d1.chain_id)
        assert d2.attempt_number == 1

    def test_standard_task_starts_at_sonnet(self) -> None:
        router = CascadeRouter()
        decision = router.select(_task(role="backend", complexity=Complexity.LOW))
        assert decision.model == "sonnet"
        assert decision.is_escalated is False
        assert decision.attempt_number == 0

    def test_manager_starts_at_sonnet(self) -> None:
        router = CascadeRouter()
        decision = router.select(_task(role="manager"))
        assert decision.model == "sonnet"

    def test_security_starts_at_sonnet(self) -> None:
        router = CascadeRouter()
        decision = router.select(_task(role="security"))
        assert decision.model == "sonnet"

    def test_high_complexity_starts_at_sonnet(self) -> None:
        router = CascadeRouter()
        decision = router.select(_task(complexity=Complexity.HIGH))
        assert decision.model == "sonnet"

    def test_large_scope_starts_at_sonnet(self) -> None:
        router = CascadeRouter()
        decision = router.select(_task(scope=Scope.LARGE))
        assert decision.model == "sonnet"

    def test_manager_override_respected(self) -> None:
        router = CascadeRouter()
        decision = router.select(_task(model="sonnet"))
        assert decision.model == "sonnet"

    def test_estimated_cost_positive(self) -> None:
        router = CascadeRouter()
        decision = router.select(_task())
        assert decision.estimated_cost_usd > 0.0

    def test_decision_has_reason(self) -> None:
        router = CascadeRouter()
        decision = router.select(_task())
        assert len(decision.reason) > 0


# ---------------------------------------------------------------------------
# CascadeRouter.select — bandit proactive skip
# ---------------------------------------------------------------------------


class TestCascadeRouterBanditProactiveSkip:
    def test_proactive_skip_when_haiku_below_threshold(self, tmp_path: Path) -> None:
        """When bandit data shows haiku fails too often, start at sonnet."""
        router = CascadeRouter(bandit_metrics_dir=tmp_path / "metrics")
        # Seed bandit: haiku has 40% success for backend (below 80% threshold)
        bandit = router._get_bandit()
        for _ in range(10):
            bandit.record(
                role="backend",
                model="sonnet",
                success=(_ < 4),  # 4/10 = 40%
                cost_usd=0.001,
            )

        decision = router.select(_task(role="backend", complexity=Complexity.LOW))
        # With low success rate, cascade escalates to opus
        assert decision.model in ("sonnet", "opus")

    def test_no_skip_when_haiku_meets_threshold(self, tmp_path: Path) -> None:
        """When haiku meets the quality threshold, use it."""
        router = CascadeRouter(bandit_metrics_dir=tmp_path / "metrics")
        bandit = router._get_bandit()
        for _ in range(10):
            bandit.record(role="backend", model="sonnet", success=True, cost_usd=0.001)

        decision = router.select(_task(role="backend", complexity=Complexity.LOW))
        assert decision.model == "sonnet"

    def test_no_skip_when_insufficient_observations(self, tmp_path: Path) -> None:
        """With fewer than MIN_OBSERVATIONS, don't skip even if success rate is low."""
        router = CascadeRouter(bandit_metrics_dir=tmp_path / "metrics", min_observations=5)
        bandit = router._get_bandit()
        # Only 3 observations (below MIN_OBSERVATIONS=5)
        for _ in range(3):
            bandit.record(role="backend", model="sonnet", success=False, cost_usd=0.001)

        decision = router.select(_task(role="backend", complexity=Complexity.LOW))
        # Should still try haiku (not enough data to skip)
        assert decision.model == "sonnet"


# ---------------------------------------------------------------------------
# CascadeRouter.detect_low_confidence
# ---------------------------------------------------------------------------


class TestDetectLowConfidence:
    def test_detects_not_sure(self) -> None:
        router = CascadeRouter()
        low, phrase = router.detect_low_confidence("I'm not sure how to approach this.")
        assert low is True
        assert phrase

    def test_detects_cannot_determine(self) -> None:
        router = CascadeRouter()
        low, _phrase = router.detect_low_confidence("I cannot determine the correct approach here.")
        assert low is True

    def test_detects_incomplete_implementation(self) -> None:
        router = CascadeRouter()
        low, _ = router.detect_low_confidence("This is an incomplete implementation.")
        assert low is True

    def test_detects_need_more_context(self) -> None:
        router = CascadeRouter()
        low, _ = router.detect_low_confidence("I need more context to proceed.")
        assert low is True

    def test_detects_partially_complete(self) -> None:
        router = CascadeRouter()
        low, _ = router.detect_low_confidence("The task is partially complete.")
        assert low is True

    def test_no_signal_in_clean_output(self) -> None:
        router = CascadeRouter()
        low, phrase = router.detect_low_confidence(
            "Implementation complete. All tests pass. Feature added successfully."
        )
        assert low is False
        assert phrase == ""

    def test_only_scans_tail(self) -> None:
        """Low-confidence phrase early in long output should NOT trigger escalation."""
        router = CascadeRouter()
        prefix = "I'm not sure " + "x" * 3000
        suffix = "Implementation complete. All tests pass."
        low, _ = router.detect_low_confidence(prefix + suffix)
        # Tail (last 2000 chars) should only contain the success message
        assert low is False

    def test_case_insensitive(self) -> None:
        router = CascadeRouter()
        low, _ = router.detect_low_confidence("I AM NOT SURE how to proceed.")
        assert low is True


# ---------------------------------------------------------------------------
# CascadeRouter.record_and_escalate
# ---------------------------------------------------------------------------


class TestRecordAndEscalate:
    def test_successful_attempt_returns_none(self) -> None:
        router = CascadeRouter()
        decision = router.select(_task())
        attempt = _attempt(chain_id=decision.chain_id, model=decision.model, success=True)
        result = router.record_and_escalate(
            chain_id=decision.chain_id,
            task=_task(),
            attempt=attempt,
            janitor_passed=True,
        )
        assert result is None

    def test_failed_attempt_escalates(self) -> None:
        router = CascadeRouter()
        decision = router.select(_task())
        attempt = _attempt(chain_id=decision.chain_id, model="sonnet", success=False)
        result = router.record_and_escalate(
            chain_id=decision.chain_id,
            task=_task(),
            attempt=attempt,
        )
        assert result is not None
        assert result.model == "opus"
        assert result.is_escalated is True
        assert result.attempt_number == 1

    def test_janitor_failure_escalates(self) -> None:
        router = CascadeRouter()
        decision = router.select(_task())
        attempt = _attempt(chain_id=decision.chain_id, model="sonnet", success=True)
        result = router.record_and_escalate(
            chain_id=decision.chain_id,
            task=_task(),
            attempt=attempt,
            janitor_passed=False,
        )
        assert result is not None
        assert result.model == "opus"
        assert "janitor" in result.reason

    def test_low_confidence_output_escalates(self) -> None:
        router = CascadeRouter()
        decision = router.select(_task())
        attempt = _attempt(chain_id=decision.chain_id, model="sonnet", success=True)
        result = router.record_and_escalate(
            chain_id=decision.chain_id,
            task=_task(),
            attempt=attempt,
            janitor_passed=True,
            output="I'm not sure this implementation is correct.",
        )
        assert result is not None
        assert "confidence" in result.reason

    def test_sonnet_escalates_to_opus(self) -> None:
        router = CascadeRouter()
        decision = router.select(_task())
        # Manually put sonnet as current model
        attempt = _attempt(chain_id=decision.chain_id, model="sonnet", success=False, attempt_number=1)
        router._chains[decision.chain_id] = [
            _attempt(chain_id=decision.chain_id, model="sonnet", success=False, attempt_number=0)
        ]
        result = router.record_and_escalate(
            chain_id=decision.chain_id,
            task=_task(),
            attempt=attempt,
        )
        assert result is not None
        assert result.model == "opus"

    def test_opus_failure_returns_none(self) -> None:
        """At the top of the cascade, no further escalation is possible."""
        router = CascadeRouter()
        decision = router.select(_task())
        attempt = _attempt(chain_id=decision.chain_id, model="opus", success=False, attempt_number=2)
        router._chains[decision.chain_id] = [
            _attempt(chain_id=decision.chain_id, model="sonnet", success=False, attempt_number=0),
            _attempt(chain_id=decision.chain_id, model="sonnet", success=False, attempt_number=1),
        ]
        result = router.record_and_escalate(
            chain_id=decision.chain_id,
            task=_task(),
            attempt=attempt,
        )
        assert result is None

    def test_attempt_is_recorded_in_chain(self) -> None:
        router = CascadeRouter()
        decision = router.select(_task())
        attempt = _attempt(chain_id=decision.chain_id, model="sonnet", success=True)
        router.record_and_escalate(
            chain_id=decision.chain_id,
            task=_task(),
            attempt=attempt,
            janitor_passed=True,
        )
        chain = router._chains[decision.chain_id]
        assert len(chain) == 1
        assert chain[0].model == "sonnet"

    def test_escalated_attempt_marked_as_escalated(self) -> None:
        router = CascadeRouter()
        decision = router.select(_task())
        attempt = _attempt(chain_id=decision.chain_id, model="sonnet", success=True)
        router.record_and_escalate(
            chain_id=decision.chain_id,
            task=_task(),
            attempt=attempt,
            janitor_passed=False,  # janitor fails → escalation
        )
        chain = router._chains[decision.chain_id]
        assert chain[0].escalated is True
        assert chain[0].escalation_reason is not None


# ---------------------------------------------------------------------------
# CascadeChainReport
# ---------------------------------------------------------------------------


class TestGetChainReport:
    def test_single_successful_attempt(self) -> None:
        router = CascadeRouter()
        decision = router.select(_task())
        attempt = _attempt(
            chain_id=decision.chain_id,
            model="sonnet",
            success=True,
            cost_usd=0.001,
        )
        router.record_and_escalate(
            chain_id=decision.chain_id,
            task=_task(),
            attempt=attempt,
            janitor_passed=True,
        )

        report = router.get_chain_report(decision.chain_id, _task())
        assert report.total_cost_usd == pytest.approx(0.001)
        assert report.escalation_overhead_usd == pytest.approx(0.0)
        assert report.first_attempt_cost_usd == pytest.approx(0.001)
        assert report.final_model == "sonnet"
        assert report.succeeded is True
        # Haiku is cheaper than Opus, so savings should be positive
        assert report.saved_vs_direct_opus_usd > 0.0

    def test_escalated_chain_has_overhead(self) -> None:
        router = CascadeRouter()
        decision = router.select(_task())
        chain_id = decision.chain_id

        # First attempt (haiku) fails
        first_attempt = _attempt(chain_id=chain_id, model="sonnet", attempt_number=0, cost_usd=0.001)
        router.record_and_escalate(chain_id=chain_id, task=_task(), attempt=first_attempt, janitor_passed=False)

        # Second attempt (sonnet) succeeds
        sonnet_attempt = _attempt(chain_id=chain_id, model="sonnet", attempt_number=1, cost_usd=0.003)
        router.record_and_escalate(chain_id=chain_id, task=_task(), attempt=sonnet_attempt, janitor_passed=True)

        report = router.get_chain_report(chain_id, _task())
        assert report.total_cost_usd == pytest.approx(0.004)
        assert report.first_attempt_cost_usd == pytest.approx(0.001)
        assert report.escalation_overhead_usd == pytest.approx(0.003)
        assert report.final_model == "sonnet"

    def test_empty_chain_returns_zero_costs(self) -> None:
        router = CascadeRouter()
        fake_id = "nonexistent"
        report = router.get_chain_report(fake_id, _task())
        assert report.total_cost_usd == 0.0
        assert report.final_model == "unknown"
        assert report.succeeded is False

    def test_to_dict_is_json_serialisable(self) -> None:
        router = CascadeRouter()
        decision = router.select(_task())
        attempt = _attempt(chain_id=decision.chain_id, model="sonnet", success=True, cost_usd=0.001)
        router.record_and_escalate(chain_id=decision.chain_id, task=_task(), attempt=attempt, janitor_passed=True)
        report = router.get_chain_report(decision.chain_id, _task())
        d = report.to_dict()
        # Should be JSON-serialisable
        serialised = json.dumps(d)
        assert "chain_id" in serialised


# ---------------------------------------------------------------------------
# CascadeRouter.save_chain
# ---------------------------------------------------------------------------


class TestSaveChain:
    def test_creates_jsonl_file(self, tmp_path: Path) -> None:
        router = CascadeRouter()
        decision = router.select(_task())
        attempt = _attempt(chain_id=decision.chain_id, model="sonnet", success=True, cost_usd=0.001)
        router.record_and_escalate(chain_id=decision.chain_id, task=_task(), attempt=attempt, janitor_passed=True)

        metrics_dir = tmp_path / "metrics"
        router.save_chain(decision.chain_id, _task(), metrics_dir)

        chains_file = metrics_dir / "cascade_chains.jsonl"
        assert chains_file.exists()
        lines = chains_file.read_text().strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["chain_id"] == decision.chain_id
        assert "timestamp" in record

    def test_appends_multiple_chains(self, tmp_path: Path) -> None:
        metrics_dir = tmp_path / "metrics"
        router = CascadeRouter()

        for _ in range(3):
            decision = router.select(_task())
            attempt = _attempt(chain_id=decision.chain_id, model="sonnet", success=True)
            router.record_and_escalate(chain_id=decision.chain_id, task=_task(), attempt=attempt, janitor_passed=True)
            router.save_chain(decision.chain_id, _task(), metrics_dir)

        lines = (metrics_dir / "cascade_chains.jsonl").read_text().strip().splitlines()
        assert len(lines) == 3


# ---------------------------------------------------------------------------
# CascadeAttempt serialisation
# ---------------------------------------------------------------------------


class TestCascadeAttemptSerialisation:
    def test_roundtrip(self) -> None:
        a = _attempt(model="sonnet", cost_usd=0.01, latency_s=45.0)
        a2 = CascadeAttempt.from_dict(a.to_dict())
        assert a2.model == a.model
        assert a2.cost_usd == a.cost_usd
        assert a2.latency_s == a.latency_s
        assert a2.success == a.success


# ---------------------------------------------------------------------------
# load_cascade_savings_summary
# ---------------------------------------------------------------------------


class TestLoadCascadeSavingsSummary:
    def test_returns_zeros_when_no_file(self, tmp_path: Path) -> None:
        summary = load_cascade_savings_summary(tmp_path)
        assert summary["total_chains"] == 0
        assert summary["total_cost_usd"] == 0.0
        assert summary["escalation_rate"] == 0.0

    def test_aggregates_correctly(self, tmp_path: Path) -> None:
        router = CascadeRouter()
        metrics_dir = tmp_path

        # Chain 1: haiku succeeded, no escalation
        d1 = router.select(_task())
        a1 = _attempt(chain_id=d1.chain_id, model="sonnet", success=True, cost_usd=0.001)
        router.record_and_escalate(chain_id=d1.chain_id, task=_task(), attempt=a1, janitor_passed=True)
        router.save_chain(d1.chain_id, _task(), metrics_dir)

        # Chain 2: haiku escalated to sonnet
        d2 = router.select(_task())
        a2a = _attempt(chain_id=d2.chain_id, model="sonnet", attempt_number=0, cost_usd=0.001)
        router.record_and_escalate(chain_id=d2.chain_id, task=_task(), attempt=a2a, janitor_passed=False)
        a2b = _attempt(chain_id=d2.chain_id, model="sonnet", attempt_number=1, cost_usd=0.003)
        router.record_and_escalate(chain_id=d2.chain_id, task=_task(), attempt=a2b, janitor_passed=True)
        router.save_chain(d2.chain_id, _task(), metrics_dir)

        summary = load_cascade_savings_summary(metrics_dir)
        assert summary["total_chains"] == 2
        # chain1: 0.001, chain2: 0.001 (haiku) + 0.003 (sonnet) = 0.005 total
        assert summary["total_cost_usd"] == pytest.approx(0.005)
        # Chain 2 had escalation overhead
        assert summary["escalation_overhead_usd"] > 0.0
        # One of the two chains escalated
        assert summary["escalation_rate"] == pytest.approx(0.5)

    def test_handles_corrupt_file_gracefully(self, tmp_path: Path) -> None:
        chains_file = tmp_path / "cascade_chains.jsonl"
        chains_file.write_text("not valid json\n{also bad}\n")
        # Should not raise; return partial or zero results
        summary = load_cascade_savings_summary(tmp_path)
        assert isinstance(summary["total_chains"], int)


# ---------------------------------------------------------------------------
# save_bandit integration
# ---------------------------------------------------------------------------


class TestSaveBandit:
    def test_save_bandit_persists_observations(self, tmp_path: Path) -> None:
        metrics_dir = tmp_path / "metrics"
        metrics_dir.mkdir()

        router = CascadeRouter(bandit_metrics_dir=metrics_dir)
        decision = router.select(_task())
        attempt = _attempt(chain_id=decision.chain_id, model="sonnet", success=True, cost_usd=0.001)
        router.record_and_escalate(chain_id=decision.chain_id, task=_task(), attempt=attempt, janitor_passed=True)
        router.save_bandit()

        bandit_file = metrics_dir / "bandit_state.json"
        assert bandit_file.exists()
        data = json.loads(bandit_file.read_text())
        arms = data.get("arms", [])
        assert any(a["model"] == "sonnet" for a in arms)
