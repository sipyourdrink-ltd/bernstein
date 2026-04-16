"""Tests for prompt cache hint utilities and adapter integration.

Tests ``mark_cacheable_prefix`` in spawner_prompt_cache,
``build_cacheable_system_blocks`` in the Claude adapter, and
cache savings tracking in CostTracker.
"""

from __future__ import annotations

from bernstein.adapters.claude import build_cacheable_system_blocks
from bernstein.core.agents.spawner_prompt_cache import CacheableBlock, mark_cacheable_prefix
from bernstein.core.cost.cost_tracker import CostTracker, TokenUsage

# ---------------------------------------------------------------------------
# mark_cacheable_prefix
# ---------------------------------------------------------------------------


class TestMarkCacheablePrefix:
    """Tests for mark_cacheable_prefix identifying static vs dynamic blocks."""

    def test_role_template_is_cacheable(self) -> None:
        """Role template and coding standards should be marked cacheable."""
        parts = [
            "# You are a Backend Engineer\n\nYou implement server-side logic.",
            "\n## Assigned tasks\nTask 1: Fix the bug",
        ]
        blocks = mark_cacheable_prefix(parts)
        assert len(blocks) == 2
        assert blocks[0].cacheable is True
        assert blocks[1].cacheable is False

    def test_all_static_parts_before_first_dynamic(self) -> None:
        """Multiple static parts before the first dynamic marker are all cacheable."""
        parts = [
            "# Role prompt header",
            "## Coding standards\n- Python 3.12+",
            "## Specialist context\nAvailable agents...",
            "\n## Assigned tasks\nTask block here",
            "\n## Instructions\nDo the thing",
        ]
        blocks = mark_cacheable_prefix(parts)
        assert len(blocks) == 5
        assert blocks[0].cacheable is True
        assert blocks[1].cacheable is True
        assert blocks[2].cacheable is True
        assert blocks[3].cacheable is False
        assert blocks[4].cacheable is False

    def test_empty_parts_are_skipped(self) -> None:
        """Empty strings in the parts list should be omitted from output."""
        parts = ["Role template", "", "## Assigned tasks\nTasks"]
        blocks = mark_cacheable_prefix(parts)
        assert len(blocks) == 2
        assert blocks[0].content == "Role template"
        assert blocks[0].cacheable is True
        assert blocks[1].cacheable is False

    def test_no_dynamic_markers_all_cacheable(self) -> None:
        """If no dynamic markers are present, all parts are cacheable."""
        parts = ["Part 1", "Part 2", "Part 3"]
        blocks = mark_cacheable_prefix(parts)
        assert len(blocks) == 3
        assert all(b.cacheable for b in blocks)

    def test_first_part_is_dynamic(self) -> None:
        """If the first part contains a dynamic marker, nothing is cacheable."""
        parts = [
            "## Assigned tasks\nDo stuff",
            "More context",
        ]
        blocks = mark_cacheable_prefix(parts)
        assert len(blocks) == 2
        assert blocks[0].cacheable is False
        assert blocks[1].cacheable is False

    def test_all_dynamic_section_markers_detected(self) -> None:
        """Each recognized dynamic marker triggers the dynamic boundary."""
        markers = [
            "## Assigned tasks",
            "## Instructions",
            "## Team awareness",
            "## Persistent Memory",
            "## Relevant Code Context",
            "## File-scope context",
            "## Parent context",
            "## Predecessor context",
            "## Heartbeat",
            "## Token budget",
            "## Operational nudges",
        ]
        for marker in markers:
            blocks = mark_cacheable_prefix(["Static preamble", f"\n{marker}\nDynamic"])
            assert blocks[0].cacheable is True, f"Preamble before '{marker}' should be cacheable"
            assert blocks[1].cacheable is False, f"Block with '{marker}' should be dynamic"

    def test_empty_input_returns_empty(self) -> None:
        """Empty input list should return empty output."""
        assert mark_cacheable_prefix([]) == []

    def test_cacheable_block_is_frozen(self) -> None:
        """CacheableBlock should be immutable (frozen dataclass)."""
        block = CacheableBlock(content="test", cacheable=True)
        try:
            block.content = "modified"  # type: ignore[misc]
            raise AssertionError("Should not allow mutation")
        except AttributeError:
            pass  # Expected for frozen dataclass

    def test_content_preserved_exactly(self) -> None:
        """Block content should preserve the original string exactly."""
        original = "  Leading whitespace\n\ttabs\ntrailing  "
        blocks = mark_cacheable_prefix([original])
        assert blocks[0].content == original


# ---------------------------------------------------------------------------
# Claude adapter cache_control
# ---------------------------------------------------------------------------


class TestClaudeAdapterCacheControl:
    """Tests for Claude adapter cache_control block generation."""

    def test_cacheable_blocks_get_cache_control(self) -> None:
        """Non-empty system addendum should produce a block with cache_control."""
        blocks = build_cacheable_system_blocks("You are a backend engineer.")
        assert len(blocks) == 1
        assert blocks[0]["type"] == "text"
        assert blocks[0]["text"] == "You are a backend engineer."
        assert blocks[0]["cache_control"] == {"type": "ephemeral"}

    def test_empty_addendum_returns_empty(self) -> None:
        """Empty system addendum should produce no blocks."""
        assert build_cacheable_system_blocks("") == []

    def test_cache_control_structure_matches_anthropic_api(self) -> None:
        """The cache_control structure must match Anthropic's API spec."""
        blocks = build_cacheable_system_blocks("Role template content here")
        cache_ctrl = blocks[0]["cache_control"]
        assert isinstance(cache_ctrl, dict)
        assert cache_ctrl["type"] == "ephemeral"
        assert set(cache_ctrl.keys()) == {"type"}


# ---------------------------------------------------------------------------
# CostTracker cache savings
# ---------------------------------------------------------------------------


class TestCostTrackerCacheMetrics:
    """Tests for cache savings tracking in the cost report."""

    def test_cache_savings_computed_from_read_tokens(self) -> None:
        """Cache savings should reflect the discount on cache-read tokens."""
        tracker = CostTracker(run_id="test-run", budget_usd=100.0)
        # Sonnet: input=$3/1M, cache_read=$0.3/1M -> savings = $2.7/1M
        tracker.record(
            model="sonnet",
            input_tokens=1000,
            output_tokens=500,
            agent_id="agent-1",
            task_id="task-1",
            cache_read_tokens=100_000,
            cache_write_tokens=0,
        )
        savings = tracker.cache_savings_usd()
        # 100_000 tokens * (3.0 - 0.3) / 1_000_000 = 0.27
        assert abs(savings - 0.27) < 0.001

    def test_cache_savings_zero_when_no_cache_reads(self) -> None:
        """No cache reads means zero savings."""
        tracker = CostTracker(run_id="test-run", budget_usd=100.0)
        tracker.record(
            model="sonnet",
            input_tokens=1000,
            output_tokens=500,
            agent_id="agent-1",
            task_id="task-1",
        )
        assert tracker.cache_savings_usd() == 0.0

    def test_cache_savings_in_report(self) -> None:
        """The RunCostReport should include cache_savings_usd."""
        tracker = CostTracker(run_id="test-run", budget_usd=100.0)
        tracker.record(
            model="opus",
            input_tokens=1000,
            output_tokens=500,
            agent_id="agent-1",
            task_id="task-1",
            cache_read_tokens=50_000,
            cache_write_tokens=10_000,
        )
        report = tracker.report()
        # Opus: input=$5/1M, cache_read=$0.5/1M -> savings = $4.5/1M
        # 50_000 * 4.5 / 1_000_000 = 0.225
        assert report.cache_savings_usd > 0
        assert abs(report.cache_savings_usd - 0.225) < 0.001

    def test_cache_savings_serialized_in_report_dict(self) -> None:
        """cache_savings_usd should appear in the serialized report dict."""
        tracker = CostTracker(run_id="test-run", budget_usd=100.0)
        report = tracker.report()
        d = report.to_dict()
        assert "cache_savings_usd" in d
        assert d["cache_savings_usd"] == 0.0

    def test_token_usage_cache_fields(self) -> None:
        """TokenUsage should track cache_read_tokens and cache_write_tokens."""
        usage = TokenUsage(
            input_tokens=1000,
            output_tokens=500,
            model="sonnet",
            cost_usd=0.01,
            agent_id="agent-1",
            task_id="task-1",
            cache_read_tokens=5000,
            cache_write_tokens=2000,
        )
        d = usage.to_dict()
        assert d["cache_read_tokens"] == 5000
        assert d["cache_write_tokens"] == 2000

        restored = TokenUsage.from_dict(d)
        assert restored.cache_read_tokens == 5000
        assert restored.cache_write_tokens == 2000
