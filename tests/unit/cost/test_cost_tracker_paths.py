"""Behavioural tests for ``cost_tracker`` dark paths.

Targets the per-run budget tracker's observable contracts:

* ``estimate_cost`` with the per-1M pricing table (exact arithmetic) and
  the blended-rate fallback for unknown models.
* ``resolve_run_budget_usd`` layered precedence + bad-env handling.
* ``EnvelopeConfig`` normalisation, allowlist matching, (de)serialisation.
* ``CostTracker.record`` aggregation into ``spent_by_model`` /
  ``spent_for_agent`` / envelope rollups; explicit-cost vs estimated.
* ``record_cumulative`` delta-safe accounting + cost proration.
* Envelope hard caps (``EnvelopeBudgetError``), allowlist refusal, and
  the threshold hook firing exactly once.
* ``status`` soft thresholds, hard-cap kill switch, unlimited budget.
* ``agent_summaries`` / ``model_breakdowns`` ordering + token buckets.
* ``project`` confidence ramp + within-budget flag.
* ``cache_savings_usd`` from cache-read pricing delta.
* ``save`` / ``load`` round-trip preserving aggregates and envelopes.
* Usage-buffer eviction with JSONL rotation; accumulators stay exact.

All numeric assertions are pinned against the real pricing tables so a
behaviour change is caught.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.cost.cost_tracker import (
    DEFAULT_QUOTA_ENVELOPE,
    EnvelopeBudgetError,
    EnvelopeConfig,
    TokenUsage,
    estimate_cost,
    resolve_run_budget_usd,
)
from bernstein.core.cost.cost_tracker import CostTracker as Tracker

# ---------------------------------------------------------------------------
# estimate_cost
# ---------------------------------------------------------------------------


class TestEstimateCost:
    def test_sonnet_input_one_million_tokens(self) -> None:
        # sonnet input pricing is $3 / 1M tokens.
        assert estimate_cost("sonnet", 1_000_000, 0) == pytest.approx(3.0)

    def test_sonnet_output_one_million_tokens(self) -> None:
        # sonnet output pricing is $15 / 1M tokens.
        assert estimate_cost("sonnet", 0, 1_000_000) == pytest.approx(15.0)

    def test_opus_input_output_combined(self) -> None:
        # opus: input $5/1M, output $25/1M.
        cost = estimate_cost("opus", 200_000, 100_000)
        assert cost == pytest.approx(0.2 * 5.0 + 0.1 * 25.0)

    def test_cache_read_cheaper_than_input(self) -> None:
        # sonnet cache_read is $0.3/1M vs $3/1M input.
        cost = estimate_cost("sonnet", 0, 0, cache_read_tokens=1_000_000)
        assert cost == pytest.approx(0.3)

    def test_cache_write_pricing(self) -> None:
        # sonnet cache_write is $3.75/1M.
        cost = estimate_cost("sonnet", 0, 0, cache_write_tokens=1_000_000)
        assert cost == pytest.approx(3.75)

    def test_model_name_substring_match_case_insensitive(self) -> None:
        # "claude-sonnet-4" contains "sonnet" -> uses sonnet pricing.
        a = estimate_cost("claude-sonnet-4-20990101", 1_000_000, 0)
        assert a == pytest.approx(3.0)

    def test_unknown_model_uses_blended_fallback(self) -> None:
        # Totally unknown model -> safe fallback blended rate 0.005/1k.
        cost = estimate_cost("totally-made-up-model-xyz", 1000, 0)
        assert cost == pytest.approx(0.005)

    def test_o3_uses_detailed_per_million_pricing(self) -> None:
        # o3 is in the per-1M table (input $2/1M) so detailed pricing wins
        # over the blended fallback: 2000 input tokens => $0.004.
        assert estimate_cost("o3", 2000, 0) == pytest.approx(0.004)

    def test_haiku_combined_input_output(self) -> None:
        # haiku: input $1/1M, output $5/1M.
        cost = estimate_cost("haiku", 1_000_000, 1_000_000)
        assert cost == pytest.approx(6.0)

    def test_zero_tokens_zero_cost(self) -> None:
        assert estimate_cost("sonnet", 0, 0) == 0.0


# ---------------------------------------------------------------------------
# resolve_run_budget_usd
# ---------------------------------------------------------------------------


class TestResolveRunBudget:
    def test_env_var_takes_precedence(self) -> None:
        env = {"BERNSTEIN_MAX_COST_USD": "12.5"}
        assert resolve_run_budget_usd(run_config_value=5.0, seed_value=3.0, env=env) == 12.5

    def test_run_config_over_seed(self) -> None:
        assert resolve_run_budget_usd(run_config_value=5.0, seed_value=3.0, env={}) == 5.0

    def test_seed_when_no_run_config(self) -> None:
        assert resolve_run_budget_usd(run_config_value=None, seed_value=3.0, env={}) == 3.0

    def test_default_when_nothing_set(self) -> None:
        assert resolve_run_budget_usd(env={}, default=2.0) == 2.0

    def test_invalid_env_falls_through(self) -> None:
        env = {"BERNSTEIN_MAX_COST_USD": "not-a-float"}
        assert resolve_run_budget_usd(run_config_value=7.0, env=env) == 7.0

    def test_negative_env_normalised_to_zero(self) -> None:
        env = {"BERNSTEIN_MAX_COST_USD": "-5"}
        assert resolve_run_budget_usd(env=env) == 0.0

    def test_blank_env_ignored(self) -> None:
        env = {"BERNSTEIN_MAX_COST_USD": "   "}
        assert resolve_run_budget_usd(seed_value=4.0, env=env) == 4.0

    def test_nonpositive_run_config_skipped(self) -> None:
        # run_config of 0 is not "set"; seed wins.
        assert resolve_run_budget_usd(run_config_value=0.0, seed_value=9.0, env={}) == 9.0


# ---------------------------------------------------------------------------
# EnvelopeConfig
# ---------------------------------------------------------------------------


class TestEnvelopeConfig:
    def test_negative_budget_normalised_to_zero(self) -> None:
        cfg = EnvelopeConfig(name="e", budget_usd=-3.0)
        assert cfg.budget_usd == 0.0

    def test_negative_hard_budget_normalised(self) -> None:
        cfg = EnvelopeConfig(name="e", hard_budget_usd=-1.0)
        assert cfg.hard_budget_usd == 0.0

    def test_out_of_range_threshold_resets_to_default(self) -> None:
        assert EnvelopeConfig(name="e", threshold_pct=1.5).threshold_pct == pytest.approx(0.80)
        assert EnvelopeConfig(name="e", threshold_pct=0.0).threshold_pct == pytest.approx(0.80)

    def test_valid_threshold_kept(self) -> None:
        assert EnvelopeConfig(name="e", threshold_pct=0.5).threshold_pct == 0.5

    def test_empty_allowlist_allows_any_model(self) -> None:
        assert EnvelopeConfig(name="e").model_allowed("anything")

    def test_allowlist_substring_case_insensitive(self) -> None:
        cfg = EnvelopeConfig(name="e", model_allowlist=("sonnet",))
        assert cfg.model_allowed("claude-SONNET-4")
        assert not cfg.model_allowed("opus")

    def test_to_dict_from_dict_round_trip(self) -> None:
        cfg = EnvelopeConfig(name="e", budget_usd=10.0, hard_budget_usd=20.0, model_allowlist=("haiku", "sonnet"))
        rebuilt = EnvelopeConfig.from_dict("e", cfg.to_dict())
        assert rebuilt == cfg

    def test_from_dict_coerces_allowlist_strings(self) -> None:
        cfg = EnvelopeConfig.from_dict("e", {"model_allowlist": ["sonnet", "", "opus"]})
        # blank entries dropped.
        assert cfg.model_allowlist == ("sonnet", "opus")


# ---------------------------------------------------------------------------
# record / spent_by_model / spent_for_agent
# ---------------------------------------------------------------------------


class TestRecordAggregation:
    def test_record_uses_explicit_cost(self) -> None:
        t = Tracker(run_id="r")
        status = t.record("agent-a", "task-1", "sonnet", 100, 50, cost_usd=0.25)
        assert status.spent_usd == pytest.approx(0.25)
        assert t.spent_usd == pytest.approx(0.25)

    def test_record_estimates_cost_when_omitted(self) -> None:
        t = Tracker(run_id="r")
        t.record("agent-a", "task-1", "sonnet", 1_000_000, 0)
        # sonnet input is $3/1M.
        assert t.spent_usd == pytest.approx(3.0)

    def test_spent_by_model_aggregates_per_model(self) -> None:
        t = Tracker(run_id="r")
        t.record("a", "t", "sonnet", 0, 0, cost_usd=1.0)
        t.record("a", "t", "sonnet", 0, 0, cost_usd=2.0)
        t.record("a", "t", "opus", 0, 0, cost_usd=4.0)
        by_model = t.spent_by_model()
        assert by_model["sonnet"] == pytest.approx(3.0)
        assert by_model["opus"] == pytest.approx(4.0)

    def test_spent_by_model_returns_copy(self) -> None:
        t = Tracker(run_id="r")
        t.record("a", "t", "sonnet", 0, 0, cost_usd=1.0)
        snapshot = t.spent_by_model()
        snapshot["sonnet"] = 999.0
        # mutating the snapshot must not change tracker state.
        assert t.spent_by_model()["sonnet"] == pytest.approx(1.0)

    def test_spent_for_agent_isolated(self) -> None:
        t = Tracker(run_id="r")
        t.record("agent-a", "t", "sonnet", 0, 0, cost_usd=1.0)
        t.record("agent-b", "t", "sonnet", 0, 0, cost_usd=3.0)
        assert t.spent_for_agent("agent-a") == pytest.approx(1.0)
        assert t.spent_for_agent("agent-b") == pytest.approx(3.0)

    def test_spent_for_unknown_agent_is_zero(self) -> None:
        t = Tracker(run_id="r")
        assert t.spent_for_agent("nope") == 0.0

    def test_total_usages_recorded_counts_all(self) -> None:
        t = Tracker(run_id="r")
        for _ in range(5):
            t.record("a", "t", "sonnet", 1, 1, cost_usd=0.01)
        assert t.total_usages_recorded == 5
        assert len(t.usages) == 5

    def test_record_attaches_role_and_feature_tags(self) -> None:
        t = Tracker(run_id="r")
        t.record("a", "t", "sonnet", 1, 1, cost_usd=0.01, role="backend", feature_label="auth")
        usage = t.usages[-1]
        assert usage.cost_tags["role"] == "backend"
        assert usage.cost_tags["feature_label"] == "auth"

    def test_record_default_envelope(self) -> None:
        t = Tracker(run_id="r")
        t.record("a", "t", "sonnet", 1, 1, cost_usd=0.5)
        assert t.spent_by_envelope()[DEFAULT_QUOTA_ENVELOPE] == pytest.approx(0.5)
        assert t.calls_by_envelope()[DEFAULT_QUOTA_ENVELOPE] == 1


# ---------------------------------------------------------------------------
# record_cumulative (delta-safe)
# ---------------------------------------------------------------------------


class TestRecordCumulative:
    def test_first_call_records_full_total(self) -> None:
        t = Tracker(run_id="r")
        delta = t.record_cumulative("a", "t", "sonnet", 1000, 500, total_cost_usd=0.3)
        assert delta == pytest.approx(0.3)
        assert t.spent_usd == pytest.approx(0.3)

    def test_second_call_records_only_delta(self) -> None:
        t = Tracker(run_id="r")
        t.record_cumulative("a", "t", "sonnet", 1000, 0, total_cost_usd=1.0)
        # cumulative grows to 2000 input tokens; prorated cost.
        delta = t.record_cumulative("a", "t", "sonnet", 2000, 0, total_cost_usd=2.0)
        # second call prorates total_cost over the delta tokens fraction.
        assert delta > 0.0
        assert t.spent_usd == pytest.approx(1.0 + delta)

    def test_no_new_tokens_returns_zero(self) -> None:
        t = Tracker(run_id="r")
        t.record_cumulative("a", "t", "sonnet", 1000, 500, total_cost_usd=1.0)
        delta = t.record_cumulative("a", "t", "sonnet", 1000, 500, total_cost_usd=1.0)
        assert delta == 0.0

    def test_decreasing_totals_clamped_no_negative(self) -> None:
        t = Tracker(run_id="r")
        t.record_cumulative("a", "t", "sonnet", 5000, 2000, total_cost_usd=1.0)
        before = t.spent_usd
        # a smaller "total" than before means no positive delta -> no record.
        delta = t.record_cumulative("a", "t", "sonnet", 100, 50, total_cost_usd=0.5)
        assert delta == 0.0
        assert t.spent_usd == pytest.approx(before)

    def test_distinct_keys_tracked_separately(self) -> None:
        t = Tracker(run_id="r")
        t.record_cumulative("a", "t1", "sonnet", 1000, 0, total_cost_usd=1.0)
        t.record_cumulative("a", "t2", "sonnet", 1000, 0, total_cost_usd=1.0)
        # different task ids => two separate cumulative baselines.
        assert t.spent_usd == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# Envelope hard caps + allowlist + threshold hook
# ---------------------------------------------------------------------------


class TestEnvelopeEnforcement:
    def test_hard_cap_breach_raises(self) -> None:
        t = Tracker(run_id="r")
        t.configure_envelopes({"sub": EnvelopeConfig(name="sub", hard_budget_usd=1.0)})
        t.record("a", "t", "sonnet", 0, 0, cost_usd=0.5, quota_envelope="sub")
        with pytest.raises(EnvelopeBudgetError) as exc:
            t.record("a", "t", "sonnet", 0, 0, cost_usd=0.75, quota_envelope="sub")
        assert exc.value.envelope == "sub"
        assert exc.value.cap_usd == pytest.approx(1.0)

    def test_hard_cap_rejection_does_not_mutate_state(self) -> None:
        t = Tracker(run_id="r")
        t.configure_envelopes({"sub": EnvelopeConfig(name="sub", hard_budget_usd=1.0)})
        t.record("a", "t", "sonnet", 0, 0, cost_usd=0.5, quota_envelope="sub")
        with pytest.raises(EnvelopeBudgetError):
            t.record("a", "t", "sonnet", 0, 0, cost_usd=5.0, quota_envelope="sub")
        # spend stays at the pre-rejection value.
        assert t.spent_by_envelope()["sub"] == pytest.approx(0.5)
        assert t.spent_usd == pytest.approx(0.5)

    def test_allowlist_refusal_raises(self) -> None:
        t = Tracker(run_id="r")
        t.configure_envelopes({"sub": EnvelopeConfig(name="sub", model_allowlist=("haiku",))})
        with pytest.raises(EnvelopeBudgetError) as exc:
            t.record("a", "t", "opus", 0, 0, cost_usd=0.1, quota_envelope="sub")
        assert "allowlist" in exc.value.reason

    def test_threshold_hook_fires_once(self) -> None:
        fired: list[str] = []
        t = Tracker(run_id="r")
        t.configure_envelopes({"sub": EnvelopeConfig(name="sub", budget_usd=1.0, threshold_pct=0.8)})
        t.set_envelope_threshold_hook(lambda report: fired.append(report.name))
        # Below 80% -> no fire.
        t.record("a", "t", "sonnet", 0, 0, cost_usd=0.5, quota_envelope="sub")
        assert fired == []
        # Crosses 80% -> fires once.
        t.record("a", "t", "sonnet", 0, 0, cost_usd=0.4, quota_envelope="sub")
        assert fired == ["sub"]
        # Further spend over the watermark -> no repeat fire.
        t.record("a", "t", "sonnet", 0, 0, cost_usd=0.05, quota_envelope="sub")
        assert fired == ["sub"]

    def test_envelope_report_reflects_spend(self) -> None:
        t = Tracker(run_id="r")
        t.configure_envelopes({"sub": EnvelopeConfig(name="sub", budget_usd=10.0, hard_budget_usd=20.0)})
        t.record("a", "t", "sonnet", 0, 0, cost_usd=5.0, quota_envelope="sub")
        rep = t.envelope_report("sub")
        assert rep.spent_usd == pytest.approx(5.0)
        assert rep.cap_usd == pytest.approx(10.0)
        assert rep.pct_used == pytest.approx(0.5)
        assert rep.remaining_usd == pytest.approx(5.0)
        assert rep.hard_remaining_usd == pytest.approx(15.0)
        assert not rep.hard_breached

    def test_envelope_reports_includes_configured_and_spent(self) -> None:
        t = Tracker(run_id="r")
        t.configure_envelopes({"cfg-only": EnvelopeConfig(name="cfg-only", budget_usd=5.0)})
        t.record("a", "t", "sonnet", 0, 0, cost_usd=1.0, quota_envelope="spent-only")
        reports = t.envelope_reports()
        # both the configured-but-unspent and the spent-but-unconfigured show.
        assert "cfg-only" in reports
        assert "spent-only" in reports
        assert reports["spent-only"].spent_usd == pytest.approx(1.0)

    def test_uncapped_envelope_report_infinite_remaining(self) -> None:
        import math

        t = Tracker(run_id="r")
        t.record("a", "t", "sonnet", 0, 0, cost_usd=1.0, quota_envelope="free")
        rep = t.envelope_report("free")
        assert math.isinf(rep.remaining_usd)
        assert rep.to_dict()["remaining_usd"] is None


# ---------------------------------------------------------------------------
# status thresholds + hard cap
# ---------------------------------------------------------------------------


class TestStatus:
    def test_unlimited_budget_never_stops(self) -> None:
        t = Tracker(run_id="r", budget_usd=0.0)
        t.record("a", "t", "opus", 0, 0, cost_usd=1000.0)
        status = t.status()
        assert status.should_stop is False
        assert status.percentage_used == 0.0
        import math

        assert math.isinf(status.remaining_usd)

    def test_warn_threshold_trips(self) -> None:
        t = Tracker(run_id="r", budget_usd=10.0, warn_threshold=0.8)
        t.record("a", "t", "sonnet", 0, 0, cost_usd=8.5)
        status = t.status()
        assert status.should_warn is True
        assert status.should_stop is False
        assert status.percentage_used == pytest.approx(0.85)

    def test_hard_stop_threshold_trips(self) -> None:
        t = Tracker(run_id="r", budget_usd=10.0)
        t.record("a", "t", "opus", 0, 0, cost_usd=10.0)
        assert t.status().should_stop is True

    def test_remaining_clamped_at_zero(self) -> None:
        t = Tracker(run_id="r", budget_usd=10.0)
        t.record("a", "t", "opus", 0, 0, cost_usd=15.0)
        assert t.status().remaining_usd == 0.0

    def test_hard_cap_kill_switch_overrides_unlimited(self) -> None:
        t = Tracker(run_id="r", budget_usd=0.0, hard_budget_usd=5.0)
        t.record("a", "t", "opus", 0, 0, cost_usd=5.0)
        status = t.status()
        assert status.should_stop is True
        assert status.should_warn is True

    def test_can_spawn_false_after_hard_cap(self) -> None:
        t = Tracker(run_id="r", hard_budget_usd=2.0)
        t.record("a", "t", "sonnet", 0, 0, cost_usd=2.0)
        assert t.can_spawn() is False

    def test_can_spawn_true_under_soft_cap(self) -> None:
        t = Tracker(run_id="r", budget_usd=10.0)
        t.record("a", "t", "sonnet", 0, 0, cost_usd=1.0)
        assert t.can_spawn() is True

    def test_can_spawn_envelope_hard_cap(self) -> None:
        t = Tracker(run_id="r")
        t.configure_envelopes({"sub": EnvelopeConfig(name="sub", hard_budget_usd=1.0)})
        t.record("a", "t", "sonnet", 0, 0, cost_usd=1.0, quota_envelope="sub")
        assert t.can_spawn(quota_envelope="sub") is False
        # a different envelope is unaffected.
        assert t.can_spawn(quota_envelope="other") is True

    def test_status_to_dict_round_number(self) -> None:
        t = Tracker(run_id="r", budget_usd=3.0)
        t.record("a", "t", "sonnet", 0, 0, cost_usd=1.0)
        d = t.status().to_dict()
        assert d["run_id"] == "r"
        assert d["percentage_used"] == pytest.approx(0.3333, abs=1e-4)


# ---------------------------------------------------------------------------
# breakdowns + projection + cache savings
# ---------------------------------------------------------------------------


class TestBreakdownsAndProjection:
    def test_agent_summaries_sorted_by_cost_desc(self) -> None:
        t = Tracker(run_id="r")
        t.record("cheap", "t", "sonnet", 0, 0, cost_usd=1.0)
        t.record("expensive", "t", "opus", 0, 0, cost_usd=9.0)
        summaries = t.agent_summaries()
        assert [s.agent_id for s in summaries] == ["expensive", "cheap"]
        assert summaries[0].total_cost_usd == pytest.approx(9.0)
        assert summaries[1].task_count == 1

    def test_agent_summary_model_breakdown(self) -> None:
        t = Tracker(run_id="r")
        t.record("a", "t", "sonnet", 0, 0, cost_usd=1.0)
        t.record("a", "t", "opus", 0, 0, cost_usd=2.0)
        summary = t.agent_summaries()[0]
        assert summary.model_breakdown["sonnet"] == pytest.approx(1.0)
        assert summary.model_breakdown["opus"] == pytest.approx(2.0)

    def test_model_breakdowns_token_buckets(self) -> None:
        t = Tracker(run_id="r")
        t.record("a", "t", "sonnet", 100, 50, cost_usd=1.0, cache_read_tokens=10, cache_write_tokens=5)
        bd = t.model_breakdowns()[0]
        assert bd.model == "sonnet"
        assert bd.input_tokens == 100
        assert bd.output_tokens == 50
        assert bd.cache_read_tokens == 10
        assert bd.cache_write_tokens == 5
        assert bd.total_tokens == 165
        assert bd.invocation_count == 1

    def test_model_breakdowns_sorted_by_cost_desc(self) -> None:
        t = Tracker(run_id="r")
        t.record("a", "t", "haiku", 0, 0, cost_usd=0.5)
        t.record("a", "t", "opus", 0, 0, cost_usd=5.0)
        models = [b.model for b in t.model_breakdowns()]
        assert models == ["opus", "haiku"]

    def test_project_no_tasks_zero_confidence(self) -> None:
        t = Tracker(run_id="r")
        t.record("a", "t", "sonnet", 0, 0, cost_usd=1.0)
        proj = t.project(tasks_done=0, tasks_remaining=10)
        assert proj.confidence == 0.0
        assert proj.avg_cost_per_task_usd == 0.0
        assert proj.projected_total_usd == pytest.approx(1.0)

    def test_project_avg_extrapolation(self) -> None:
        t = Tracker(run_id="r")
        t.record("a", "t", "sonnet", 0, 0, cost_usd=4.0)
        proj = t.project(tasks_done=2, tasks_remaining=3)
        # avg = 4/2 = 2; projected = 4 + 2*3 = 10.
        assert proj.avg_cost_per_task_usd == pytest.approx(2.0)
        assert proj.projected_total_usd == pytest.approx(10.0)

    def test_project_confidence_caps_at_one(self) -> None:
        t = Tracker(run_id="r")
        t.record("a", "t", "sonnet", 0, 0, cost_usd=1.0)
        proj = t.project(tasks_done=10, tasks_remaining=0)
        assert proj.confidence == 1.0

    def test_project_within_budget_flag(self) -> None:
        t = Tracker(run_id="r", budget_usd=5.0)
        t.record("a", "t", "sonnet", 0, 0, cost_usd=1.0)
        # avg 1.0/task; 3 remaining -> projected 4.0 <= 5.0 budget.
        assert t.project(tasks_done=1, tasks_remaining=3).within_budget is True
        # 10 remaining -> projected 11.0 > 5.0.
        assert t.project(tasks_done=1, tasks_remaining=10).within_budget is False

    def test_cache_savings_from_cache_reads(self) -> None:
        t = Tracker(run_id="r")
        # sonnet: input $3/1M, cache_read $0.3/1M -> savings $2.7/1M.
        t.record("a", "t", "sonnet", 0, 0, cost_usd=0.3, cache_read_tokens=1_000_000)
        assert t.cache_savings_usd() == pytest.approx(2.7)

    def test_no_cache_reads_zero_savings(self) -> None:
        t = Tracker(run_id="r")
        t.record("a", "t", "sonnet", 100, 100, cost_usd=0.1)
        assert t.cache_savings_usd() == 0.0

    def test_report_assembles_sections(self) -> None:
        t = Tracker(run_id="r", budget_usd=10.0)
        t.record("a", "t", "sonnet", 100, 50, cost_usd=1.0)
        t.record("b", "t", "opus", 200, 100, cost_usd=2.0)
        report = t.report(tasks_done=2, tasks_remaining=2)
        assert report.total_spent_usd == pytest.approx(3.0)
        assert len(report.per_agent) == 2
        assert len(report.per_model) == 2
        assert report.projection is not None
        assert report.projection.tasks_done == 2

    def test_report_without_tasks_has_no_projection(self) -> None:
        t = Tracker(run_id="r")
        t.record("a", "t", "sonnet", 1, 1, cost_usd=0.1)
        assert t.report().projection is None


# ---------------------------------------------------------------------------
# save / load round-trip
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_save_writes_expected_path(self, tmp_path: Path) -> None:
        t = Tracker(run_id="run-xyz", budget_usd=10.0)
        t.record("a", "t", "sonnet", 100, 50, cost_usd=1.0)
        out = t.save(tmp_path)
        assert out == tmp_path / "runtime" / "costs" / "run-xyz.json"
        assert out.exists()

    def test_load_round_trip_preserves_aggregates(self, tmp_path: Path) -> None:
        t = Tracker(run_id="run-1", budget_usd=20.0, hard_budget_usd=30.0)
        t.record("agent-a", "t1", "sonnet", 100, 50, cost_usd=1.0)
        t.record("agent-b", "t2", "opus", 200, 100, cost_usd=2.0)
        t.save(tmp_path)

        loaded = Tracker.load(tmp_path, "run-1")
        assert loaded is not None
        assert loaded.run_id == "run-1"
        assert loaded.budget_usd == pytest.approx(20.0)
        assert loaded.hard_budget_usd == pytest.approx(30.0)
        assert loaded.spent_usd == pytest.approx(3.0)
        assert loaded.spent_by_model()["sonnet"] == pytest.approx(1.0)
        assert loaded.spent_by_model()["opus"] == pytest.approx(2.0)
        assert loaded.spent_for_agent("agent-a") == pytest.approx(1.0)

    def test_load_rebuilds_model_breakdowns(self, tmp_path: Path) -> None:
        t = Tracker(run_id="run-2")
        t.record("a", "t", "sonnet", 100, 50, cost_usd=1.0)
        t.save(tmp_path)
        loaded = Tracker.load(tmp_path, "run-2")
        assert loaded is not None
        # accumulators must be rebuilt so breakdowns survive reload.
        bd = loaded.model_breakdowns()
        assert len(bd) == 1
        assert bd[0].input_tokens == 100

    def test_load_preserves_envelope_config(self, tmp_path: Path) -> None:
        t = Tracker(run_id="run-3")
        t.configure_envelopes({"sub": EnvelopeConfig(name="sub", budget_usd=5.0, hard_budget_usd=10.0)})
        t.record("a", "t", "sonnet", 0, 0, cost_usd=1.0, quota_envelope="sub")
        t.save(tmp_path)
        loaded = Tracker.load(tmp_path, "run-3")
        assert loaded is not None
        assert "sub" in loaded.envelopes
        assert loaded.envelopes["sub"].budget_usd == pytest.approx(5.0)
        assert loaded.spent_by_envelope()["sub"] == pytest.approx(1.0)

    def test_load_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert Tracker.load(tmp_path, "does-not-exist") is None

    def test_load_corrupt_file_returns_none(self, tmp_path: Path) -> None:
        costs_dir = tmp_path / "runtime" / "costs"
        costs_dir.mkdir(parents=True)
        (costs_dir / "bad.json").write_text("{ not valid json")
        assert Tracker.load(tmp_path, "bad") is None

    def test_save_metrics_writes_report(self, tmp_path: Path) -> None:
        t = Tracker(run_id="run-m")
        t.record("a", "t", "sonnet", 10, 5, cost_usd=0.5)
        out = t.save_metrics(tmp_path)
        assert out == tmp_path / "costs_run-m.json"
        payload = json.loads(out.read_text())
        assert payload["run_id"] == "run-m"
        assert payload["total_spent_usd"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# usage buffer eviction + rotation
# ---------------------------------------------------------------------------


class TestUsageBufferEviction:
    def test_buffer_caps_in_memory_rows(self) -> None:
        t = Tracker(run_id="r", usage_buffer_size=3)
        for _ in range(5):
            t.record("a", "t", "sonnet", 1, 1, cost_usd=0.01)
        # ring buffer holds at most 3 rows.
        assert len(t.usages) == 3
        # but the total count is exact.
        assert t.total_usages_recorded == 5

    def test_accumulators_exact_after_eviction(self) -> None:
        t = Tracker(run_id="r", usage_buffer_size=2)
        for _ in range(10):
            t.record("a", "t", "sonnet", 100, 50, cost_usd=0.10)
        # spend total survives eviction of older rows.
        assert t.spent_usd == pytest.approx(1.0)
        bd = t.model_breakdowns()[0]
        assert bd.invocation_count == 10
        assert bd.input_tokens == 1000

    def test_eviction_rotates_to_jsonl(self, tmp_path: Path) -> None:
        rotation = tmp_path / "rot"
        t = Tracker(run_id="r", usage_buffer_size=2, rotation_dir=rotation)
        for _ in range(5):
            t.record("a", "t", "sonnet", 1, 1, cost_usd=0.01)
        rotation_file = rotation / "usages-r.jsonl"
        assert rotation_file.exists()
        # 5 recorded, buffer=2 -> 3 rows rotated out.
        lines = rotation_file.read_text().strip().splitlines()
        assert len(lines) == 3
        first = json.loads(lines[0])
        assert first["model"] == "sonnet"


# ---------------------------------------------------------------------------
# TokenUsage (de)serialisation
# ---------------------------------------------------------------------------


class TestTokenUsage:
    def test_to_dict_from_dict_round_trip(self) -> None:
        usage = TokenUsage(
            input_tokens=10,
            output_tokens=5,
            model="sonnet",
            cost_usd=0.5,
            agent_id="a",
            task_id="t",
            cache_read_tokens=3,
            cache_write_tokens=2,
            cost_tags={"role": "qa"},
            quota_envelope="sub",
        )
        rebuilt = TokenUsage.from_dict(usage.to_dict())
        assert rebuilt.input_tokens == 10
        assert rebuilt.model == "sonnet"
        assert rebuilt.cost_usd == pytest.approx(0.5)
        assert rebuilt.cache_read_tokens == 3
        assert rebuilt.cost_tags == {"role": "qa"}
        assert rebuilt.quota_envelope == "sub"

    def test_from_dict_defaults_missing_envelope(self) -> None:
        usage = TokenUsage.from_dict(
            {
                "input_tokens": 1,
                "output_tokens": 1,
                "model": "sonnet",
                "cost_usd": 0.1,
                "agent_id": "a",
                "task_id": "t",
            }
        )
        assert usage.quota_envelope == DEFAULT_QUOTA_ENVELOPE

    def test_from_dict_non_dict_tags_become_empty(self) -> None:
        usage = TokenUsage.from_dict(
            {
                "input_tokens": 1,
                "output_tokens": 1,
                "model": "sonnet",
                "cost_usd": 0.1,
                "agent_id": "a",
                "task_id": "t",
                "cost_tags": "not-a-dict",
            }
        )
        assert usage.cost_tags == {}


# ---------------------------------------------------------------------------
# cheaper_model_for + shareable_summary + buffer env + retry budget
# ---------------------------------------------------------------------------


class TestCheaperModelFor:
    def test_no_cap_returns_none(self) -> None:
        t = Tracker(run_id="r", budget_usd=0.0)
        assert t.cheaper_model_for("opus") is None

    def test_below_warn_threshold_returns_none(self) -> None:
        t = Tracker(run_id="r", budget_usd=10.0, warn_threshold=0.8)
        t.record("a", "t", "opus", 0, 0, cost_usd=1.0)  # 10% spent
        assert t.cheaper_model_for("opus") is None

    def test_opus_reroutes_to_sonnet_when_warning(self) -> None:
        t = Tracker(run_id="r", budget_usd=10.0, warn_threshold=0.8)
        t.record("a", "t", "opus", 0, 0, cost_usd=8.5)  # 85% spent
        assert t.cheaper_model_for("opus") == "sonnet"

    def test_sonnet_reroutes_to_haiku_when_warning(self) -> None:
        t = Tracker(run_id="r", budget_usd=10.0, warn_threshold=0.8)
        t.record("a", "t", "sonnet", 0, 0, cost_usd=9.0)
        assert t.cheaper_model_for("claude-sonnet-4") == "haiku"

    def test_unknown_model_falls_back_to_default_cheap(self) -> None:
        t = Tracker(run_id="r", budget_usd=10.0, warn_threshold=0.8)
        t.record("a", "t", "opus", 0, 0, cost_usd=9.0)
        assert t.cheaper_model_for("some-random-model") == "haiku"

    def test_already_cheapest_returns_none(self) -> None:
        t = Tracker(run_id="r", budget_usd=10.0, warn_threshold=0.8)
        t.record("a", "t", "opus", 0, 0, cost_usd=9.0)
        # "haiku" is already the default cheap model -> nothing cheaper.
        assert t.cheaper_model_for("haiku") is None


class TestShareableSummary:
    def test_summary_reports_tasks_and_cost(self) -> None:
        t = Tracker(run_id="r")
        t.record("a", "t", "sonnet", 1000, 500, cost_usd=0.5)
        out = t.shareable_summary(tasks_done=3, tasks_failed=1, total_duration_s=125.0)
        assert "3 completed" in out
        assert "1 failed" in out
        assert "2m 5s" in out

    def test_summary_includes_savings_when_cheaper_than_opus(self) -> None:
        t = Tracker(run_id="r")
        # haiku is far cheaper than opus -> baseline savings accrue.
        t.record("a", "t", "haiku", 1_000_000, 1_000_000, cost_usd=6.0)
        out = t.shareable_summary(tasks_done=1)
        assert "Saved:" in out
        assert "single agent" in out

    def test_summary_no_savings_section_when_only_opus(self) -> None:
        t = Tracker(run_id="r")
        t.record("a", "t", "opus", 1000, 1000, cost_usd=5.0)
        out = t.shareable_summary(tasks_done=1)
        # all-opus run accrues no "vs opus" savings.
        assert "Saved:" not in out

    def test_summary_seconds_only_when_under_a_minute(self) -> None:
        t = Tracker(run_id="r")
        t.record("a", "t", "sonnet", 1, 1, cost_usd=0.01)
        out = t.shareable_summary(tasks_done=1, total_duration_s=45.0)
        assert "45s" in out
        assert "m " not in out.split("Time:")[1].split("\n")[0]


class TestUsageBufferEnv:
    def test_env_override_buffer_size(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BERNSTEIN_COST_USAGE_BUFFER", "4")
        t = Tracker(run_id="r")  # usage_buffer_size=None -> resolve from env
        for _ in range(10):
            t.record("a", "t", "sonnet", 1, 1, cost_usd=0.01)
        assert len(t.usages) == 4
        assert t.total_usages_recorded == 10

    def test_invalid_env_uses_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BERNSTEIN_COST_USAGE_BUFFER", "not-an-int")
        t = Tracker(run_id="r")
        # Falls back to the large default; small records stay in memory.
        for _ in range(3):
            t.record("a", "t", "sonnet", 1, 1, cost_usd=0.01)
        assert len(t.usages) == 3

    def test_nonpositive_env_uses_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BERNSTEIN_COST_USAGE_BUFFER", "0")
        t = Tracker(run_id="r")
        assert t.usage_buffer_size == 10_000


class TestRetryBudgetAttachment:
    def test_attach_and_read_back(self) -> None:
        t = Tracker(run_id="r")
        sentinel = object()
        assert t.retry_budget is None
        t.attach_retry_budget(sentinel)
        assert t.retry_budget is sentinel


# --- run_id as a filename component -----------------------------------------
#
# The run id names three files on disk and arrives from `BERNSTEIN_RUN_ID` or
# from `GET /costs/{run_id}`. Both are input, so it is checked on the way in
# and on the way out - `load` builds a path from it before any instance exists
# for `__post_init__` to have validated.


@pytest.mark.parametrize("bad", ["a/b", "a\\b", "..", ".", "", "x\x00y"])
def test_construction_refuses_a_run_id_that_is_not_one_component(bad: str) -> None:
    with pytest.raises(ValueError, match="single path component"):
        Tracker(run_id=bad)


@pytest.mark.parametrize("bad", ["../outside", "..\\outside", "sub/dir"])
def test_load_refuses_a_traversing_run_id_before_it_builds_the_path(tmp_path: Path, bad: str) -> None:
    """The file it would have reached exists, so a pass here would be a read."""
    outside = tmp_path / "runtime" / "outside.json"
    outside.parent.mkdir(parents=True)
    outside.write_text('{"run_id": "outside", "budget_usd": 1.0}', encoding="utf-8")

    assert Tracker.load(tmp_path, bad) is None


def test_load_still_finds_an_ordinary_run(tmp_path: Path) -> None:
    tracker = Tracker(run_id="20260812-120000", budget_usd=5.0)
    tracker.save(tmp_path)

    restored = Tracker.load(tmp_path, "20260812-120000")

    assert restored is not None
    assert restored.run_id == "20260812-120000"


@pytest.mark.parametrize("payload", ['{"run_id": "other"}', '{"run_id": ""}', '{"run_id": 7}', "{}"])
def test_load_refuses_a_file_that_names_a_different_run(tmp_path: Path, payload: str) -> None:
    """The filename is validated; the identity inside the file has to agree.

    `GET /costs/requested` reads `requested.json`. Were that file free to call
    itself something else, the response would carry the other run's identity
    and its spend under the id the caller asked about.
    """
    costs_dir = tmp_path / "runtime" / "costs"
    costs_dir.mkdir(parents=True)
    (costs_dir / "requested.json").write_text(payload, encoding="utf-8")

    assert Tracker.load(tmp_path, "requested") is None
