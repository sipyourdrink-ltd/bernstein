"""Unit tests for the tiered compaction strategy.

Covers:
- The policy selector picking a tier from budget pressure.
- Each tier's trigger predicate.
- Each tier's reduction behaviour.
- The no-pressure no-op.
- Cost attribution and trace recording.
"""

from pathlib import Path

import pytest

from bernstein.core.lineage.spine import content_hash_of
from bernstein.core.memory.compaction import (
    ABSENT_HASH,
    BudgetPressure,
    Tier,
    TierContext,
    TierResult,
    compact,
    record_tier_event,
    run_legacy_compaction,
    select_tier,
    verify_compacted_step,
    verify_compaction_references,
)
from bernstein.core.memory.compaction import auto as auto_tier
from bernstein.core.memory.compaction import micro as micro_tier
from bernstein.core.memory.compaction import session_memory as session_tier
from bernstein.core.memory.compaction import time_based as time_tier
from bernstein.core.memory.compaction.tiers import TIER_COST_WEIGHT, estimate_tokens
from bernstein.core.memory.compaction.verification import (
    compute_referenced_content_hashes,
    compute_source_content_hash,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _tool_result_block(body: str) -> str:
    return f"```tool_result name=bash\n{body}\n```"


# ---------------------------------------------------------------------------
# Policy: tier selection by budget pressure
# ---------------------------------------------------------------------------


class TestPolicySelection:
    def test_no_pressure_selects_none(self) -> None:
        assert select_tier(BudgetPressure()) is Tier.NONE

    def test_opening_turn_selects_none(self) -> None:
        # Turn 1 is below the micro threshold even with some context use.
        assert select_tier(BudgetPressure(turn_count=1, context_pct_used=0.3)) is Tier.NONE

    def test_mild_pressure_selects_micro(self) -> None:
        p = BudgetPressure(turn_count=3, context_pct_used=0.20)
        assert select_tier(p) is Tier.MICRO

    def test_threshold_pressure_selects_auto(self) -> None:
        p = BudgetPressure(turn_count=3, context_pct_used=0.85)
        assert select_tier(p) is Tier.AUTO

    def test_idle_live_session_selects_time_based(self) -> None:
        p = BudgetPressure(turn_count=3, context_pct_used=0.20, idle_seconds=400.0)
        assert select_tier(p) is Tier.TIME_BASED

    def test_session_complete_selects_session_memory(self) -> None:
        p = BudgetPressure(turn_count=3, context_pct_used=0.85, session_complete=True)
        assert select_tier(p) is Tier.SESSION_MEMORY

    def test_session_complete_outranks_auto(self) -> None:
        # Both auto and session-memory triggers could fire; completion wins.
        p = BudgetPressure(turn_count=9, context_pct_used=0.99, session_complete=True)
        assert select_tier(p) is Tier.SESSION_MEMORY

    def test_auto_outranks_time_based(self) -> None:
        # High context use plus idle: the heavier auto tier wins.
        p = BudgetPressure(turn_count=3, context_pct_used=0.85, idle_seconds=400.0)
        assert select_tier(p) is Tier.AUTO


# ---------------------------------------------------------------------------
# Trigger predicates per tier
# ---------------------------------------------------------------------------


class TestTriggers:
    def test_micro_requires_two_turns_and_some_use(self) -> None:
        assert micro_tier.should_run(BudgetPressure(turn_count=2, context_pct_used=0.1))
        assert not micro_tier.should_run(BudgetPressure(turn_count=1, context_pct_used=0.1))
        assert not micro_tier.should_run(BudgetPressure(turn_count=5, context_pct_used=0.0))

    def test_auto_requires_threshold_and_live_session(self) -> None:
        assert auto_tier.should_run(BudgetPressure(context_pct_used=auto_tier.THRESHOLD_PCT))
        assert not auto_tier.should_run(BudgetPressure(context_pct_used=0.5))
        assert not auto_tier.should_run(BudgetPressure(context_pct_used=0.99, session_complete=True))

    def test_time_based_requires_idle_and_live_session(self) -> None:
        idle = time_tier.IDLE_THRESHOLD_SECONDS
        assert time_tier.should_run(BudgetPressure(idle_seconds=idle))
        assert not time_tier.should_run(BudgetPressure(idle_seconds=idle - 1))
        assert not time_tier.should_run(BudgetPressure(idle_seconds=idle, session_complete=True))

    def test_session_memory_requires_completion(self) -> None:
        assert session_tier.should_run(BudgetPressure(session_complete=True))
        assert not session_tier.should_run(BudgetPressure(session_complete=False))


# ---------------------------------------------------------------------------
# Reduction behaviour per tier
# ---------------------------------------------------------------------------


class TestReduction:
    def test_micro_collapses_long_tool_bodies(self) -> None:
        text = _tool_result_block("A" * 600)
        ctx = TierContext(session_id="s", context_text=text)
        result = micro_tier.compact(ctx)
        assert result.tier is Tier.MICRO
        assert result.after_tokens < result.before_tokens
        assert "[tool result body pruned" in result.compacted_text
        # Header is retained so the agent knows the call happened.
        assert "tool_result name=bash" in result.compacted_text

    def test_micro_leaves_short_bodies_untouched(self) -> None:
        text = _tool_result_block("short body")
        ctx = TierContext(session_id="s", context_text=text)
        result = micro_tier.compact(ctx)
        assert result.compacted_text == text
        assert result.tokens_saved == 0

    def test_auto_summarises_and_reduces(self) -> None:
        text = "\n".join(f"# section {i}\n" + "detail line " * 40 for i in range(20))
        ctx = TierContext(session_id="s", context_text=text)
        result = auto_tier.compact(ctx)
        assert result.tier is Tier.AUTO
        assert result.after_tokens < result.before_tokens

    def test_auto_routes_llm_call_to_pipeline(self) -> None:
        # The auto tier forwards ``llm_call`` to the shared pipeline, which
        # delegates the actual summary to the orchestrator rather than
        # calling the summarizer inline. With a callable supplied the
        # delegated-summary path runs and the structural fallback does not.
        def fake_llm(prompt: str) -> str:  # pragma: no cover - not invoked inline
            return "summary"

        ctx = TierContext(session_id="s", context_text="# h\n" + "x " * 200)
        delegated = auto_tier.compact(ctx, llm_call=fake_llm)
        structural = auto_tier.compact(TierContext(session_id="s", context_text="# h\n" + "x " * 200))
        assert delegated.tier is Tier.AUTO
        assert delegated.compacted_text != structural.compacted_text

    def test_session_memory_builds_durable_summary(self) -> None:
        text = "\n".join(f"# section {i}\n" + "detail " * 50 for i in range(30))
        ctx = TierContext(session_id="s", context_text=text)
        result = session_tier.compact(ctx)
        assert result.tier is Tier.SESSION_MEMORY
        assert result.compacted_text.startswith("[session summary]")
        assert result.after_tokens < result.before_tokens

    def test_time_based_prunes_aged_blocks(self) -> None:
        text = "[age:12] very old block\n" + "stale content " * 20 + "\n\n[age:1] fresh block\nkept content here"
        ctx = TierContext(session_id="s", context_text=text)
        result = time_tier.compact(ctx)
        assert result.tier is Tier.TIME_BASED
        assert result.after_tokens < result.before_tokens
        assert "[stale block pruned]" in result.compacted_text
        assert "fresh block" in result.compacted_text

    def test_time_based_keeps_recent_blocks(self) -> None:
        text = "[age:1] recent\nkept content\n\n[age:2] also recent\nalso kept"
        ctx = TierContext(session_id="s", context_text=text)
        result = time_tier.compact(ctx)
        assert "[stale block pruned]" not in result.compacted_text
        assert result.tokens_saved == 0


# ---------------------------------------------------------------------------
# No-pressure no-op
# ---------------------------------------------------------------------------


class TestNoOp:
    def test_no_pressure_is_noop(self) -> None:
        text = "some context that should not change"
        ctx = TierContext(session_id="s", context_text=text, pressure=BudgetPressure())
        result = compact(ctx)
        assert result.tier is Tier.NONE
        assert result.compacted_text == text
        assert result.before_tokens == result.after_tokens
        assert result.tokens_saved == 0
        assert result.cost_estimate == 0.0
        assert result.correlation_id == ""

    def test_noop_costs_nothing(self) -> None:
        ctx = TierContext(session_id="s", context_text="x" * 400, pressure=BudgetPressure())
        result = compact(ctx)
        assert result.cost_estimate == 0.0


# ---------------------------------------------------------------------------
# Cost attribution
# ---------------------------------------------------------------------------


class TestCostAttribution:
    def test_cost_scales_with_tier_weight(self) -> None:
        # Same reducible content, run through micro and time-based; the
        # cheaper tier weight must yield no greater cost per token saved.
        assert TIER_COST_WEIGHT[Tier.MICRO] < TIER_COST_WEIGHT[Tier.AUTO]
        assert TIER_COST_WEIGHT[Tier.AUTO] < TIER_COST_WEIGHT[Tier.SESSION_MEMORY]

    def test_cost_estimate_is_nonnegative(self) -> None:
        text = _tool_result_block("B" * 800)
        ctx = TierContext(session_id="s", context_text=text)
        result = micro_tier.compact(ctx)
        assert result.cost_estimate >= 0.0

    def test_cost_zero_when_nothing_saved(self) -> None:
        text = _tool_result_block("tiny")
        ctx = TierContext(session_id="s", context_text=text)
        result = micro_tier.compact(ctx)
        assert result.cost_estimate == 0.0


# ---------------------------------------------------------------------------
# Trace recording
# ---------------------------------------------------------------------------


class TestTraceRecording:
    def test_record_tier_event_carries_tier_and_cost(self) -> None:
        from bernstein.core.observability.traces import AgentTrace

        text = _tool_result_block("C" * 900)
        ctx = TierContext(
            session_id="s",
            context_text=text,
            pressure=BudgetPressure(turn_count=3, context_pct_used=0.2),
        )
        result = compact(ctx)
        trace = AgentTrace(
            trace_id="t1",
            session_id="s",
            task_ids=["task-a"],
            agent_role="dev",
            model="sonnet",
            effort="high",
            spawn_ts=0.0,
        )
        step = record_tier_event(trace, result)
        assert step.type == "compact"
        assert step.compaction_tokens_before == result.before_tokens
        assert step.compaction_tokens_after == result.after_tokens
        assert step.compaction_correlation_id == result.correlation_id
        assert step.compaction_source_content_hash == result.source_content_hash
        assert step.compaction_referenced_content_hashes == dict(result.referenced_content_hashes)
        assert "tier=micro" in step.detail
        assert "cost_estimate=" in step.detail

    def test_result_to_dict_round_trips_fields(self) -> None:
        result = compact(
            TierContext(
                session_id="s",
                context_text=_tool_result_block("D" * 700),
                pressure=BudgetPressure(turn_count=3, context_pct_used=0.2),
                referenced_content_hashes={"src/test.py": "sha256:1234"},
            )
        )
        d = result.to_dict()
        assert d["tier"] == "micro"
        assert d["before_tokens"] == result.before_tokens
        assert d["after_tokens"] == result.after_tokens
        assert d["cost_estimate"] == result.cost_estimate
        assert d["source_content_hash"] == result.source_content_hash
        assert d["referenced_content_hashes"] == {"src/test.py": "sha256:1234"}

        # Deserialise via from_dict
        restored = TierResult.from_dict(d)
        assert restored.tier == result.tier
        assert restored.source_content_hash == result.source_content_hash
        assert restored.referenced_content_hashes == result.referenced_content_hashes

    def test_from_dict_handles_missing_keys_gracefully(self) -> None:
        # Older records without the new hash keys must still load cleanly
        legacy_dict = {
            "tier": "micro",
            "before_tokens": 100,
            "after_tokens": 50,
            "tokens_saved": 50,
            "cost_estimate": 0.001,
            "correlation_id": "c1",
            "reason": "prune",
        }
        restored = TierResult.from_dict(legacy_dict)
        assert restored.tier is Tier.MICRO
        assert restored.source_content_hash == ""
        assert restored.referenced_content_hashes == {}


# ---------------------------------------------------------------------------
# Source content hashing (Issue #3696)
# ---------------------------------------------------------------------------


class TestContentHashing:
    @pytest.mark.parametrize(
        ("tier_pressure", "expected_tier"),
        [
            (BudgetPressure(), Tier.NONE),
            (BudgetPressure(turn_count=3, context_pct_used=0.2), Tier.MICRO),
            (BudgetPressure(turn_count=3, context_pct_used=0.85), Tier.AUTO),
            (BudgetPressure(turn_count=3, context_pct_used=0.2, idle_seconds=400.0), Tier.TIME_BASED),
            (BudgetPressure(session_complete=True), Tier.SESSION_MEMORY),
        ],
    )
    def test_compaction_produces_source_content_hash_matching_recomputation(
        self, tier_pressure: BudgetPressure, expected_tier: Tier
    ) -> None:
        text = _tool_result_block("Hello world context pre-image content")
        ctx = TierContext(session_id="s-hash", context_text=text, pressure=tier_pressure)
        result = compact(ctx)
        assert result.tier is expected_tier
        expected_hash = content_hash_of(text.encode("utf-8"))
        assert result.source_content_hash == expected_hash

    def test_identical_preimages_produce_identical_hashes(self) -> None:
        text = "Exact duplicate pre-compaction text for hashing test"
        ctx1 = TierContext(
            session_id="s1", context_text=text, pressure=BudgetPressure(turn_count=2, context_pct_used=0.1)
        )
        ctx2 = TierContext(
            session_id="s2", context_text=text, pressure=BudgetPressure(turn_count=2, context_pct_used=0.1)
        )
        r1 = compact(ctx1)
        r2 = compact(ctx2)
        assert r1.source_content_hash == r2.source_content_hash
        assert len(r1.source_content_hash) > 0

    def test_one_byte_difference_produces_different_hash(self) -> None:
        text1 = "Some context payload alpha."
        text2 = "Some context payload alpha!"
        ctx1 = TierContext(session_id="s1", context_text=text1)
        ctx2 = TierContext(session_id="s2", context_text=text2)
        r1 = micro_tier.compact(ctx1)
        r2 = micro_tier.compact(ctx2)
        assert r1.source_content_hash != r2.source_content_hash

    def test_explicit_utf8_encoding_used(self) -> None:
        unicode_text = "Compaction pre-image with unicode: \U0001f30d \u2705 \u00e9\u00e8"
        ctx = TierContext(session_id="s", context_text=unicode_text)
        result = compact(ctx)
        expected_digest = content_hash_of(unicode_text.encode("utf-8"))
        assert result.source_content_hash == expected_digest
        assert compute_source_content_hash(unicode_text) == expected_digest


# ---------------------------------------------------------------------------
# Referenced artifact hashing (Issue #3696)
# ---------------------------------------------------------------------------


class TestReferencedArtifactHashing:
    def test_referenced_files_on_disk_are_hashed(self, tmp_path: Path) -> None:
        f1 = tmp_path / "a.py"
        f1.write_text("print('hello')", encoding="utf-8")
        f2 = tmp_path / "b.txt"
        f2.write_bytes(b"binary content bytes")

        ctx = TierContext(
            session_id="s",
            context_text=_tool_result_block("content referencing files"),
            referenced_paths=["a.py", "b.txt"],
            root_dir=tmp_path,
        )
        result = micro_tier.compact(ctx)
        assert result.referenced_content_hashes["a.py"] == content_hash_of(f1.read_bytes())
        assert result.referenced_content_hashes["b.txt"] == content_hash_of(f2.read_bytes())

    def test_missing_referenced_file_records_absent(self, tmp_path: Path) -> None:
        f_real = tmp_path / "real.py"
        f_real.write_text("exists", encoding="utf-8")

        ctx = TierContext(
            session_id="s",
            context_text=_tool_result_block("referencing missing file"),
            referenced_paths=["real.py", "missing.py"],
            root_dir=tmp_path,
        )
        result = micro_tier.compact(ctx)
        assert result.referenced_content_hashes["real.py"] == content_hash_of(b"exists")
        assert result.referenced_content_hashes["missing.py"] == ABSENT_HASH

    def test_precomputed_referenced_hashes_preserved(self) -> None:
        precomputed = {"cached/file.py": "sha256:fedcba9876543210"}
        ctx = TierContext(
            session_id="s",
            context_text="short",
            referenced_content_hashes=precomputed,
        )
        result = compact(ctx)
        assert result.referenced_content_hashes == precomputed

    def test_compute_referenced_content_hashes_direct(self, tmp_path: Path) -> None:
        f = tmp_path / "x.txt"
        f.write_bytes(b"content")
        computed = compute_referenced_content_hashes(
            ["x.txt", "missing.txt"],
            precomputed={"pre.txt": "sha256:999"},
            root_dir=tmp_path,
        )
        assert computed["x.txt"] == content_hash_of(b"content")
        assert computed["missing.txt"] == ABSENT_HASH
        assert computed["pre.txt"] == "sha256:999"


# ---------------------------------------------------------------------------
# Verification helper (Issue #3696)
# ---------------------------------------------------------------------------


class TestCompactionVerification:
    def test_verify_compaction_references_empty(self) -> None:
        v_res = verify_compaction_references({})
        assert v_res.valid is True
        assert v_res.checked_count == 0

    def test_verification_succeeds_when_artifacts_unchanged(self, tmp_path: Path) -> None:
        f = tmp_path / "target.py"
        f.write_text("x = 1\n", encoding="utf-8")

        ctx = TierContext(
            session_id="s",
            context_text=_tool_result_block("context"),
            referenced_paths=["target.py"],
            root_dir=tmp_path,
        )
        result = compact(ctx)
        v_res = verify_compacted_step(result, root_dir=tmp_path)
        assert v_res.valid is True
        assert len(v_res.divergences) == 0
        assert v_res.checked_count == 1
        assert "all match" in v_res.report()

    def test_verification_detects_post_compaction_modification(self, tmp_path: Path) -> None:
        f = tmp_path / "target.py"
        f.write_text("x = 1\n", encoding="utf-8")
        original_hash = content_hash_of(b"x = 1\n")

        ctx = TierContext(
            session_id="s",
            context_text=_tool_result_block("context"),
            referenced_paths=["target.py"],
            root_dir=tmp_path,
        )
        result = compact(ctx)
        assert result.referenced_content_hashes["target.py"] == original_hash

        # Modify file after compaction
        f.write_text("x = 2 # modified post-compaction\n", encoding="utf-8")
        new_hash = content_hash_of(b"x = 2 # modified post-compaction\n")

        v_res = verify_compacted_step(result, root_dir=tmp_path)
        assert v_res.valid is False
        assert len(v_res.divergences) == 1
        div = v_res.divergences[0]
        assert div.path == "target.py"
        assert div.expected_hash == original_hash
        assert div.actual_hash == new_hash
        assert div.is_divergent is True
        assert "Divergence detected" in v_res.report()

    def test_verification_detects_post_compaction_deletion(self, tmp_path: Path) -> None:
        f = tmp_path / "deleted.py"
        f.write_text("to be deleted\n", encoding="utf-8")
        original_hash = content_hash_of(b"to be deleted\n")

        ctx = TierContext(
            session_id="s",
            context_text=_tool_result_block("context"),
            referenced_paths=["deleted.py"],
            root_dir=tmp_path,
        )
        result = compact(ctx)

        # Delete file after compaction
        f.unlink()

        v_res = verify_compacted_step(result, root_dir=tmp_path)
        assert v_res.valid is False
        assert len(v_res.divergences) == 1
        assert v_res.divergences[0].expected_hash == original_hash
        assert v_res.divergences[0].actual_hash == ABSENT_HASH

    def test_verification_detects_post_compaction_creation(self, tmp_path: Path) -> None:
        # File was missing at compaction time (recorded as "absent")
        ctx = TierContext(
            session_id="s",
            context_text=_tool_result_block("context"),
            referenced_paths=["created_later.py"],
            root_dir=tmp_path,
        )
        result = compact(ctx)
        assert result.referenced_content_hashes["created_later.py"] == ABSENT_HASH

        # File is created afterwards
        (tmp_path / "created_later.py").write_text("new content", encoding="utf-8")

        v_res = verify_compacted_step(result, root_dir=tmp_path)
        assert v_res.valid is False
        assert len(v_res.divergences) == 1
        assert v_res.divergences[0].expected_hash == ABSENT_HASH
        assert v_res.divergences[0].actual_hash == content_hash_of(b"new content")

    def test_verification_with_trace_step(self, tmp_path: Path) -> None:
        from bernstein.core.observability.traces import AgentTrace

        f = tmp_path / "module.py"
        f.write_text("def run(): pass\n", encoding="utf-8")

        ctx = TierContext(
            session_id="s-trace",
            context_text=_tool_result_block("context text"),
            referenced_paths=["module.py"],
            root_dir=tmp_path,
        )
        result = compact(ctx)
        trace = AgentTrace(
            trace_id="t-verify",
            session_id="s-trace",
            task_ids=["t1"],
            agent_role="dev",
            model="sonnet",
            effort="high",
            spawn_ts=0.0,
        )
        step = record_tier_event(trace, result)

        # Verify directly from TraceStep
        v_res = verify_compacted_step(step, root_dir=tmp_path)
        assert v_res.valid is True
        assert v_res.checked_count == 1

        # Now mutate the file
        f.write_text("def run(): fail()\n", encoding="utf-8")
        v_res2 = verify_compacted_step(step, root_dir=tmp_path)
        assert v_res2.valid is False
        assert len(v_res2.divergences) == 1


# ---------------------------------------------------------------------------
# Legacy shim
# ---------------------------------------------------------------------------


class TestLegacyShim:
    def test_legacy_entrypoint_defers_to_policy(self) -> None:
        text = "\n".join(f"# section {i}\n" + "detail " * 40 for i in range(20))
        result = run_legacy_compaction("s", text)
        # Full-context default pressure selects the auto tier.
        assert result.tier is Tier.AUTO
        assert result.after_tokens < result.before_tokens


# ---------------------------------------------------------------------------
# Shared estimator
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected_floor"),
    [("", 0), ("a", 1), ("a" * 40, 10)],
)
def test_estimate_tokens(text: str, expected_floor: int) -> None:
    assert estimate_tokens(text) == expected_floor
