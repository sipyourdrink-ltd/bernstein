"""Tests for per-quota-envelope cost attribution + budget hooks (issue #1405).

Coverage targets:
* Envelope routing on ``TokenUsage`` / ``CostTracker.record()``
* Hard-cap rejection raising :class:`EnvelopeBudgetError`
* Model allowlist enforcement
* Multi-envelope aggregation via ``cost_rollup_by_envelope.rollup``
* Threshold-hook firing via :func:`envelope_threshold_reached`
* :class:`SpendLedger` envelope tagging + aggregation
* Property-based invariants for rollup math
* Integration-style flows with a mock adapter recording across envelopes
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from bernstein.core.cost.budget_actions import (
    EnvelopeThresholdEvent,
    envelope_threshold_reached,
)
from bernstein.core.cost.cost_rollup_by_envelope import (
    aggregate_totals,
    rollup,
)
from bernstein.core.cost.cost_tracker import (
    DEFAULT_ENVELOPE_THRESHOLD,
    DEFAULT_QUOTA_ENVELOPE,
    CostTracker,
    EnvelopeBudgetError,
    EnvelopeConfig,
    EnvelopeReport,
    TokenUsage,
)
from bernstein.core.cost.spend_ledger import (
    CallTags,
    LedgerEntry,
    SpendLedger,
    aggregate_entries,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_usage(
    *,
    cost: float = 0.01,
    envelope: str = DEFAULT_QUOTA_ENVELOPE,
    model: str = "sonnet",
    ts: float = 1000.0,
) -> TokenUsage:
    return TokenUsage(
        input_tokens=10,
        output_tokens=20,
        model=model,
        cost_usd=cost,
        agent_id="agent-1",
        task_id="task-1",
        timestamp=ts,
        quota_envelope=envelope,
    )


# ---------------------------------------------------------------------------
# TokenUsage envelope field
# ---------------------------------------------------------------------------


class TestTokenUsageEnvelope:
    def test_default_envelope_is_subscription(self) -> None:
        usage = TokenUsage(
            input_tokens=1,
            output_tokens=1,
            model="m",
            cost_usd=0.0,
            agent_id="a",
            task_id="t",
        )
        assert usage.quota_envelope == "subscription"

    def test_explicit_envelope_assignment(self) -> None:
        usage = _make_usage(envelope="agent-sdk-credits")
        assert usage.quota_envelope == "agent-sdk-credits"

    def test_envelope_roundtrip_serialisation(self) -> None:
        usage = _make_usage(envelope="on-demand-tokens")
        restored = TokenUsage.from_dict(usage.to_dict())
        assert restored.quota_envelope == "on-demand-tokens"

    def test_legacy_dict_without_envelope_defaults(self) -> None:
        legacy_dict = {
            "input_tokens": 1,
            "output_tokens": 2,
            "model": "sonnet",
            "cost_usd": 0.01,
            "agent_id": "a",
            "task_id": "t",
        }
        usage = TokenUsage.from_dict(legacy_dict)
        assert usage.quota_envelope == "subscription"

    def test_empty_envelope_string_falls_back_to_default(self) -> None:
        legacy_dict = {
            "input_tokens": 1,
            "output_tokens": 1,
            "model": "m",
            "cost_usd": 0.0,
            "agent_id": "a",
            "task_id": "t",
            "quota_envelope": "",
        }
        usage = TokenUsage.from_dict(legacy_dict)
        assert usage.quota_envelope == "subscription"


# ---------------------------------------------------------------------------
# EnvelopeConfig
# ---------------------------------------------------------------------------


class TestEnvelopeConfig:
    def test_defaults(self) -> None:
        cfg = EnvelopeConfig(name="sub")
        assert cfg.budget_usd == 0.0
        assert cfg.hard_budget_usd == 0.0
        assert cfg.model_allowlist == ()
        assert cfg.threshold_pct == DEFAULT_ENVELOPE_THRESHOLD

    def test_negative_caps_normalised_to_zero(self) -> None:
        cfg = EnvelopeConfig(name="sub", budget_usd=-1.0, hard_budget_usd=-5.0)
        assert cfg.budget_usd == 0.0
        assert cfg.hard_budget_usd == 0.0

    def test_invalid_threshold_falls_back_to_default(self) -> None:
        cfg = EnvelopeConfig(name="sub", threshold_pct=0.0)
        assert cfg.threshold_pct == DEFAULT_ENVELOPE_THRESHOLD
        cfg2 = EnvelopeConfig(name="sub", threshold_pct=1.5)
        assert cfg2.threshold_pct == DEFAULT_ENVELOPE_THRESHOLD

    def test_model_allowed_empty_allowlist(self) -> None:
        cfg = EnvelopeConfig(name="sub")
        assert cfg.model_allowed("claude-sonnet-4")
        assert cfg.model_allowed("any-other-model")

    def test_model_allowed_substring_case_insensitive(self) -> None:
        cfg = EnvelopeConfig(name="sub", model_allowlist=("Sonnet", "Haiku"))
        assert cfg.model_allowed("claude-SONNET-4")
        assert cfg.model_allowed("Haiku-3")
        assert not cfg.model_allowed("opus-4")

    def test_to_dict_roundtrip(self) -> None:
        cfg = EnvelopeConfig(
            name="sdk",
            budget_usd=10.0,
            hard_budget_usd=15.0,
            model_allowlist=("sonnet",),
            threshold_pct=0.75,
        )
        d = cfg.to_dict()
        restored = EnvelopeConfig.from_dict("sdk", d)
        assert restored == cfg


# ---------------------------------------------------------------------------
# CostTracker envelope routing & enforcement
# ---------------------------------------------------------------------------


class TestCostTrackerEnvelope:
    def test_default_envelope_routes_to_subscription_bucket(self) -> None:
        tr = CostTracker(run_id="r-1")
        tr.record("a", "t", "sonnet", 100, 50, cost_usd=0.10)
        assert tr.spent_by_envelope() == {"subscription": pytest.approx(0.10)}
        assert tr.calls_by_envelope() == {"subscription": 1}

    def test_explicit_envelope_routing(self) -> None:
        tr = CostTracker(run_id="r-1")
        tr.record("a", "t", "sonnet", 100, 50, cost_usd=0.10, quota_envelope="sdk")
        tr.record("a", "t", "sonnet", 100, 50, cost_usd=0.05, quota_envelope="on-demand")
        assert tr.spent_by_envelope()["sdk"] == pytest.approx(0.10)
        assert tr.spent_by_envelope()["on-demand"] == pytest.approx(0.05)

    def test_multi_envelope_aggregation(self) -> None:
        tr = CostTracker(run_id="r-1")
        for envelope, cost in (("sub", 0.10), ("sub", 0.05), ("sdk", 0.20)):
            tr.record("a", "t", "sonnet", 1, 1, cost_usd=cost, quota_envelope=envelope)
        spent = tr.spent_by_envelope()
        assert spent["sub"] == pytest.approx(0.15)
        assert spent["sdk"] == pytest.approx(0.20)
        assert tr.calls_by_envelope() == {"sub": 2, "sdk": 1}

    def test_record_cumulative_propagates_envelope(self) -> None:
        tr = CostTracker(run_id="r-1")
        tr.record_cumulative(
            "a",
            "t",
            "sonnet",
            total_input_tokens=100,
            total_output_tokens=50,
            total_cost_usd=0.10,
            quota_envelope="sdk",
        )
        assert tr.spent_by_envelope() == {"sdk": pytest.approx(0.10)}

    def test_hard_cap_rejection_raises(self) -> None:
        tr = CostTracker(run_id="r-1")
        tr.configure_envelopes({"sdk": EnvelopeConfig(name="sdk", hard_budget_usd=0.10)})
        tr.record("a", "t", "sonnet", 1, 1, cost_usd=0.08, quota_envelope="sdk")
        with pytest.raises(EnvelopeBudgetError) as excinfo:
            tr.record("a", "t", "sonnet", 1, 1, cost_usd=0.05, quota_envelope="sdk")
        assert excinfo.value.envelope == "sdk"
        # State must NOT advance on rejection.
        assert tr.spent_by_envelope()["sdk"] == pytest.approx(0.08)

    def test_hard_cap_zero_disables_enforcement(self) -> None:
        tr = CostTracker(run_id="r-1")
        tr.configure_envelopes({"sdk": EnvelopeConfig(name="sdk", hard_budget_usd=0.0)})
        tr.record("a", "t", "sonnet", 1, 1, cost_usd=1000.0, quota_envelope="sdk")
        assert tr.spent_by_envelope()["sdk"] == pytest.approx(1000.0)

    def test_model_allowlist_blocks_disallowed_model(self) -> None:
        tr = CostTracker(run_id="r-1")
        tr.configure_envelopes({"sdk": EnvelopeConfig(name="sdk", model_allowlist=("sonnet",))})
        with pytest.raises(EnvelopeBudgetError):
            tr.record("a", "t", "opus", 1, 1, cost_usd=0.05, quota_envelope="sdk")

    def test_model_allowlist_admits_matching_model(self) -> None:
        tr = CostTracker(run_id="r-1")
        tr.configure_envelopes({"sdk": EnvelopeConfig(name="sdk", model_allowlist=("sonnet",))})
        tr.record("a", "t", "claude-sonnet-4", 1, 1, cost_usd=0.05, quota_envelope="sdk")
        assert tr.spent_by_envelope()["sdk"] == pytest.approx(0.05)

    def test_can_spawn_respects_envelope_hard_cap(self) -> None:
        tr = CostTracker(run_id="r-1")
        tr.configure_envelopes({"sdk": EnvelopeConfig(name="sdk", hard_budget_usd=0.05)})
        assert tr.can_spawn(quota_envelope="sdk")
        tr.record("a", "t", "sonnet", 1, 1, cost_usd=0.05, quota_envelope="sdk")
        assert not tr.can_spawn(quota_envelope="sdk")
        # Other envelopes remain admissible.
        assert tr.can_spawn(quota_envelope="other")

    def test_unknown_envelope_still_tracked(self) -> None:
        tr = CostTracker(run_id="r-1")
        tr.record("a", "t", "sonnet", 1, 1, cost_usd=0.05, quota_envelope="custom")
        assert "custom" in tr.spent_by_envelope()

    def test_envelope_threshold_hook_fires_once(self) -> None:
        events: list[EnvelopeReport] = []
        tr = CostTracker(run_id="r-1")
        tr.configure_envelopes({"sub": EnvelopeConfig(name="sub", budget_usd=1.0, threshold_pct=0.5)})
        tr.set_envelope_threshold_hook(events.append)
        # Stay below threshold first.
        tr.record("a", "t", "sonnet", 1, 1, cost_usd=0.40, quota_envelope="sub")
        assert events == []
        # Cross threshold.
        tr.record("a", "t", "sonnet", 1, 1, cost_usd=0.20, quota_envelope="sub")
        assert len(events) == 1
        assert events[0].name == "sub"
        # Subsequent records do NOT re-fire.
        tr.record("a", "t", "sonnet", 1, 1, cost_usd=0.10, quota_envelope="sub")
        assert len(events) == 1

    def test_envelope_threshold_hook_failure_swallowed(self) -> None:
        def bad_hook(_report: EnvelopeReport) -> None:
            raise RuntimeError("boom")

        tr = CostTracker(run_id="r-1")
        tr.configure_envelopes({"sub": EnvelopeConfig(name="sub", budget_usd=1.0, threshold_pct=0.5)})
        tr.set_envelope_threshold_hook(bad_hook)
        # Must not propagate the hook failure to the caller.
        tr.record("a", "t", "sonnet", 1, 1, cost_usd=0.80, quota_envelope="sub")
        assert tr.spent_by_envelope()["sub"] == pytest.approx(0.80)

    def test_envelope_threshold_not_fired_without_cap(self) -> None:
        events: list[EnvelopeReport] = []
        tr = CostTracker(run_id="r-1")
        # No cap configured -> hook can't determine threshold and stays silent.
        tr.set_envelope_threshold_hook(events.append)
        tr.record("a", "t", "sonnet", 1, 1, cost_usd=100.0)
        assert events == []

    def test_envelope_report_remaining(self) -> None:
        tr = CostTracker(run_id="r-1")
        tr.configure_envelopes({"sub": EnvelopeConfig(name="sub", budget_usd=1.0, hard_budget_usd=2.0)})
        tr.record("a", "t", "sonnet", 1, 1, cost_usd=0.40, quota_envelope="sub")
        rep = tr.envelope_report("sub")
        assert rep.spent_usd == pytest.approx(0.40)
        assert rep.cap_usd == pytest.approx(1.0)
        assert rep.remaining_usd == pytest.approx(0.60)
        assert rep.hard_remaining_usd == pytest.approx(1.60)
        assert rep.pct_used == pytest.approx(0.40)

    def test_envelope_reports_covers_configured_and_seen(self) -> None:
        tr = CostTracker(run_id="r-1")
        tr.configure_envelopes({"sub": EnvelopeConfig(name="sub", budget_usd=1.0)})
        tr.record("a", "t", "sonnet", 1, 1, cost_usd=0.10, quota_envelope="custom")
        reports = tr.envelope_reports()
        # Both configured-without-spend and spent-without-config must appear.
        assert "sub" in reports
        assert "custom" in reports
        assert reports["sub"].spent_usd == pytest.approx(0.0)
        assert reports["custom"].spent_usd == pytest.approx(0.10)

    def test_save_and_load_preserves_envelopes(self, tmp_path: Path) -> None:
        tr = CostTracker(run_id="r-1")
        tr.configure_envelopes(
            {
                "sub": EnvelopeConfig(name="sub", budget_usd=1.0),
                "sdk": EnvelopeConfig(name="sdk", hard_budget_usd=0.50),
            }
        )
        tr.record("a", "t", "sonnet", 1, 1, cost_usd=0.10, quota_envelope="sub")
        tr.record("a", "t", "sonnet", 1, 1, cost_usd=0.05, quota_envelope="sdk")
        path = tr.save(tmp_path)

        loaded = CostTracker.load(tmp_path, "r-1")
        assert loaded is not None
        assert path.exists()
        assert loaded.spent_by_envelope() == {
            "sub": pytest.approx(0.10),
            "sdk": pytest.approx(0.05),
        }
        assert set(loaded.envelopes) == {"sub", "sdk"}

    def test_legacy_snapshot_derives_envelope_totals_from_usages(self, tmp_path: Path) -> None:
        # Simulate an older snapshot without envelope blocks.
        costs_dir = tmp_path / "runtime" / "costs"
        costs_dir.mkdir(parents=True)
        snapshot = {
            "run_id": "r-x",
            "budget_usd": 0.0,
            "hard_budget_usd": 0.0,
            "spent_usd": 0.10,
            "warn_threshold": 0.8,
            "critical_threshold": 0.95,
            "hard_stop_threshold": 1.0,
            "usages": [
                {
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "model": "sonnet",
                    "cost_usd": 0.10,
                    "agent_id": "a",
                    "task_id": "t",
                    "tenant_id": "default",
                    "timestamp": 1000.0,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                    "quota_envelope": "sdk",
                }
            ],
            "cumulative_tokens": {},
        }
        (costs_dir / "r-x.json").write_text(json.dumps(snapshot))
        loaded = CostTracker.load(tmp_path, "r-x")
        assert loaded is not None
        assert loaded.spent_by_envelope()["sdk"] == pytest.approx(0.10)


# ---------------------------------------------------------------------------
# envelope_threshold_reached hook
# ---------------------------------------------------------------------------


class TestEnvelopeThresholdReached:
    def test_returns_none_when_under_threshold(self) -> None:
        assert envelope_threshold_reached(envelope="sub", spent_usd=0.30, cap_usd=1.0, threshold_pct=0.80) is None

    def test_returns_event_at_threshold(self) -> None:
        evt = envelope_threshold_reached(envelope="sub", spent_usd=0.80, cap_usd=1.0, threshold_pct=0.80)
        assert isinstance(evt, EnvelopeThresholdEvent)
        assert evt.envelope == "sub"
        assert evt.pct_used == pytest.approx(0.80)
        assert evt.hard_breached is False

    def test_hard_breach_fires_with_zero_soft_cap(self) -> None:
        evt = envelope_threshold_reached(envelope="sub", spent_usd=2.0, cap_usd=0.0, hard_cap_usd=1.0)
        assert evt is not None
        assert evt.hard_breached is True

    def test_disabled_when_no_caps(self) -> None:
        evt = envelope_threshold_reached(envelope="sub", spent_usd=2.0, cap_usd=0.0, hard_cap_usd=0.0)
        assert evt is None

    def test_event_dict_is_json_safe(self) -> None:
        evt = envelope_threshold_reached(envelope="sub", spent_usd=0.80, cap_usd=1.0)
        assert evt is not None
        # Must round-trip through json without errors.
        json.dumps(evt.to_dict())


# ---------------------------------------------------------------------------
# Rollup
# ---------------------------------------------------------------------------


class TestRollup:
    def test_empty_records_returns_empty_when_no_config(self) -> None:
        out = rollup([])
        assert out == {}

    def test_configured_envelope_appears_even_with_zero_spend(self) -> None:
        out = rollup([], {"sub": EnvelopeConfig(name="sub", budget_usd=1.0)})
        assert "sub" in out
        assert out["sub"].total_spend == pytest.approx(0.0)

    def test_rollup_sums_spend_per_envelope(self) -> None:
        records = [
            _make_usage(cost=0.10, envelope="sub"),
            _make_usage(cost=0.05, envelope="sub"),
            _make_usage(cost=0.20, envelope="sdk"),
        ]
        out = rollup(records)
        assert out["sub"].total_spend == pytest.approx(0.15)
        assert out["sdk"].total_spend == pytest.approx(0.20)

    def test_rollup_pct_used_calculation(self) -> None:
        records = [_make_usage(cost=0.60, envelope="sub")]
        out = rollup(records, {"sub": EnvelopeConfig(name="sub", budget_usd=1.0)})
        assert out["sub"].pct_used == pytest.approx(0.60)

    def test_rollup_threshold_reached_flag(self) -> None:
        records = [_make_usage(cost=0.85, envelope="sub")]
        out = rollup(
            records,
            {"sub": EnvelopeConfig(name="sub", budget_usd=1.0, threshold_pct=0.8)},
        )
        assert out["sub"].threshold_reached is True

    def test_rollup_hard_breached_flag(self) -> None:
        records = [_make_usage(cost=1.5, envelope="sub")]
        out = rollup(
            records,
            {"sub": EnvelopeConfig(name="sub", hard_budget_usd=1.0)},
        )
        assert out["sub"].hard_breached is True

    def test_rollup_forecast_with_burn_rate(self) -> None:
        # Two records 10 seconds apart spending $0.20 total -> burn=0.02/s.
        records = [
            _make_usage(cost=0.10, envelope="sub", ts=1000.0),
            _make_usage(cost=0.10, envelope="sub", ts=1010.0),
        ]
        out = rollup(records, {"sub": EnvelopeConfig(name="sub", budget_usd=1.0)})
        row = out["sub"]
        assert row.burn_rate_usd_per_sec == pytest.approx(0.02)
        # remaining $0.80 / 0.02/s = 40s
        assert row.forecast_to_cap_seconds == pytest.approx(40.0)

    def test_rollup_forecast_none_when_single_record(self) -> None:
        records = [_make_usage(cost=0.10, envelope="sub", ts=1000.0)]
        out = rollup(
            records,
            {"sub": EnvelopeConfig(name="sub", budget_usd=1.0)},
            now=1000.0,
        )
        # Window is zero so no forecast is produced.
        assert out["sub"].forecast_to_cap_seconds is None

    def test_rollup_models_collected(self) -> None:
        records = [
            _make_usage(envelope="sub", model="sonnet"),
            _make_usage(envelope="sub", model="opus"),
        ]
        out = rollup(records)
        assert out["sub"].models == ("opus", "sonnet")

    def test_rollup_to_envelope_report(self) -> None:
        records = [_make_usage(cost=0.10, envelope="sub")]
        out = rollup(records, {"sub": EnvelopeConfig(name="sub", budget_usd=1.0)})
        report = out["sub"].to_envelope_report()
        assert isinstance(report, EnvelopeReport)
        assert report.remaining_usd == pytest.approx(0.90)

    def test_rollup_row_to_dict_json_safe(self) -> None:
        records = [_make_usage(cost=0.10, envelope="sub")]
        out = rollup(records)
        json.dumps(out["sub"].to_dict())

    def test_aggregate_totals(self) -> None:
        records = [
            _make_usage(cost=0.10, envelope="sub"),
            _make_usage(cost=0.20, envelope="sdk"),
        ]
        out = rollup(
            records,
            {
                "sub": EnvelopeConfig(name="sub", budget_usd=1.0, hard_budget_usd=2.0),
                "sdk": EnvelopeConfig(name="sdk", budget_usd=0.50),
            },
        )
        totals = aggregate_totals(out)
        assert totals["total_spend"] == pytest.approx(0.30)
        assert totals["total_cap"] == pytest.approx(1.50)
        assert totals["total_hard_cap"] == pytest.approx(2.0)
        assert totals["total_calls"] == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# SpendLedger envelope tagging
# ---------------------------------------------------------------------------


class TestSpendLedgerEnvelope:
    def test_ledger_entry_serialises_envelope(self) -> None:
        entry = LedgerEntry(
            ts=1000.0,
            ts_iso="2026-05-17T00:00:00+00:00",
            run_id="r-1",
            task_id="t",
            agent_id="a",
            role="",
            feature_label="",
            model="sonnet",
            input_tokens=1,
            output_tokens=1,
            cache_read_tokens=0,
            cache_write_tokens=0,
            cost_usd=0.10,
            quota_envelope="sdk",
        )
        roundtrip = LedgerEntry.from_dict(json.loads(entry.to_json()))
        assert roundtrip.quota_envelope == "sdk"

    def test_legacy_ledger_entry_defaults_envelope(self) -> None:
        entry = LedgerEntry.from_dict(
            {
                "ts": 1000.0,
                "task_id": "t",
                "model": "sonnet",
                "cost_usd": 0.10,
            }
        )
        assert entry.quota_envelope == "subscription"

    def test_record_writes_envelope_dimension(self, tmp_path: Path) -> None:
        path = tmp_path / "ledger.jsonl"
        led = SpendLedger(path=path, run_id="r-1")
        led.record(
            tags=CallTags(task_id="t", agent_id="a", quota_envelope="sdk"),
            model="sonnet",
            cost_usd=0.10,
        )
        led.record(
            tags=CallTags(task_id="t", agent_id="a", quota_envelope="subscription"),
            model="sonnet",
            cost_usd=0.05,
        )
        env_totals = led.totals_by("envelope")
        assert env_totals == {"sdk": pytest.approx(0.10), "subscription": pytest.approx(0.05)}

    def test_aggregate_entries_envelope_dimension(self) -> None:
        entries = [
            LedgerEntry(
                ts=1000.0,
                ts_iso="",
                run_id="r",
                task_id="t",
                agent_id="a",
                role="",
                feature_label="",
                model="sonnet",
                input_tokens=10,
                output_tokens=5,
                cache_read_tokens=0,
                cache_write_tokens=0,
                cost_usd=0.10,
                quota_envelope=env,
            )
            for env in ("sub", "sub", "sdk")
        ]
        out = aggregate_entries(entries, "envelope")
        assert out["sub"]["cost_usd"] == pytest.approx(0.20)
        assert out["sub"]["calls"] == 2
        assert out["sdk"]["cost_usd"] == pytest.approx(0.10)

    def test_merged_tag_dict_hides_default_envelope(self) -> None:
        tags = CallTags(task_id="t", quota_envelope="subscription")
        merged = tags.merged()
        # Backwards-compat: default envelope must NOT pollute the tag map.
        assert "quota_envelope" not in merged

    def test_merged_tag_dict_includes_explicit_envelope(self) -> None:
        tags = CallTags(task_id="t", quota_envelope="sdk")
        merged = tags.merged()
        assert merged["quota_envelope"] == "sdk"


# ---------------------------------------------------------------------------
# Property-based invariants (>=10 cases)
# ---------------------------------------------------------------------------


cost_strategy = st.floats(min_value=0.0, max_value=1000.0, allow_nan=False, allow_infinity=False)
envelope_strategy = st.sampled_from(["subscription", "agent-sdk-credits", "on-demand-tokens", "custom"])
model_strategy = st.sampled_from(["sonnet", "opus", "haiku", "gpt-5"])


@st.composite
def usage_strategy(draw: st.DrawFn) -> TokenUsage:
    cost = draw(cost_strategy)
    env = draw(envelope_strategy)
    model = draw(model_strategy)
    ts = draw(st.floats(min_value=1.0, max_value=1e9, allow_nan=False, allow_infinity=False))
    return TokenUsage(
        input_tokens=1,
        output_tokens=1,
        model=model,
        cost_usd=cost,
        agent_id="a",
        task_id="t",
        timestamp=ts,
        quota_envelope=env,
    )


class TestEnvelopeProperties:
    @settings(derandomize=True, max_examples=50, deadline=None)
    @given(records=st.lists(usage_strategy(), min_size=0, max_size=30))
    def test_rollup_total_equals_sum_of_costs(self, records: list[TokenUsage]) -> None:
        out = rollup(records)
        total = sum(row.total_spend for row in out.values())
        expected = sum(r.cost_usd for r in records)
        assert total == pytest.approx(expected, rel=1e-6)

    @settings(derandomize=True, max_examples=50, deadline=None)
    @given(records=st.lists(usage_strategy(), min_size=0, max_size=30))
    def test_rollup_calls_equals_record_count(self, records: list[TokenUsage]) -> None:
        out = rollup(records)
        total_calls = sum(row.calls for row in out.values())
        assert total_calls == len(records)

    @settings(derandomize=True, max_examples=50, deadline=None)
    @given(records=st.lists(usage_strategy(), min_size=1, max_size=20))
    def test_envelope_count_bounded_by_distinct_envelopes(self, records: list[TokenUsage]) -> None:
        distinct = {r.quota_envelope or "subscription" for r in records}
        out = rollup(records)
        assert set(out) == distinct

    @settings(derandomize=True, max_examples=50, deadline=None)
    @given(records=st.lists(usage_strategy(), min_size=0, max_size=20))
    def test_rollup_pct_used_is_nonnegative(self, records: list[TokenUsage]) -> None:
        out = rollup(
            records,
            {"subscription": EnvelopeConfig(name="subscription", budget_usd=100.0)},
        )
        for row in out.values():
            assert row.pct_used >= 0.0

    @settings(derandomize=True, max_examples=50, deadline=None)
    @given(records=st.lists(usage_strategy(), min_size=0, max_size=20))
    def test_rollup_burn_rate_nonnegative(self, records: list[TokenUsage]) -> None:
        out = rollup(records)
        for row in out.values():
            assert row.burn_rate_usd_per_sec >= 0.0

    @settings(derandomize=True, max_examples=50, deadline=None)
    @given(records=st.lists(usage_strategy(), min_size=0, max_size=20))
    def test_rollup_remaining_via_report_nonnegative(self, records: list[TokenUsage]) -> None:
        out = rollup(
            records,
            {"subscription": EnvelopeConfig(name="subscription", budget_usd=100.0)},
        )
        for row in out.values():
            rep = row.to_envelope_report()
            assert rep.remaining_usd >= 0.0

    @settings(derandomize=True, max_examples=50, deadline=None)
    @given(
        cost=st.floats(min_value=0.0, max_value=1000.0, allow_nan=False, allow_infinity=False),
        cap=st.floats(min_value=0.01, max_value=1000.0, allow_nan=False, allow_infinity=False),
        threshold=st.floats(min_value=0.01, max_value=1.0, allow_nan=False, allow_infinity=False),
    )
    def test_threshold_event_consistency(self, cost: float, cap: float, threshold: float) -> None:
        evt = envelope_threshold_reached(envelope="sub", spent_usd=cost, cap_usd=cap, threshold_pct=threshold)
        if cost / cap >= threshold:
            assert evt is not None
        else:
            assert evt is None

    @settings(derandomize=True, max_examples=40, deadline=None)
    @given(
        cost=st.floats(min_value=0.0, max_value=1000.0, allow_nan=False, allow_infinity=False),
        hard_cap=st.floats(min_value=0.01, max_value=1000.0, allow_nan=False, allow_infinity=False),
    )
    def test_hard_cap_rejects_records_that_breach(self, cost: float, hard_cap: float) -> None:
        tr = CostTracker(run_id="r-prop")
        tr.configure_envelopes({"sub": EnvelopeConfig(name="sub", hard_budget_usd=hard_cap)})
        if cost > hard_cap:
            with pytest.raises(EnvelopeBudgetError):
                tr.record(
                    "a",
                    "t",
                    "sonnet",
                    1,
                    1,
                    cost_usd=cost,
                    quota_envelope="sub",
                )
        else:
            tr.record(
                "a",
                "t",
                "sonnet",
                1,
                1,
                cost_usd=cost,
                quota_envelope="sub",
            )
            assert tr.spent_by_envelope()["sub"] == pytest.approx(cost)

    @settings(derandomize=True, max_examples=40, deadline=None)
    @given(
        envelopes=st.lists(envelope_strategy, min_size=0, max_size=8),
    )
    def test_calls_by_envelope_count_matches_records(self, envelopes: list[str]) -> None:
        tr = CostTracker(run_id="r-prop")
        for env in envelopes:
            tr.record("a", "t", "sonnet", 1, 1, cost_usd=0.01, quota_envelope=env)
        counts = tr.calls_by_envelope()
        for env in set(envelopes):
            assert counts[env] == envelopes.count(env)

    @settings(derandomize=True, max_examples=30, deadline=None)
    @given(allowlist=st.lists(st.sampled_from(["sonnet", "opus", "haiku"]), min_size=1, max_size=3, unique=True))
    def test_model_allowlist_blocks_outsiders(self, allowlist: list[str]) -> None:
        tr = CostTracker(run_id="r-prop")
        tr.configure_envelopes({"sub": EnvelopeConfig(name="sub", model_allowlist=tuple(allowlist))})
        outsiders = {"sonnet", "opus", "haiku", "gpt-5"} - set(allowlist)
        if not outsiders:
            return
        outside_model = next(iter(outsiders))
        # gpt-5 contains no substring of sonnet/opus/haiku; sonnet/opus/haiku
        # would each match themselves so we only test the outsider case
        # when at least one of {sonnet, opus, haiku} is excluded.
        if any(name in outside_model for name in allowlist):
            return
        with pytest.raises(EnvelopeBudgetError):
            tr.record(
                "a",
                "t",
                outside_model,
                1,
                1,
                cost_usd=0.01,
                quota_envelope="sub",
            )


# ---------------------------------------------------------------------------
# Integration tests via a mock adapter (>=5 flows)
# ---------------------------------------------------------------------------


class _MockAdapter:
    """Minimal adapter-like driver that records token usage via the tracker.

    Mirrors the way the real adapters call ``CostTracker.record()`` from
    a streaming-merge hook: it advances ``ts`` per call so the rollup can
    compute a meaningful burn rate.
    """

    def __init__(self, tracker: CostTracker, *, envelope: str) -> None:
        self.tracker = tracker
        self.envelope = envelope
        self._ts = 1000.0

    def send(self, *, cost_usd: float, model: str = "sonnet") -> None:
        self._ts += 1.0
        self.tracker.record(
            agent_id="agent-mock",
            task_id="task-mock",
            model=model,
            input_tokens=10,
            output_tokens=10,
            cost_usd=cost_usd,
            quota_envelope=self.envelope,
        )


class TestIntegration:
    def test_two_adapters_separate_envelopes(self) -> None:
        tr = CostTracker(run_id="r-int")
        a = _MockAdapter(tr, envelope="subscription")
        b = _MockAdapter(tr, envelope="agent-sdk-credits")
        for _ in range(3):
            a.send(cost_usd=0.05)
        for _ in range(2):
            b.send(cost_usd=0.10)
        spent = tr.spent_by_envelope()
        assert spent["subscription"] == pytest.approx(0.15)
        assert spent["agent-sdk-credits"] == pytest.approx(0.20)

    def test_hard_cap_blocks_further_adapter_calls(self) -> None:
        tr = CostTracker(run_id="r-int")
        tr.configure_envelopes({"agent-sdk-credits": EnvelopeConfig(name="agent-sdk-credits", hard_budget_usd=0.10)})
        adapter = _MockAdapter(tr, envelope="agent-sdk-credits")
        adapter.send(cost_usd=0.06)
        adapter.send(cost_usd=0.03)
        with pytest.raises(EnvelopeBudgetError):
            adapter.send(cost_usd=0.05)
        assert tr.spent_by_envelope()["agent-sdk-credits"] == pytest.approx(0.09)

    def test_threshold_hook_fires_for_adapter_workflow(self) -> None:
        events: list[EnvelopeReport] = []
        tr = CostTracker(run_id="r-int")
        tr.configure_envelopes({"subscription": EnvelopeConfig(name="subscription", budget_usd=1.0, threshold_pct=0.8)})
        tr.set_envelope_threshold_hook(events.append)
        adapter = _MockAdapter(tr, envelope="subscription")
        for _ in range(9):
            adapter.send(cost_usd=0.10)
        assert len(events) == 1
        assert events[0].name == "subscription"
        assert events[0].pct_used >= 0.80

    def test_ledger_integration_per_envelope_aggregation(self, tmp_path: Path) -> None:
        path = tmp_path / "ledger.jsonl"
        ledger = SpendLedger(path=path, run_id="r-int")
        tr = CostTracker(run_id="r-int", spend_ledger=ledger)
        a = _MockAdapter(tr, envelope="subscription")
        b = _MockAdapter(tr, envelope="agent-sdk-credits")
        for _ in range(2):
            a.send(cost_usd=0.05)
        for _ in range(3):
            b.send(cost_usd=0.10)
        # SpendLedger should reflect envelope dimension totals.
        totals = ledger.totals_by("envelope")
        assert totals["subscription"] == pytest.approx(0.10)
        assert totals["agent-sdk-credits"] == pytest.approx(0.30)
        # Reading the ledger back and running rollup must produce the same totals.
        entries = SpendLedger.load_entries(path)
        records = [
            TokenUsage(
                input_tokens=e.input_tokens,
                output_tokens=e.output_tokens,
                model=e.model,
                cost_usd=e.cost_usd,
                agent_id=e.agent_id,
                task_id=e.task_id,
                timestamp=e.ts,
                quota_envelope=e.quota_envelope,
            )
            for e in entries
        ]
        rollup_out = rollup(records)
        assert rollup_out["subscription"].total_spend == pytest.approx(0.10)
        assert rollup_out["agent-sdk-credits"].total_spend == pytest.approx(0.30)

    def test_save_load_then_continue_recording(self, tmp_path: Path) -> None:
        tr = CostTracker(run_id="r-int")
        tr.configure_envelopes({"subscription": EnvelopeConfig(name="subscription", budget_usd=2.0)})
        adapter = _MockAdapter(tr, envelope="subscription")
        adapter.send(cost_usd=0.10)
        tr.save(tmp_path)
        loaded = CostTracker.load(tmp_path, "r-int")
        assert loaded is not None
        adapter2 = _MockAdapter(loaded, envelope="subscription")
        adapter2.send(cost_usd=0.05)
        assert loaded.spent_by_envelope()["subscription"] == pytest.approx(0.15)
        # Configured envelope mapping survived reload.
        assert "subscription" in loaded.envelopes


# ---------------------------------------------------------------------------
# EnvelopeReport JSON safety
# ---------------------------------------------------------------------------


class TestEnvelopeReportSerialisation:
    def test_to_dict_renders_inf_as_none(self) -> None:
        tr = CostTracker(run_id="r-int")
        tr.record("a", "t", "sonnet", 1, 1, cost_usd=0.01)
        report = tr.envelope_report("subscription")
        d = report.to_dict()
        # Unset cap -> remaining is +inf -> serialised as None.
        assert d["remaining_usd"] is None
        assert d["hard_remaining_usd"] is None
        json.dumps(d)
