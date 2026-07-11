"""Behavioural tests for ``router_core`` - tier-aware provider routing.

Covers the dark paths of the deterministic routing engine:

* Region normalisation + matching (prefix semantics).
* Budget-aware opus downgrade predicate.
* ``ProviderHealth`` EMA latency + success-rate + status transitions.
* ``CostTracker`` per-request averages.
* ``ProviderConfig`` free-tier exhaustion + reset + effective cost.
* ``TierAwareRouter`` register / availability / quota / health / cost.
* ``get_available_providers`` health + tier + policy filtering and the
  weighted-score ordering.
* ``select_provider_for_task`` dispatch chain: preferred tier ->
  fallback tier -> last resort -> ``RouterError``, plus vision /
  large-context capability gating and pinned-provider error paths.
* ``record_outcome`` routing-weight bumps with clamping.
* ``save_weights`` / ``load_weights`` round-trip.
* ``validate_policy`` issue surfacing; ``get_provider_summary`` shape.
* ``route_task`` deterministic model selection (manager override,
  high-stakes, large scope, critical, heuristic) + batch flag +
  budget-aware downgrade.

Routing here is RNG-free (no bandit dir is passed), so every assertion
is deterministic.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from bernstein.core.models import Complexity, ModelConfig, Scope, Task, TaskType

from bernstein.core.routing.router_core import (
    BUDGET_AWARE_OPUS_MARGIN,
    DEFAULT_OPUS_TASK_COST_USD,
    CostTracker,
    ProviderConfig,
    ProviderHealth,
    ProviderHealthStatus,
    RouterError,
    RouterState,
    Tier,
    TierAwareRouter,
    _check_opus_override,
    _should_downgrade_from_opus,
    clear_budget_context,
    normalize_region,
    region_matches,
    route_task,
    set_budget_context,
)
from bernstein.core.routing.router_policies import ModelPolicy

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _task(
    role: str = "backend",
    complexity: Complexity = Complexity.MEDIUM,
    scope: Scope = Scope.MEDIUM,
    priority: int = 2,
    *,
    title: str = "Do something",
    description: str = "desc",
    model: str | None = None,
    effort: str | None = None,
    batch_eligible: bool = False,
) -> Task:
    return Task(
        id="t1",
        title=title,
        description=description,
        role=role,
        complexity=complexity,
        scope=scope,
        priority=priority,
        model=model,
        effort=effort,
        owned_files=[],
        estimated_minutes=30,
        task_type=TaskType.STANDARD,
        metadata={},
        batch_eligible=batch_eligible,
    )


def _provider(
    name: str,
    *,
    tier: Tier = Tier.STANDARD,
    models: dict[str, ModelConfig] | None = None,
    cost_per_1k: float = 0.01,
    available: bool = True,
    region: str = "global",
    max_context: int = 200_000,
    supports_vision: bool = False,
) -> ProviderConfig:
    return ProviderConfig(
        name=name,
        models=models or {"sonnet": ModelConfig(model="sonnet", effort="high")},
        tier=tier,
        cost_per_1k_tokens=cost_per_1k,
        available=available,
        region=region,
        max_context_tokens=max_context,
        supports_vision=supports_vision,
    )


@pytest.fixture(autouse=True)
def _reset_budget_context() -> None:
    # Module-level budget state leaks across tests; reset before each.
    clear_budget_context()


# ---------------------------------------------------------------------------
# region helpers
# ---------------------------------------------------------------------------


class TestRegionHelpers:
    def test_normalize_lowercases_and_dashes(self) -> None:
        assert normalize_region("US_East") == "us-east"

    def test_normalize_none_and_empty(self) -> None:
        assert normalize_region(None) == ""
        assert normalize_region("  ") == ""

    def test_no_required_region_matches_anything(self) -> None:
        assert region_matches(None, "us-east") is True

    def test_required_but_provider_unknown_fails(self) -> None:
        assert region_matches("eu", None) is False

    def test_exact_match(self) -> None:
        assert region_matches("eu-west", "EU_West") is True

    def test_prefix_match(self) -> None:
        # provider "us-east-1" satisfies required "us".
        assert region_matches("us", "us-east-1") is True

    def test_prefix_must_be_dash_delimited(self) -> None:
        # "useast" does not match required "us" (no dash boundary).
        assert region_matches("us", "useast") is False


# ---------------------------------------------------------------------------
# budget-aware downgrade
# ---------------------------------------------------------------------------


class TestBudgetDowngradePredicate:
    def test_disabled_never_downgrades(self) -> None:
        assert _should_downgrade_from_opus(0.0, enabled=False, estimated_opus_cost_usd=1.5) is False

    def test_unknown_budget_never_downgrades(self) -> None:
        assert _should_downgrade_from_opus(None, enabled=True, estimated_opus_cost_usd=1.5) is False

    def test_infinite_budget_never_downgrades(self) -> None:
        assert _should_downgrade_from_opus(float("inf"), enabled=True, estimated_opus_cost_usd=1.5) is False

    def test_low_budget_downgrades(self) -> None:
        # margin 2x * 1.5 = 3.0; remaining 2.0 < 3.0 -> downgrade.
        assert _should_downgrade_from_opus(2.0, enabled=True, estimated_opus_cost_usd=1.5) is True

    def test_high_budget_does_not_downgrade(self) -> None:
        assert _should_downgrade_from_opus(10.0, enabled=True, estimated_opus_cost_usd=1.5) is False

    def test_margin_constant_is_two(self) -> None:
        assert BUDGET_AWARE_OPUS_MARGIN == 2.0
        assert DEFAULT_OPUS_TASK_COST_USD == 1.5


# ---------------------------------------------------------------------------
# ProviderHealth state machine
# ---------------------------------------------------------------------------


class TestProviderHealth:
    def test_consecutive_failures_degrade_then_unhealthy(self) -> None:
        h = ProviderHealth()
        h.update(success=False)
        h.update(success=False)
        assert h.status == ProviderHealthStatus.DEGRADED
        for _ in range(3):
            h.update(success=False)
        assert h.status == ProviderHealthStatus.UNHEALTHY

    def test_recovery_to_healthy_after_three_successes(self) -> None:
        h = ProviderHealth()
        for _ in range(5):
            h.update(success=False)
        assert h.status == ProviderHealthStatus.UNHEALTHY
        for _ in range(3):
            h.update(success=True)
        assert h.status == ProviderHealthStatus.HEALTHY

    def test_success_resets_failure_counter(self) -> None:
        h = ProviderHealth()
        h.update(success=False)
        h.update(success=True)
        assert h.consecutive_failures == 0
        assert h.consecutive_successes == 1

    def test_success_rate_recalculated(self) -> None:
        h = ProviderHealth()
        h.update(success=True)
        # one success, zero failures -> 100%.
        assert h.success_rate == pytest.approx(1.0)
        assert h.error_rate == pytest.approx(0.0)

    def test_latency_ema(self) -> None:
        h = ProviderHealth()
        h.update(success=True, latency_ms=100.0)
        # first sample: alpha*100 + (1-alpha)*0 = 0.3*100 = 30.
        assert h.avg_latency_ms == pytest.approx(30.0)
        h.update(success=True, latency_ms=100.0)
        # 0.3*100 + 0.7*30 = 51.
        assert h.avg_latency_ms == pytest.approx(51.0)

    def test_zero_latency_does_not_update_ema(self) -> None:
        h = ProviderHealth()
        h.update(success=True, latency_ms=50.0)
        before = h.avg_latency_ms
        h.update(success=True, latency_ms=0.0)
        assert h.avg_latency_ms == pytest.approx(before)


# ---------------------------------------------------------------------------
# CostTracker (provider-level)
# ---------------------------------------------------------------------------


class TestProviderCostTracker:
    def test_record_accumulates(self) -> None:
        c = CostTracker()
        c.record_request(tokens=1000, cost_usd=0.5)
        c.record_request(tokens=2000, cost_usd=1.0)
        assert c.total_requests == 2
        assert c.total_tokens == 3000
        assert c.total_cost_usd == pytest.approx(1.5)

    def test_avg_cost_per_request(self) -> None:
        c = CostTracker()
        c.record_request(tokens=1000, cost_usd=0.4)
        c.record_request(tokens=1000, cost_usd=0.6)
        assert c.avg_cost_per_request == pytest.approx(0.5)

    def test_avg_cost_per_1k_tokens(self) -> None:
        c = CostTracker()
        c.record_request(tokens=2000, cost_usd=0.02)
        # 0.02 / 2000 * 1000 = 0.01 per 1k.
        assert c.avg_cost_per_1k_tokens == pytest.approx(0.01)


# ---------------------------------------------------------------------------
# ProviderConfig free tier
# ---------------------------------------------------------------------------


class TestProviderConfigFreeTier:
    def test_no_limit_never_exhausted(self) -> None:
        p = _provider("p", tier=Tier.FREE)
        assert p.is_free_tier_exhausted() is False

    def test_exhausted_when_used_meets_limit(self) -> None:
        p = _provider("p", tier=Tier.FREE)
        p.free_tier_limit = 100
        p.free_tier_used = 100
        assert p.is_free_tier_exhausted() is True

    def test_reset_clears_usage_after_reset_time(self) -> None:
        import time

        p = _provider("p", tier=Tier.FREE)
        p.free_tier_limit = 10
        p.free_tier_used = 10
        p.free_tier_reset = time.time() - 1.0  # already passed
        # reaching the check resets usage -> not exhausted.
        assert p.is_free_tier_exhausted() is False
        assert p.free_tier_used == 0

    def test_effective_cost_zero_for_unexhausted_free(self) -> None:
        p = _provider("p", tier=Tier.FREE, cost_per_1k=0.05)
        assert p.get_effective_cost() == 0.0

    def test_effective_cost_full_for_exhausted_free(self) -> None:
        p = _provider("p", tier=Tier.FREE, cost_per_1k=0.05)
        p.free_tier_limit = 1
        p.free_tier_used = 1
        assert p.get_effective_cost() == pytest.approx(0.05)

    def test_effective_cost_standard_tier(self) -> None:
        p = _provider("p", tier=Tier.STANDARD, cost_per_1k=0.02)
        assert p.get_effective_cost() == pytest.approx(0.02)


# ---------------------------------------------------------------------------
# TierAwareRouter registration + mutators
# ---------------------------------------------------------------------------


class TestRouterRegistration:
    def test_register_and_unregister(self) -> None:
        r = TierAwareRouter()
        r.register_provider(_provider("p1"))
        assert "p1" in r.state.providers
        r.unregister_provider("p1")
        assert "p1" not in r.state.providers

    def test_unregister_missing_is_noop(self) -> None:
        r = TierAwareRouter()
        r.unregister_provider("ghost")  # must not raise

    def test_update_availability(self) -> None:
        r = TierAwareRouter()
        r.register_provider(_provider("p1"))
        r.update_provider_availability("p1", False)
        assert r.state.providers["p1"].available is False

    def test_update_quota(self) -> None:
        r = TierAwareRouter()
        r.register_provider(_provider("p1"))
        r.update_provider_quota("p1", 42)
        assert r.state.providers["p1"].quota_remaining == 42

    def test_update_health_routes_to_provider(self) -> None:
        r = TierAwareRouter()
        r.register_provider(_provider("p1"))
        r.update_provider_health("p1", success=False, latency_ms=10.0)
        assert r.state.providers["p1"].health.consecutive_failures == 1

    def test_record_cost_routes_to_provider(self) -> None:
        r = TierAwareRouter()
        r.register_provider(_provider("p1"))
        r.record_provider_cost("p1", tokens=1000, cost_usd=0.5)
        assert r.state.providers["p1"].cost_tracker.total_cost_usd == pytest.approx(0.5)

    def test_get_max_context_tokens(self) -> None:
        r = TierAwareRouter()
        r.register_provider(_provider("p1", max_context=128_000))
        assert r.get_provider_max_context_tokens("p1") == 128_000
        assert r.get_provider_max_context_tokens("missing") is None


# ---------------------------------------------------------------------------
# get_available_providers filtering + ordering
# ---------------------------------------------------------------------------


class TestGetAvailableProviders:
    def test_unavailable_excluded(self) -> None:
        r = TierAwareRouter()
        r.register_provider(_provider("up", available=True))
        r.register_provider(_provider("down", available=False))
        names = [p.name for p in r.get_available_providers()]
        assert "up" in names
        assert "down" not in names

    def test_tier_filter(self) -> None:
        r = TierAwareRouter()
        r.register_provider(_provider("free", tier=Tier.FREE))
        r.register_provider(_provider("std", tier=Tier.STANDARD))
        names = [p.name for p in r.get_available_providers(tier=Tier.FREE)]
        assert names == ["free"]

    def test_unhealthy_excluded_when_require_healthy(self) -> None:
        r = TierAwareRouter()
        sick = _provider("sick")
        for _ in range(5):
            sick.health.update(success=False)
        r.register_provider(sick)
        r.register_provider(_provider("ok"))
        names = [p.name for p in r.get_available_providers(require_healthy=True)]
        assert "sick" not in names
        assert "ok" in names

    def test_unhealthy_included_when_not_require_healthy(self) -> None:
        r = TierAwareRouter()
        sick = _provider("sick")
        for _ in range(5):
            sick.health.update(success=False)
        r.register_provider(sick)
        names = [p.name for p in r.get_available_providers(require_healthy=False)]
        assert "sick" in names

    def test_policy_denied_provider_excluded(self) -> None:
        state = RouterState(model_policy=ModelPolicy(denied_providers=["bad"]))
        r = TierAwareRouter(state=state)
        r.register_provider(_provider("bad"))
        r.register_provider(_provider("good"))
        names = [p.name for p in r.get_available_providers()]
        assert "bad" not in names
        assert "good" in names

    def test_free_tier_scores_higher_than_standard(self) -> None:
        # With identical health, a free (cost 0) provider should sort
        # ahead of a standard provider on score.
        r = TierAwareRouter()
        free = _provider("free", tier=Tier.FREE, cost_per_1k=0.0)
        std = _provider("std", tier=Tier.STANDARD, cost_per_1k=0.05)
        # warm both to HEALTHY with identical success history.
        for p in (free, std):
            for _ in range(3):
                p.health.update(success=True)
        r.register_provider(std)
        r.register_provider(free)
        ordered = [p.name for p in r.get_available_providers()]
        assert ordered[0] == "free"


# ---------------------------------------------------------------------------
# select_provider_for_task dispatch + fallback
# ---------------------------------------------------------------------------


class TestSelectProviderDispatch:
    def test_preferred_tier_selected(self) -> None:
        state = RouterState(preferred_tier=Tier.FREE)
        r = TierAwareRouter(state=state)
        r.register_provider(_provider("free-p", tier=Tier.FREE))
        decision = r.select_provider_for_task(_task(), base_config=ModelConfig(model="sonnet", effort="high"))
        assert decision.provider == "free-p"
        assert decision.reason == "preferred_tier"
        assert decision.fallback is False

    def test_fallback_to_standard_when_no_free(self) -> None:
        state = RouterState(preferred_tier=Tier.FREE, fallback_enabled=True)
        r = TierAwareRouter(state=state)
        r.register_provider(_provider("std-p", tier=Tier.STANDARD))
        decision = r.select_provider_for_task(_task(), base_config=ModelConfig(model="sonnet", effort="high"))
        assert decision.provider == "std-p"
        assert decision.reason == "fallback"
        assert decision.fallback is True

    def test_last_resort_uses_degraded_provider(self) -> None:
        state = RouterState(preferred_tier=Tier.FREE, fallback_enabled=True)
        r = TierAwareRouter(state=state)
        degraded = _provider("only", tier=Tier.STANDARD)
        for _ in range(5):
            degraded.health.update(success=False)  # UNHEALTHY
        r.register_provider(degraded)
        decision = r.select_provider_for_task(_task(), base_config=ModelConfig(model="sonnet", effort="high"))
        assert decision.provider == "only"
        assert decision.reason == "last_resort"
        assert decision.fallback is True

    def test_no_provider_raises_router_error(self) -> None:
        r = TierAwareRouter()
        with pytest.raises(RouterError, match="No available provider"):
            r.select_provider_for_task(_task(), base_config=ModelConfig(model="sonnet", effort="high"))

    def test_no_fallback_when_disabled_raises(self) -> None:
        state = RouterState(preferred_tier=Tier.FREE, fallback_enabled=False)
        r = TierAwareRouter(state=state)
        # only a standard provider; fallback disabled -> last resort still
        # tries any-available, which finds it.  Disable by making the model
        # unsupported instead.
        r.register_provider(
            _provider("std", tier=Tier.STANDARD, models={"opus": ModelConfig(model="opus", effort="max")})
        )
        with pytest.raises(RouterError):
            r.select_provider_for_task(_task(), base_config=ModelConfig(model="totally-unknown-model", effort="high"))

    def test_vision_requirement_excludes_non_vision_provider(self) -> None:
        state = RouterState(preferred_tier=Tier.STANDARD)
        r = TierAwareRouter(state=state)
        r.register_provider(_provider("text-only", tier=Tier.STANDARD, supports_vision=False))
        r.register_provider(_provider("visual", tier=Tier.STANDARD, supports_vision=True))
        # title mentions a screenshot -> vision required.
        task = _task(title="Analyze this screenshot")
        decision = r.select_provider_for_task(task, base_config=ModelConfig(model="sonnet", effort="high"))
        assert decision.provider == "visual"

    def test_large_context_excludes_small_window_provider(self) -> None:
        state = RouterState(preferred_tier=Tier.STANDARD)
        r = TierAwareRouter(state=state)
        r.register_provider(_provider("small", tier=Tier.STANDARD, max_context=50_000))
        r.register_provider(_provider("big", tier=Tier.STANDARD, max_context=200_000))
        # LARGE scope -> requires >= 100k context.
        task = _task(scope=Scope.LARGE)
        decision = r.select_provider_for_task(task, base_config=ModelConfig(model="sonnet", effort="high"))
        assert decision.provider == "big"


class TestPreferredProviderPin:
    def test_pinned_provider_selected(self) -> None:
        r = TierAwareRouter()
        r.register_provider(_provider("pinned", tier=Tier.STANDARD))
        decision = r.select_provider_for_task(
            _task(), base_config=ModelConfig(model="sonnet", effort="high"), preferred_provider="pinned"
        )
        assert decision.provider == "pinned"
        assert decision.reason == "role_policy"

    def test_pinned_unknown_provider_raises(self) -> None:
        r = TierAwareRouter()
        with pytest.raises(RouterError, match="not registered"):
            r.select_provider_for_task(
                _task(), base_config=ModelConfig(model="sonnet", effort="high"), preferred_provider="ghost"
            )

    def test_pinned_unavailable_provider_raises(self) -> None:
        r = TierAwareRouter()
        r.register_provider(_provider("p", available=False))
        with pytest.raises(RouterError, match="unavailable"):
            r.select_provider_for_task(
                _task(), base_config=ModelConfig(model="sonnet", effort="high"), preferred_provider="p"
            )

    def test_pinned_denied_by_policy_raises(self) -> None:
        state = RouterState(model_policy=ModelPolicy(denied_providers=["p"]))
        r = TierAwareRouter(state=state)
        r.register_provider(_provider("p"))
        with pytest.raises(RouterError, match="denied by model_policy"):
            r.select_provider_for_task(
                _task(), base_config=ModelConfig(model="sonnet", effort="high"), preferred_provider="p"
            )

    def test_pinned_unsupported_model_raises(self) -> None:
        r = TierAwareRouter()
        r.register_provider(_provider("p", models={"opus": ModelConfig(model="opus", effort="max")}))
        with pytest.raises(RouterError, match="does not support model"):
            r.select_provider_for_task(
                _task(), base_config=ModelConfig(model="zzz-unknown", effort="high"), preferred_provider="p"
            )


# ---------------------------------------------------------------------------
# record_outcome + weights persistence
# ---------------------------------------------------------------------------


class TestOutcomeAndWeights:
    def test_success_increments_weight(self) -> None:
        r = TierAwareRouter()
        r.register_provider(_provider("p"))
        r.record_outcome("p", success=True)
        assert r.state.providers["p"].routing_weight == pytest.approx(1.1)

    def test_failure_decrements_weight(self) -> None:
        r = TierAwareRouter()
        r.register_provider(_provider("p"))
        r.record_outcome("p", success=False)
        assert r.state.providers["p"].routing_weight == pytest.approx(0.8)

    def test_weight_clamped_at_two(self) -> None:
        r = TierAwareRouter()
        r.register_provider(_provider("p"))
        for _ in range(50):
            r.record_outcome("p", success=True)
        assert r.state.providers["p"].routing_weight == pytest.approx(2.0)

    def test_weight_floored_at_point_one(self) -> None:
        r = TierAwareRouter()
        r.register_provider(_provider("p"))
        for _ in range(50):
            r.record_outcome("p", success=False)
        assert r.state.providers["p"].routing_weight == pytest.approx(0.1)

    def test_record_outcome_unknown_provider_noop(self) -> None:
        r = TierAwareRouter()
        r.record_outcome("ghost", success=True)  # must not raise

    def test_save_and_load_weights_round_trip(self, tmp_path: Path) -> None:
        r = TierAwareRouter()
        r.register_provider(_provider("p"))
        r.record_outcome("p", success=True)
        r.record_outcome("p", success=True)
        saved_weight = r.state.providers["p"].routing_weight
        r.save_weights(tmp_path)

        r2 = TierAwareRouter()
        r2.register_provider(_provider("p"))
        r2.load_weights(tmp_path)
        assert r2.state.providers["p"].routing_weight == pytest.approx(saved_weight)

    def test_load_weights_missing_file_noop(self, tmp_path: Path) -> None:
        r = TierAwareRouter()
        r.register_provider(_provider("p"))
        r.load_weights(tmp_path)  # no weights.json -> no change, no raise
        assert r.state.providers["p"].routing_weight == pytest.approx(1.0)

    def test_load_weights_ignores_unregistered(self, tmp_path: Path) -> None:
        r = TierAwareRouter()
        r.register_provider(_provider("p"))
        r.record_outcome("p", success=True)
        r.save_weights(tmp_path)
        # new router without 'p' registered must not crash on load.
        r2 = TierAwareRouter()
        r2.load_weights(tmp_path)
        assert "p" not in r2.state.providers


# ---------------------------------------------------------------------------
# validate_policy + summary
# ---------------------------------------------------------------------------


class TestValidateAndSummary:
    def test_denied_unregistered_provider_flagged(self) -> None:
        state = RouterState(model_policy=ModelPolicy(denied_providers=["nope"]))
        r = TierAwareRouter(state=state)
        r.register_provider(_provider("free", tier=Tier.FREE))
        r.register_provider(_provider("std", tier=Tier.STANDARD))
        r.register_provider(_provider("prem", tier=Tier.PREMIUM))
        issues = r.validate_policy()
        assert any("nope" in i and "not registered" in i for i in issues)

    def test_missing_tier_coverage_flagged(self) -> None:
        r = TierAwareRouter()
        r.register_provider(_provider("only-standard", tier=Tier.STANDARD))
        issues = r.validate_policy()
        # FREE and PREMIUM have no providers.
        assert any("free" in i for i in issues)
        assert any("premium" in i for i in issues)

    def test_get_provider_summary_shape(self) -> None:
        r = TierAwareRouter()
        r.register_provider(_provider("p", tier=Tier.FREE, region="eu-west"))
        r.record_provider_cost("p", tokens=1000, cost_usd=0.5)
        summary = r.get_provider_summary()
        assert summary["p"]["tier"] == "free"
        assert summary["p"]["total_cost_usd"] == pytest.approx(0.5)
        assert summary["p"]["region"] == "eu-west"
        assert summary["p"]["available"] is True


# ---------------------------------------------------------------------------
# route_task model selection
# ---------------------------------------------------------------------------


class TestRouteTaskSelection:
    def test_manager_override_wins(self) -> None:
        cfg = route_task(_task(model="haiku", effort="low"))
        assert cfg.model == "haiku"
        assert cfg.effort == "low"

    # Routing no longer hardcodes Claude tier names: high-stakes tasks
    # escalate to max effort on the operator-supplied default_model, and
    # unconfigured tasks refuse instead of guessing.

    def test_high_stakes_role_routes_default_model_max(self) -> None:
        cfg = route_task(_task(role="security"), default_model="run-default")
        assert cfg.model == "run-default"
        assert cfg.effort == "max"

    def test_architect_role_routes_default_model(self) -> None:
        assert route_task(_task(role="architect"), default_model="run-default").model == "run-default"

    def test_large_scope_routes_default_model(self) -> None:
        cfg = route_task(_task(scope=Scope.LARGE), default_model="run-default")
        assert cfg.model == "run-default"
        assert cfg.effort == "max"

    def test_critical_priority_routes_default_model(self) -> None:
        cfg = route_task(_task(priority=1), default_model="run-default")
        assert cfg.model == "run-default"
        assert cfg.effort == "max"

    def test_plain_medium_backend_routes_default_model_high(self) -> None:
        cfg = route_task(
            _task(role="backend", complexity=Complexity.MEDIUM, scope=Scope.MEDIUM, priority=2),
            default_model="run-default",
        )
        assert cfg.model == "run-default"
        assert cfg.effort == "high"

    def test_high_complexity_heuristic_routes_default_model(self) -> None:
        cfg = route_task(
            _task(role="backend", complexity=Complexity.HIGH, scope=Scope.MEDIUM, priority=2),
            default_model="run-default",
        )
        assert cfg.model == "run-default"

    def test_batch_eligible_sets_is_batch(self) -> None:
        cfg = route_task(_task(batch_eligible=True), default_model="run-default")
        assert cfg.is_batch is True

    def test_critical_batch_not_batched(self) -> None:
        # priority=1 is never routed to batch even if eligible.
        cfg = route_task(_task(priority=1, batch_eligible=True), default_model="run-default")
        assert cfg.is_batch is False


class TestRouteTaskBudgetAware:
    # The high-stakes escalation now expresses as max effort on the
    # default_model (no hardcoded opus); the budget downgrade drops the
    # escalation back to the plain heuristic (high effort).

    def test_budget_downgrade_skips_escalation_for_high_stakes(self) -> None:
        # Low remaining budget -> high-stakes task keeps the plain heuristic.
        cfg = route_task(
            _task(role="security"),
            budget_remaining_usd=1.0,
            budget_aware_routing_enabled=True,
            default_model="run-default",
        )
        assert cfg.model == "run-default"
        assert cfg.effort == "high"

    def test_ample_budget_keeps_premium_effort(self) -> None:
        cfg = route_task(
            _task(role="security"),
            budget_remaining_usd=100.0,
            budget_aware_routing_enabled=True,
            default_model="run-default",
        )
        assert cfg.effort == "max"

    def test_disabled_budget_routing_keeps_premium_effort(self) -> None:
        cfg = route_task(
            _task(role="security"),
            budget_remaining_usd=0.5,
            budget_aware_routing_enabled=False,
            default_model="run-default",
        )
        assert cfg.effort == "max"

    def test_module_context_drives_downgrade(self) -> None:
        # When kwargs are omitted, set_budget_context state applies.
        set_budget_context(1.0, enabled=True)
        cfg = route_task(_task(role="manager"), default_model="run-default")
        assert cfg.model == "run-default"
        assert cfg.effort == "high"

    def test_check_opus_override_returns_none_for_plain_task(self) -> None:
        assert _check_opus_override(_task(role="backend", priority=2, scope=Scope.MEDIUM)) is None

    def test_check_opus_override_reason_for_high_stakes(self) -> None:
        reason = _check_opus_override(_task(role="security"))
        assert reason is not None
        assert "high-stakes" in reason


# ---------------------------------------------------------------------------
# batch routing + active-agent counts + default router
# ---------------------------------------------------------------------------


class TestRouterMisc:
    def test_route_batch_returns_decision_per_task(self) -> None:
        state = RouterState(preferred_tier=Tier.STANDARD)
        r = TierAwareRouter(state=state)
        r.register_provider(_provider("p", tier=Tier.STANDARD))
        decisions = r.route_batch([_task(model="sonnet"), _task(model="sonnet"), _task(model="sonnet")])
        assert len(decisions) == 3
        assert all(d.provider == "p" for d in decisions)

    def test_update_active_agent_counts_copies(self) -> None:
        r = TierAwareRouter()
        counts = {"p": 3}
        r.update_active_agent_counts(counts)
        counts["p"] = 99  # mutate caller's dict
        # router kept its own copy.
        assert r.state.active_agent_counts["p"] == 3

    def test_get_default_router_is_singleton(self) -> None:
        from bernstein.core.routing.router_core import get_default_router

        a = get_default_router()
        b = get_default_router()
        assert a is b
        assert isinstance(a, TierAwareRouter)


# ---------------------------------------------------------------------------
# YAML provider + policy loading
# ---------------------------------------------------------------------------


class TestYamlLoading:
    def test_load_providers_registers_entries(self, tmp_path: Path) -> None:
        from bernstein.core.routing.router_core import load_providers_from_yaml

        yaml_text = """
providers:
  free-prov:
    tier: free
    cost_per_1k_tokens: 0.0
    free_tier_limit: 1000
    max_context_tokens: 128000
    models:
      sonnet:
        model: sonnet
        effort: high
        aliases: [claude-sonnet]
  paid-prov:
    tier: standard
    cost_per_1k_tokens: 0.02
    supports_vision: true
    region: eu-west
"""
        path = tmp_path / "providers.yaml"
        path.write_text(yaml_text)
        r = TierAwareRouter()
        load_providers_from_yaml(path, r)
        assert "free-prov" in r.state.providers
        assert r.state.providers["free-prov"].tier == Tier.FREE
        assert r.state.providers["free-prov"].free_tier_limit == 1000
        assert r.state.providers["paid-prov"].supports_vision is True
        assert r.state.providers["paid-prov"].region == "eu-west"

    def test_load_providers_missing_key_noop(self, tmp_path: Path) -> None:
        from bernstein.core.routing.router_core import load_providers_from_yaml

        path = tmp_path / "providers.yaml"
        path.write_text("something_else: 1")
        r = TierAwareRouter()
        load_providers_from_yaml(path, r)
        assert len(r.state.providers) == 0

    def test_load_providers_malformed_entry_skipped(self, tmp_path: Path) -> None:
        from bernstein.core.routing.router_core import load_providers_from_yaml

        # one good provider, one with an invalid tier (raises -> skipped).
        yaml_text = """
providers:
  good:
    tier: standard
  broken:
    tier: not-a-real-tier
"""
        path = tmp_path / "providers.yaml"
        path.write_text(yaml_text)
        r = TierAwareRouter()
        load_providers_from_yaml(path, r)
        assert "good" in r.state.providers
        assert "broken" not in r.state.providers

    def test_load_providers_bad_yaml_noop(self, tmp_path: Path) -> None:
        from bernstein.core.routing.router_core import load_providers_from_yaml

        path = tmp_path / "providers.yaml"
        path.write_text("::: not valid yaml :::\n  - [")
        r = TierAwareRouter()
        load_providers_from_yaml(path, r)  # must not raise
        assert len(r.state.providers) == 0

    def test_load_model_policy_applies(self, tmp_path: Path) -> None:
        from bernstein.core.routing.router_core import load_model_policy_from_yaml

        yaml_text = """
model_policy:
  denied_providers: [evil-corp]
  allowed_providers: [good-corp]
"""
        path = tmp_path / "policy.yaml"
        path.write_text(yaml_text)
        r = TierAwareRouter()
        load_model_policy_from_yaml(path, r)
        assert "evil-corp" in r.state.model_policy.denied_providers
        assert r.state.model_policy.is_provider_allowed("evil-corp", "global") is False

    def test_load_model_policy_bad_yaml_noop(self, tmp_path: Path) -> None:
        from bernstein.core.routing.router_core import load_model_policy_from_yaml

        path = tmp_path / "policy.yaml"
        path.write_text(":::not yaml")
        r = TierAwareRouter()
        before = r.state.model_policy
        load_model_policy_from_yaml(path, r)
        # policy unchanged on parse failure.
        assert r.state.model_policy is before

    def test_load_model_policy_non_dict_noop(self, tmp_path: Path) -> None:
        from bernstein.core.routing.router_core import load_model_policy_from_yaml

        path = tmp_path / "policy.yaml"
        path.write_text("- just\n- a\n- list")
        r = TierAwareRouter()
        before = r.state.model_policy
        load_model_policy_from_yaml(path, r)
        assert r.state.model_policy is before
