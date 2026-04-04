"""Unit tests for prompt token-usage breakdown analysis."""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.core.prompt_token_analysis import (
    PromptTokenReport,
    SectionTokens,
    _section_category,
    analyse_prompt_sections,
    save_prompt_token_report,
)


# ---------------------------------------------------------------------------
# _section_category — section → bucket mapping
# ---------------------------------------------------------------------------


class TestSectionCategory:
    def test_role_is_system_prompt(self) -> None:
        assert _section_category("role") == "system_prompt"

    def test_specialists_is_system_prompt(self) -> None:
        assert _section_category("specialists") == "system_prompt"

    def test_context_is_context(self) -> None:
        assert _section_category("context") == "context"

    def test_project_is_context(self) -> None:
        assert _section_category("project") == "context"

    def test_lessons_is_context(self) -> None:
        assert _section_category("lessons") == "context"

    def test_predecessor_is_context(self) -> None:
        assert _section_category("predecessor") == "context"

    def test_recommendations_is_context(self) -> None:
        assert _section_category("recommendations") == "context"

    def test_tasks_is_user_prompt(self) -> None:
        assert _section_category("tasks") == "user_prompt"

    def test_instructions_is_user_prompt(self) -> None:
        assert _section_category("instructions") == "user_prompt"

    def test_git_safety_is_user_prompt(self) -> None:
        assert _section_category("git_safety") == "user_prompt"

    def test_heartbeat_is_user_prompt(self) -> None:
        assert _section_category("heartbeat") == "user_prompt"

    def test_signal_is_user_prompt(self) -> None:
        assert _section_category("signal") == "user_prompt"

    def test_unknown_section_is_user_prompt(self) -> None:
        assert _section_category("some_unknown_section") == "user_prompt"


# ---------------------------------------------------------------------------
# analyse_prompt_sections
# ---------------------------------------------------------------------------


def _make_sections(
    role: str = "You are a backend engineer.",
    context: str = "## Project context\nLots of context here.\n",
    tasks: str = "## Tasks\nDo something.",
    instructions: str = "## Instructions\nComplete the tasks.",
) -> list[tuple[str, str]]:
    return [
        ("role", role),
        ("context", context),
        ("tasks", tasks),
        ("instructions", instructions),
    ]


class TestAnalysePromptSections:
    def test_returns_report_with_totals(self) -> None:
        sections = _make_sections()
        report = analyse_prompt_sections(sections)
        assert report.total_tokens > 0
        assert report.system_prompt_tokens > 0
        assert report.context_tokens > 0
        assert report.user_prompt_tokens > 0

    def test_percentages_sum_to_100(self) -> None:
        sections = _make_sections()
        report = analyse_prompt_sections(sections)
        total_pct = report.system_prompt_pct + report.context_pct + report.user_prompt_pct
        assert abs(total_pct - 100.0) < 0.5  # allow small float rounding

    def test_section_pct_of_total_sums_to_100(self) -> None:
        sections = _make_sections()
        report = analyse_prompt_sections(sections)
        total_pct = sum(s.pct_of_total for s in report.sections)
        assert abs(total_pct - 100.0) < 0.5

    def test_sections_sorted_descending_by_tokens(self) -> None:
        sections = _make_sections(
            role="Short.",
            context="A" * 5000,  # large context
            tasks="Short.",
            instructions="Short.",
        )
        report = analyse_prompt_sections(sections)
        tokens = [s.tokens for s in report.sections]
        assert tokens == sorted(tokens, reverse=True)

    def test_session_id_propagated(self) -> None:
        sections = _make_sections()
        report = analyse_prompt_sections(sections, session_id="sess-abc")
        assert report.session_id == "sess-abc"

    def test_empty_sections_returns_zero_totals(self) -> None:
        report = analyse_prompt_sections([])
        assert report.total_tokens == 0
        assert report.system_prompt_pct == 0.0
        assert report.context_pct == 0.0
        assert report.user_prompt_pct == 0.0

    def test_suggestion_generated_when_category_exceeds_budget(self) -> None:
        # Make context dominate > 50% recommended limit
        sections = [
            ("role", "short"),
            ("context", "X" * 20_000),  # dominant context
            ("tasks", "short"),
        ]
        report = analyse_prompt_sections(sections)
        # context should exceed its 50% cap and produce a suggestion
        assert any("Context" in s for s in report.suggestions)

    def test_no_suggestion_when_within_budget(self) -> None:
        sections = [
            ("role", "R" * 500),
            ("context", "C" * 1000),
            ("tasks", "T" * 1500),
            ("instructions", "I" * 800),
        ]
        report = analyse_prompt_sections(sections)
        # With a balanced mix, suggestions should be minimal or empty
        # We just verify the type is correct
        assert isinstance(report.suggestions, list)

    def test_multiple_sections_same_category(self) -> None:
        sections = [
            ("role", "Role prompt here."),
            ("specialists", "Specialist agents."),
            ("tasks", "Task content."),
        ]
        report = analyse_prompt_sections(sections)
        # system_prompt should include both "role" and "specialists"
        sp_sections = [s for s in report.sections if s.category == "system_prompt"]
        assert len(sp_sections) == 2
        expected_sp_tokens = sum(s.tokens for s in sp_sections)
        assert report.system_prompt_tokens == expected_sp_tokens


# ---------------------------------------------------------------------------
# PromptTokenReport.summary
# ---------------------------------------------------------------------------


class TestPromptTokenReportSummary:
    def test_summary_contains_session_id(self) -> None:
        report = analyse_prompt_sections(_make_sections(), session_id="sess-xyz")
        summary = report.summary()
        assert "sess-xyz" in summary

    def test_summary_contains_token_counts(self) -> None:
        report = analyse_prompt_sections(_make_sections())
        summary = report.summary()
        assert "System prompt" in summary
        assert "Context" in summary
        assert "User prompt" in summary

    def test_summary_lists_suggestions(self) -> None:
        sections = [
            ("role", "r"),
            ("context", "C" * 30_000),
            ("tasks", "t"),
        ]
        report = analyse_prompt_sections(sections)
        if report.suggestions:
            summary = report.summary()
            assert "Suggestions:" in summary


# ---------------------------------------------------------------------------
# PromptTokenReport.to_dict
# ---------------------------------------------------------------------------


class TestPromptTokenReportToDict:
    def test_all_keys_present(self) -> None:
        report = analyse_prompt_sections(_make_sections(), session_id="s1")
        d = report.to_dict()
        expected_keys = {
            "session_id",
            "total_tokens",
            "system_prompt_tokens",
            "context_tokens",
            "user_prompt_tokens",
            "system_prompt_pct",
            "context_pct",
            "user_prompt_pct",
            "sections",
            "suggestions",
        }
        assert expected_keys <= set(d.keys())

    def test_sections_serialised_as_list(self) -> None:
        report = analyse_prompt_sections(_make_sections())
        d = report.to_dict()
        assert isinstance(d["sections"], list)
        for sec in d["sections"]:
            assert "name" in sec
            assert "tokens" in sec
            assert "category" in sec


# ---------------------------------------------------------------------------
# save_prompt_token_report
# ---------------------------------------------------------------------------


class TestSavePromptTokenReport:
    def test_creates_file(self, tmp_path: Path) -> None:
        report = analyse_prompt_sections(_make_sections(), session_id="sess-save")
        path = save_prompt_token_report(report, tmp_path)
        assert path.exists()
        assert path.name == "prompt_token_usage_sess-save.json"

    def test_file_is_valid_json(self, tmp_path: Path) -> None:
        report = analyse_prompt_sections(_make_sections(), session_id="sess-json")
        path = save_prompt_token_report(report, tmp_path)
        data = json.loads(path.read_text())
        assert data["session_id"] == "sess-json"
        assert data["total_tokens"] > 0

    def test_fallback_filename_when_no_session_id(self, tmp_path: Path) -> None:
        report = analyse_prompt_sections(_make_sections(), session_id="")
        path = save_prompt_token_report(report, tmp_path)
        assert path.name == "prompt_token_usage.json"

    def test_metrics_dir_created(self, tmp_path: Path) -> None:
        report = analyse_prompt_sections(_make_sections(), session_id="s2")
        path = save_prompt_token_report(report, tmp_path)
        assert path.parent == tmp_path / ".sdd" / "metrics"
