"""Unit tests for spawn-time prompt budget check (#4377)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from bernstein.core.agents import spawn_prompt_budget
from bernstein.core.agents.spawn_prompt_budget import (
    SpawnPromptBudgetResult,
    check_spawn_prompt_budget,
    get_spawn_prompt_budget,
)

if TYPE_CHECKING:
    from pathlib import Path


def _make_sections(*pairs: tuple[str, int]) -> list[tuple[str, str]]:
    """Build named_sections from (name, char_count) pairs."""
    return [(name, "x" * count) for name, count in pairs]


class TestInjectedToolDefinitionCounting:
    """Full definitions are measured locally, independently of prompt assembly."""

    @staticmethod
    def _counter():
        counter = getattr(spawn_prompt_budget, "_count_injected_tool_definitions", None)
        assert callable(counter), "full injected tool definition counter is missing"
        return counter

    @pytest.mark.parametrize("supplied_counter", [False, True])
    def test_full_definitions_per_server(self, supplied_counter: bool) -> None:
        from bernstein.core.tokens.token_counter import count_tokens_via_estimation

        large = {
            "name": "report",
            "description": "Отчёт 🧪",
            "inputSchema": {"type": "object"},
            "outputSchema": {"description": "結果" * 10_000, "type": "string"},
            "annotations": {"readOnlyHint": True},
            "_meta": {"vendor": "extra"},
        }
        definitions = {"alpha": [large, {"name": "ping"}], "beta": [{"name": "echo"}]}
        serialized = [
            json.dumps(t, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            for tools in definitions.values()
            for t in tools
        ]
        calls = []

        def counter(text: str) -> int:
            calls.append(text)
            return len(text.encode("utf-8")) + 7

        count = counter if supplied_counter else lambda text: count_tokens_via_estimation(text, file_type="json")
        result = self._counter()(definitions, counter=counter) if supplied_counter else self._counter()(definitions)
        if supplied_counter:
            assert calls == serialized
        assert result.per_server_tokens == {
            "alpha": sum(count(text) for text in serialized[:2]),
            "beta": count(serialized[2]),
        }
        assert result.total_tokens == sum(result.per_server_tokens.values())
        reordered = json.loads(json.dumps(definitions, sort_keys=True))
        assert self._counter()(reordered).per_server_tokens == self._counter()(definitions).per_server_tokens
        reduced = {**definitions, "alpha": [{k: v for k, v in large.items() if k != "outputSchema"}]}
        assert self._counter()(reduced).total_tokens < self._counter()(definitions).total_tokens

    @pytest.mark.parametrize("tools, expected", [([], 0), (None, None)])
    def test_empty_and_missing_discovery(self, tools, expected) -> None:
        def counter(text: str) -> int:
            pytest.fail("empty or missing definitions must not invoke the counter")

        result = self._counter()({"stub": tools, "empty": []}, counter=counter)
        assert result.per_server_tokens == {"stub": expected, "empty": 0}
        assert result.total_tokens == expected
        assert self._counter()({}).total_tokens == 0


class TestCheckSpawnPromptBudget:
    """Tests for check_spawn_prompt_budget."""

    def test_multi_source_attribution_breakdown(self) -> None:
        """Breakdown lists each source with bytes and tokens."""
        sections = _make_sections(
            ("role", 4000),
            ("instructions", 8000),
            ("context", 2000),
        )
        result = check_spawn_prompt_budget(
            sections,
            model="sonnet",
            budget_pct=100.0,  # generous budget so we focus on breakdown
        )
        assert isinstance(result, SpawnPromptBudgetResult)
        assert len(result.section_breakdown) == 3

        # Each entry is (name, bytes, tokens) with tokens > 0
        names_in_breakdown = {name for name, _, _ in result.section_breakdown}
        assert names_in_breakdown == {"role", "instructions", "context"}

        for _name, nbytes, tokens in result.section_breakdown:
            assert nbytes > 0
            assert tokens > 0

        # Verify sorted descending by tokens
        token_counts = [t for _, _, t in result.section_breakdown]
        assert token_counts == sorted(token_counts, reverse=True)

    def test_warning_fires_at_boundary(self) -> None:
        """Oversized prompt triggers warning with per-source attribution."""
        # Build a prompt that clearly exceeds 5% of a 200k context window
        # 5% of 200k = 10k tokens ≈ 40k chars
        sections = _make_sections(
            ("role", 50_000),  # ~12,500 tokens
            ("instructions", 60_000),  # ~15,000 tokens
            ("context", 20_000),  # ~5,000 tokens
        )
        result = check_spawn_prompt_budget(
            sections,
            model="sonnet",  # 200k context
            budget_pct=5.0,  # 10k token budget -> exceeded
        )
        assert result.over_budget is True
        assert result.warning_message != ""
        assert "Spawn prompt budget exceeded" in result.warning_message
        # Per-source attribution must be present
        assert "role" in result.warning_message
        assert "instructions" in result.warning_message
        assert "context" in result.warning_message
        assert "tokens" in result.warning_message
        assert "bytes" in result.warning_message

    def test_within_budget_no_warning(self) -> None:
        """Small prompt within budget produces no warning."""
        sections = _make_sections(
            ("role", 100),
            ("instructions", 200),
        )
        result = check_spawn_prompt_budget(
            sections,
            model="sonnet",  # 200k context
            budget_pct=25.0,  # 50k token budget -> not exceeded
        )
        assert result.over_budget is False
        assert result.warning_message == ""
        assert result.total_estimated_tokens > 0
        assert result.utilization_pct < 1.0

    def test_absolute_fallback_budget(self) -> None:
        """Unknown model falls back to absolute token budget."""
        # With unknown model, resolve_context_limit returns 200k default.
        # But we explicitly test the abs_budget path by setting budget_pct
        # that would produce 0 budget_tokens (e.g., 0% of context)
        # then verifying abs_budget kicks in.
        sections = _make_sections(
            ("role", 200_000),  # ~50k tokens
        )
        result = check_spawn_prompt_budget(
            sections,
            model="",
            budget_pct=0.001,  # essentially 0% -> budget_tokens ~0
            abs_budget=100,  # tiny absolute budget
        )
        # With budget_pct=0.001% of 200k = 2 tokens, the 50k prompt exceeds it
        assert result.over_budget is True
        assert result.total_estimated_tokens > 100


class TestGetSpawnPromptBudget:
    """Tests for the session-keyed cache."""

    def test_cached_result_retrievable(self) -> None:
        """Result is cached when session_id is provided."""
        sections = _make_sections(("role", 400))
        sid = "test-session-budget-cache"
        result = check_spawn_prompt_budget(
            sections,
            model="sonnet",
            session_id=sid,
        )
        cached = get_spawn_prompt_budget(sid)
        assert cached is result

    def test_no_cache_without_session_id(self) -> None:
        """Result is not cached when session_id is empty."""
        sections = _make_sections(("role", 400))
        check_spawn_prompt_budget(sections, model="sonnet", session_id="")
        assert get_spawn_prompt_budget("") is None


class TestBudgetRunsOnTheSpawnPath:
    """The budget must be measured on the prompt the adapter is handed.

    ``spawn_prompt._render_prompt`` and ``spawner_core._render_prompt_with_receipt``
    are two separate prompt builders, and only the second one runs when an
    agent is actually spawned. A budget wired into the first reports on a
    prompt no run ever sees, so these tests pin the check to the spawner's
    own builder.
    """

    def test_spawner_builder_records_a_budget(self, tmp_path: Path, make_task) -> None:
        """Rendering through the spawner's builder caches a budget result."""
        from bernstein.core.agents.spawner_core import _render_prompt_with_receipt

        session_id = "budget-on-spawn-path"
        prompt, _receipt = _render_prompt_with_receipt(
            [make_task()],
            tmp_path,
            tmp_path,
            session_id=session_id,
            model="sonnet",
        )

        assert prompt
        recorded = get_spawn_prompt_budget(session_id)
        assert recorded is not None, (
            "the spawner's prompt builder did not measure a budget - a budget "
            "checked only in spawn_prompt._render_prompt never sees a real spawn"
        )
        assert recorded.total_estimated_tokens > 0
        assert recorded.section_breakdown

    def test_spawner_builder_flags_an_over_budget_prompt(self, tmp_path: Path, make_task) -> None:
        """A prompt far past the absolute fallback is recorded as over budget."""
        from bernstein.core.agents.spawner_core import _render_prompt_with_receipt

        session_id = "budget-on-spawn-path-over"
        # No model, so the absolute fallback budget applies; the goal alone
        # carries enough characters to exceed it.
        _render_prompt_with_receipt(
            [make_task(description="y" * 400_000)],
            tmp_path,
            tmp_path,
            session_id=session_id,
        )

        recorded = get_spawn_prompt_budget(session_id)
        assert recorded is not None
        assert recorded.over_budget is True
        assert recorded.warning_message
