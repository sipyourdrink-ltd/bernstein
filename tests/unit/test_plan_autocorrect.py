"""Tests for smart error autocorrect (plan_autocorrect.py)."""

from __future__ import annotations

from bernstein.core.plan_autocorrect import (
    CorrectionSuggestion,
    apply_corrections,
    check_plan_for_typos,
    format_correction_prompt,
    fuzzy_match,
)

# ---------------------------------------------------------------------------
# fuzzy_match
# ---------------------------------------------------------------------------


def test_fuzzy_match_exact() -> None:
    """Exact match returns the candidate unchanged."""
    assert fuzzy_match("backend", ["backend", "frontend"]) == "backend"


def test_fuzzy_match_close() -> None:
    """A typo close enough should resolve to the right candidate."""
    assert fuzzy_match("backnd", ["backend", "frontend"]) == "backend"


def test_fuzzy_match_no_match() -> None:
    """Completely unrelated string returns None."""
    assert fuzzy_match("zzzzz", ["backend", "frontend"]) is None


def test_fuzzy_match_case_insensitive() -> None:
    """Matching should be case-insensitive."""
    assert fuzzy_match("BACKEND", ["backend", "frontend"]) == "backend"


def test_fuzzy_match_empty_value() -> None:
    """Empty value returns None."""
    assert fuzzy_match("", ["backend"]) is None


def test_fuzzy_match_empty_candidates() -> None:
    """Empty candidates list returns None."""
    assert fuzzy_match("backend", []) is None


def test_fuzzy_match_custom_threshold() -> None:
    """High threshold rejects a weak match."""
    # "bk" vs "backend" should have a low ratio
    assert fuzzy_match("bk", ["backend"], threshold=0.9) is None


# ---------------------------------------------------------------------------
# check_plan_for_typos
# ---------------------------------------------------------------------------


def test_check_plan_finds_role_typo() -> None:
    """A misspelled role should produce a suggestion."""
    plan: dict[str, object] = {
        "stages": [
            {
                "name": "Build",
                "steps": [{"goal": "Setup", "role": "backnd"}],
            }
        ]
    }
    suggestions = check_plan_for_typos(plan)
    assert len(suggestions) == 1
    assert suggestions[0].field == "role"
    assert suggestions[0].original == "backnd"
    assert suggestions[0].suggested == "backend"


def test_check_plan_finds_complexity_typo() -> None:
    """A misspelled complexity should produce a suggestion."""
    plan: dict[str, object] = {
        "stages": [
            {
                "name": "Build",
                "steps": [{"goal": "Setup", "complexity": "hgih"}],
            }
        ]
    }
    suggestions = check_plan_for_typos(plan)
    assert len(suggestions) == 1
    assert suggestions[0].field == "complexity"
    assert suggestions[0].suggested == "high"


def test_check_plan_finds_scope_typo() -> None:
    """A misspelled scope should produce a suggestion."""
    plan: dict[str, object] = {
        "stages": [
            {
                "name": "Build",
                "steps": [{"goal": "Setup", "scope": "smal"}],
            }
        ]
    }
    suggestions = check_plan_for_typos(plan)
    assert len(suggestions) == 1
    assert suggestions[0].field == "scope"
    assert suggestions[0].suggested == "small"


def test_check_plan_no_typos() -> None:
    """Valid values produce no suggestions."""
    plan: dict[str, object] = {
        "stages": [
            {
                "name": "Build",
                "steps": [
                    {
                        "goal": "Setup",
                        "role": "backend",
                        "complexity": "high",
                        "scope": "small",
                    }
                ],
            }
        ]
    }
    suggestions = check_plan_for_typos(plan)
    assert suggestions == []


def test_check_plan_empty_stages() -> None:
    """Plan with no stages returns empty list."""
    assert check_plan_for_typos({}) == []
    assert check_plan_for_typos({"stages": []}) == []


def test_check_plan_multiple_typos() -> None:
    """Multiple typos across steps are all reported."""
    plan: dict[str, object] = {
        "stages": [
            {
                "name": "Build",
                "steps": [
                    {"goal": "A", "role": "backnd", "complexity": "hgih"},
                    {"goal": "B", "role": "frotnend"},
                ],
            }
        ]
    }
    suggestions = check_plan_for_typos(plan)
    assert len(suggestions) == 3


# ---------------------------------------------------------------------------
# apply_corrections
# ---------------------------------------------------------------------------


def test_apply_corrections_fixes_values() -> None:
    """Corrections should be applied to the plan copy."""
    plan: dict[str, object] = {
        "stages": [
            {
                "name": "Build",
                "steps": [{"goal": "Setup", "role": "backnd"}],
            }
        ]
    }
    corrections = [
        CorrectionSuggestion(
            field="role",
            original="backnd",
            suggested="backend",
            confidence=0.83,
            reason="Role 'backnd' not found. Did you mean 'backend'?",
        )
    ]
    result = apply_corrections(plan, corrections)

    # Original is unchanged
    stages = plan["stages"]
    assert isinstance(stages, list)
    assert stages[0]["steps"][0]["role"] == "backnd"

    # Result is corrected
    result_stages = result["stages"]
    assert isinstance(result_stages, list)
    assert result_stages[0]["steps"][0]["role"] == "backend"


def test_apply_corrections_empty_list() -> None:
    """Empty corrections return the same dict."""
    plan: dict[str, object] = {"stages": []}
    result = apply_corrections(plan, [])
    assert result is plan


def test_apply_corrections_no_stages() -> None:
    """Plan without stages key is returned as-is (deep copy)."""
    plan: dict[str, object] = {"name": "empty"}
    corrections = [
        CorrectionSuggestion(
            field="role",
            original="x",
            suggested="backend",
            confidence=0.8,
            reason="test",
        )
    ]
    result = apply_corrections(plan, corrections)
    assert result == plan
    assert result is not plan


# ---------------------------------------------------------------------------
# format_correction_prompt
# ---------------------------------------------------------------------------


def test_format_correction_prompt_output() -> None:
    """Formatted output contains the reason and confidence."""
    suggestions = [
        CorrectionSuggestion(
            field="role",
            original="backnd",
            suggested="backend",
            confidence=0.83,
            reason="Role 'backnd' not found. Did you mean 'backend'?",
        ),
        CorrectionSuggestion(
            field="complexity",
            original="hgih",
            suggested="high",
            confidence=0.75,
            reason="Complexity 'hgih' not found. Did you mean 'high'?",
        ),
    ]
    output = format_correction_prompt(suggestions)
    assert "Role 'backnd' not found. Did you mean 'backend'?" in output
    assert "confidence: 0.83" in output
    assert "Complexity 'hgih' not found. Did you mean 'high'?" in output
    assert "confidence: 0.75" in output
    assert output.count("\n") == 1  # two lines separated by one newline


def test_format_correction_prompt_empty() -> None:
    """Empty suggestions produce empty string."""
    assert format_correction_prompt([]) == ""
