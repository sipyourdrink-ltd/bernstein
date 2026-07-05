"""Per-profile cost attribution from the spend ledger (issue #2245).

Covers:

* Per-entry attribution keyed on the ``response_profile`` cost tag.
* Transition exclusion: a task with a recorded ``profile_transition``
  event is attributed to no profile - excluded, never split.
* The honesty rule: cross-profile comparisons only exist when both
  profiles have at least ``MIN_COMPARABLE_TASKS`` tasks sharing the
  same role and model.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.cost.profile_attribution import (
    EXCLUDED_LABEL,
    MIN_COMPARABLE_TASKS,
    UNATTRIBUTED_LABEL,
    ProfileTransition,
    aggregate_ledger_by_profile,
    attribute_by_profile,
    compute_profile_comparisons,
    entry_profile,
    load_transitions,
    record_profile_transition,
    transitioned_task_ids,
)
from bernstein.core.cost.spend_ledger import CallTags, LedgerEntry, SpendLedger


def _entry(
    task_id: str,
    profile: str = "",
    *,
    role: str = "backend",
    model: str = "sonnet",
    cost_usd: float = 0.10,
    output_tokens: int = 100,
    ts: float = 1000.0,
) -> LedgerEntry:
    tags = {"response_profile": profile, "profile_content_sha256": "a" * 64} if profile else {}
    return LedgerEntry(
        ts=ts,
        ts_iso="2026-01-01T00:00:00+00:00",
        run_id="r-1",
        task_id=task_id,
        agent_id="agent-1",
        role=role,
        feature_label="",
        model=model,
        input_tokens=50,
        output_tokens=output_tokens,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost_usd=cost_usd,
        tags=tags,
    )


class TestEntryProfile:
    def test_reads_response_profile_tag(self) -> None:
        assert entry_profile(_entry("t-1", "terse")) == "terse"

    def test_missing_tag_is_empty(self) -> None:
        assert entry_profile(_entry("t-1")) == ""


class TestTransitionRecords:
    def test_record_and_load_roundtrip(self, tmp_path: Path) -> None:
        path = tmp_path / "profile_transitions.jsonl"
        rec = record_profile_transition(
            path,
            task_id="t-1",
            agent_id="a-1",
            from_profile="verbose",
            to_profile="terse",
            from_sha256="b" * 64,
            to_sha256="c" * 64,
            ts=1234.5,
        )
        assert isinstance(rec, ProfileTransition)
        loaded = load_transitions(path)
        assert len(loaded) == 1
        assert loaded[0].task_id == "t-1"
        assert loaded[0].from_profile == "verbose"
        assert loaded[0].to_profile == "terse"
        assert loaded[0].ts == pytest.approx(1234.5)

    def test_load_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert load_transitions(tmp_path / "absent.jsonl") == []

    def test_malformed_lines_are_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "profile_transitions.jsonl"
        record_profile_transition(path, task_id="t-1", agent_id="a", from_profile="verbose", to_profile="terse")
        with path.open("a", encoding="utf-8") as fh:
            fh.write("not json\n")
        assert len(load_transitions(path)) == 1

    def test_transitioned_task_ids(self, tmp_path: Path) -> None:
        path = tmp_path / "profile_transitions.jsonl"
        record_profile_transition(path, task_id="t-1", agent_id="a", from_profile="verbose", to_profile="terse")
        record_profile_transition(path, task_id="t-2", agent_id="a", from_profile="terse", to_profile="balanced")
        assert transitioned_task_ids(load_transitions(path)) == frozenset({"t-1", "t-2"})


class TestAttributeByProfile:
    def test_entries_grouped_by_profile_tag(self) -> None:
        entries = [
            _entry("t-1", "terse", cost_usd=0.10),
            _entry("t-2", "terse", cost_usd=0.20),
            _entry("t-3", "verbose", cost_usd=0.30),
        ]
        result = attribute_by_profile(entries, [])
        assert result.profiles["terse"].cost_usd == pytest.approx(0.30)
        assert result.profiles["terse"].tasks == 2
        assert result.profiles["verbose"].tasks == 1
        assert result.excluded.calls == 0

    def test_untagged_entries_are_unattributed(self) -> None:
        result = attribute_by_profile([_entry("t-1")], [])
        assert result.profiles[UNATTRIBUTED_LABEL].tasks == 1

    def test_transitioned_task_excluded_never_split(self) -> None:
        """Acceptance 3: a task that changed profile mid-flight is
        attributed to exactly zero profiles - all its entries land in the
        excluded bucket, none in either profile bucket."""
        entries = [
            # Task t-x started under verbose (first session entry), was
            # overridden to terse (second session entry).
            _entry("t-x", "verbose", cost_usd=0.40, output_tokens=400),
            _entry("t-x", "terse", cost_usd=0.10, output_tokens=50),
            _entry("t-ok", "terse", cost_usd=0.20),
        ]
        transitions = [
            ProfileTransition(
                ts=1000.0,
                ts_iso="",
                task_id="t-x",
                agent_id="a-1",
                from_profile="verbose",
                to_profile="terse",
                from_sha256="",
                to_sha256="",
            )
        ]
        result = attribute_by_profile(entries, transitions)
        assert result.excluded.calls == 2
        assert result.excluded.cost_usd == pytest.approx(0.50)
        assert result.excluded.task_ids == {"t-x"}
        assert "verbose" not in result.profiles
        assert result.profiles["terse"].task_ids == {"t-ok"}
        assert result.profiles["terse"].cost_usd == pytest.approx(0.20)

    def test_every_entry_lands_in_exactly_one_bucket(self) -> None:
        entries = [
            _entry("t-1", "terse", cost_usd=0.11),
            _entry("t-2", "verbose", cost_usd=0.22),
            _entry("t-3", cost_usd=0.33),
            _entry("t-4", "terse", cost_usd=0.44),
        ]
        transitions = [
            ProfileTransition(
                ts=0.0,
                ts_iso="",
                task_id="t-4",
                agent_id="a",
                from_profile="verbose",
                to_profile="terse",
                from_sha256="",
                to_sha256="",
            )
        ]
        result = attribute_by_profile(entries, transitions)
        bucket_total = sum(b.cost_usd for b in result.profiles.values()) + result.excluded.cost_usd
        assert bucket_total == pytest.approx(sum(e.cost_usd for e in entries))


class TestAggregateLedgerByProfile:
    def test_totals_match_entry_sum_to_the_cent(self, tmp_path: Path) -> None:
        """Acceptance 4: grouped totals equal the per-entry ledger sum."""
        ledger_path = tmp_path / "ledger.jsonl"
        led = SpendLedger(path=ledger_path, run_id="r-1")
        led.record(
            tags=CallTags(task_id="t-1", agent_id="a", role="backend", extra={"response_profile": "terse"}),
            model="sonnet",
            cost_usd=0.101,
        )
        led.record(
            tags=CallTags(task_id="t-2", agent_id="a", role="backend", extra={"response_profile": "verbose"}),
            model="sonnet",
            cost_usd=0.202,
        )
        led.record(tags=CallTags(task_id="t-3", agent_id="a", role="qa"), model="haiku", cost_usd=0.05)

        entries = SpendLedger.load_entries(ledger_path)
        grouped = aggregate_ledger_by_profile(entries, [])
        grouped_total = round(sum(v["cost_usd"] for v in grouped.values()), 2)
        entry_total = round(sum(e.cost_usd for e in entries), 2)
        assert grouped_total == entry_total
        assert grouped["terse"]["cost_usd"] == pytest.approx(0.101)
        assert grouped["verbose"]["cost_usd"] == pytest.approx(0.202)
        assert grouped[UNATTRIBUTED_LABEL]["cost_usd"] == pytest.approx(0.05)

    def test_excluded_bucket_labelled(self) -> None:
        entries = [_entry("t-x", "terse", cost_usd=0.10)]
        transitions = [
            ProfileTransition(
                ts=0.0,
                ts_iso="",
                task_id="t-x",
                agent_id="a",
                from_profile="verbose",
                to_profile="terse",
                from_sha256="",
                to_sha256="",
            )
        ]
        grouped = aggregate_ledger_by_profile(entries, transitions)
        assert grouped[EXCLUDED_LABEL]["cost_usd"] == pytest.approx(0.10)


class TestHonestyRule:
    def test_constant_default(self) -> None:
        assert MIN_COMPARABLE_TASKS == 5

    def _cohort(self, profile: str, n: int, *, role: str = "backend", model: str = "sonnet") -> list[LedgerEntry]:
        cost = 0.10 if profile == "terse" else 0.30
        toks = 100 if profile == "terse" else 300
        return [
            _entry(f"{profile}-{role}-{model}-{i}", profile, role=role, model=model, cost_usd=cost, output_tokens=toks)
            for i in range(n)
        ]

    def test_insufficient_comparable_runs_when_below_n(self) -> None:
        entries = self._cohort("terse", MIN_COMPARABLE_TASKS) + self._cohort("verbose", MIN_COMPARABLE_TASKS - 1)
        comparisons = compute_profile_comparisons(entries, [])
        assert comparisons == []

    def test_role_model_mismatch_is_not_comparable(self) -> None:
        entries = self._cohort("terse", MIN_COMPARABLE_TASKS) + self._cohort(
            "verbose", MIN_COMPARABLE_TASKS, model="haiku"
        )
        assert compute_profile_comparisons(entries, []) == []

    def test_comparison_emitted_when_both_reach_n(self) -> None:
        entries = self._cohort("terse", MIN_COMPARABLE_TASKS) + self._cohort("verbose", MIN_COMPARABLE_TASKS)
        comparisons = compute_profile_comparisons(entries, [])
        assert len(comparisons) == 1
        comp = comparisons[0]
        assert comp.profile_a == "terse"
        assert comp.profile_b == "verbose"
        assert comp.role == "backend"
        assert comp.model == "sonnet"
        assert comp.tasks_a == MIN_COMPARABLE_TASKS
        assert comp.tasks_b == MIN_COMPARABLE_TASKS
        assert comp.mean_cost_usd_per_task_a == pytest.approx(0.10)
        assert comp.mean_cost_usd_per_task_b == pytest.approx(0.30)
        assert comp.mean_output_tokens_per_task_a == pytest.approx(100.0)
        assert comp.mean_output_tokens_per_task_b == pytest.approx(300.0)

    def test_transitioned_tasks_do_not_count_toward_n(self) -> None:
        entries = self._cohort("terse", MIN_COMPARABLE_TASKS) + self._cohort("verbose", MIN_COMPARABLE_TASKS)
        transitions = [
            ProfileTransition(
                ts=0.0,
                ts_iso="",
                task_id="verbose-backend-sonnet-0",
                agent_id="a",
                from_profile="verbose",
                to_profile="terse",
                from_sha256="",
                to_sha256="",
            )
        ]
        assert compute_profile_comparisons(entries, transitions) == []

    def test_min_tasks_override(self) -> None:
        entries = self._cohort("terse", 2) + self._cohort("verbose", 2)
        comparisons = compute_profile_comparisons(entries, [], min_tasks=2)
        assert len(comparisons) == 1
