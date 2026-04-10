"""Tests for agent skill badges."""

from __future__ import annotations

from bernstein.core.skill_badges import (
    AgentSkillSet,
    SkillBadge,
    SkillLevel,
    format_skill_badges,
    get_skill_set,
)

# -- SkillLevel ordering ----------------------------------------------------


class TestSkillLevelOrdering:
    """SkillLevel values must be orderable via their integer backing."""

    def test_none_is_lowest(self) -> None:
        assert SkillLevel.NONE < SkillLevel.BASIC

    def test_basic_lt_proficient(self) -> None:
        assert SkillLevel.BASIC < SkillLevel.PROFICIENT

    def test_proficient_lt_expert(self) -> None:
        assert SkillLevel.PROFICIENT < SkillLevel.EXPERT

    def test_full_ordering(self) -> None:
        ordered = sorted([SkillLevel.EXPERT, SkillLevel.NONE, SkillLevel.PROFICIENT, SkillLevel.BASIC])
        assert ordered == [
            SkillLevel.NONE,
            SkillLevel.BASIC,
            SkillLevel.PROFICIENT,
            SkillLevel.EXPERT,
        ]

    def test_integer_values(self) -> None:
        assert int(SkillLevel.NONE) == 0
        assert int(SkillLevel.BASIC) == 1
        assert int(SkillLevel.PROFICIENT) == 2
        assert int(SkillLevel.EXPERT) == 3


# -- SkillBadge creation ----------------------------------------------------


class TestSkillBadgeCreation:
    """SkillBadge is a frozen dataclass with name, level, icon."""

    def test_create_badge(self) -> None:
        badge = SkillBadge(name="reasoning", level=SkillLevel.EXPERT, icon="\U0001f9e0")
        assert badge.name == "reasoning"
        assert badge.level == SkillLevel.EXPERT
        assert badge.icon == "\U0001f9e0"

    def test_frozen(self) -> None:
        badge = SkillBadge(name="tools", level=SkillLevel.BASIC, icon="\U0001f527")
        try:
            badge.name = "hacked"  # type: ignore[misc]
            raise AssertionError("Should have raised FrozenInstanceError")
        except AttributeError:
            pass  # expected

    def test_equality(self) -> None:
        a = SkillBadge(name="code", level=SkillLevel.PROFICIENT, icon="\U0001f4bb")
        b = SkillBadge(name="code", level=SkillLevel.PROFICIENT, icon="\U0001f4bb")
        assert a == b

    def test_inequality_on_level(self) -> None:
        a = SkillBadge(name="code", level=SkillLevel.BASIC, icon="\U0001f4bb")
        b = SkillBadge(name="code", level=SkillLevel.EXPERT, icon="\U0001f4bb")
        assert a != b


# -- get_skill_set -----------------------------------------------------------


class TestGetSkillSet:
    """get_skill_set resolves profiles with exact -> adapter wildcard -> fallback."""

    def test_exact_match_claude_opus(self) -> None:
        ss = get_skill_set("claude", "opus", "agent-001")
        assert ss.agent_id == "agent-001"
        assert ss.adapter == "claude"
        assert ss.model == "opus"
        # claude/opus should have EXPERT reasoning
        reasoning = next(b for b in ss.badges if b.name == "reasoning")
        assert reasoning.level == SkillLevel.EXPERT

    def test_exact_match_codex_gpt4(self) -> None:
        ss = get_skill_set("codex", "gpt-4", "agent-002")
        reasoning = next(b for b in ss.badges if b.name == "reasoning")
        assert reasoning.level == SkillLevel.PROFICIENT
        code = next(b for b in ss.badges if b.name == "code")
        assert code.level == SkillLevel.EXPERT

    def test_adapter_wildcard_fallback(self) -> None:
        # Unknown model under claude adapter should hit claude/*
        ss = get_skill_set("claude", "unknown-model-v99", "agent-003")
        assert ss.adapter == "claude"
        assert ss.model == "unknown-model-v99"
        reasoning = next(b for b in ss.badges if b.name == "reasoning")
        assert reasoning.level == SkillLevel.PROFICIENT  # claude/* profile

    def test_global_fallback(self) -> None:
        # Completely unknown adapter/model
        ss = get_skill_set("mystery-adapter", "mystery-model", "agent-004")
        assert ss.adapter == "mystery-adapter"
        for badge in ss.badges:
            assert badge.level == SkillLevel.BASIC

    def test_returns_agent_skill_set_type(self) -> None:
        ss = get_skill_set("gemini", "pro", "agent-005")
        assert isinstance(ss, AgentSkillSet)

    def test_badges_list_is_independent_copy(self) -> None:
        ss1 = get_skill_set("claude", "opus", "a1")
        ss2 = get_skill_set("claude", "opus", "a2")
        # Mutating one should not affect the other
        ss1.badges.append(SkillBadge(name="extra", level=SkillLevel.NONE, icon="?"))
        assert len(ss2.badges) == 3


# -- format_skill_badges -----------------------------------------------------


class TestFormatSkillBadges:
    """format_skill_badges renders Rich-friendly badge strings."""

    def test_contains_badge_names(self) -> None:
        ss = get_skill_set("claude", "opus", "agent-fmt")
        result = format_skill_badges(ss)
        assert "reasoning" in result
        assert "tools" in result
        assert "code" in result

    def test_contains_filled_stars_for_expert(self) -> None:
        ss = get_skill_set("claude", "opus", "agent-fmt")
        result = format_skill_badges(ss)
        # Expert = three filled stars
        assert "\u2605\u2605\u2605" in result

    def test_contains_mixed_stars_for_proficient(self) -> None:
        ss = get_skill_set("claude", "haiku", "agent-fmt")
        result = format_skill_badges(ss)
        # Haiku has BASIC reasoning -> one filled, two hollow
        assert "\u2605\u2606\u2606" in result

    def test_contains_bold_markup(self) -> None:
        ss = get_skill_set("claude", "opus", "agent-fmt")
        result = format_skill_badges(ss)
        assert "[bold]" in result
        assert "[/bold]" in result

    def test_empty_badges(self) -> None:
        ss = AgentSkillSet(agent_id="x", adapter="x", model="x", badges=[])
        result = format_skill_badges(ss)
        assert result == ""
