"""Tests for the contextual help system with inline doc links."""

from __future__ import annotations

from bernstein.cli.contextual_help import (
    HELP_LINKS,
    HelpLink,
    enrich_error_message,
    find_help_link,
    format_help_suggestion,
)


class TestFindHelpLink:
    """Tests for find_help_link pattern matching."""

    def test_matches_rate_limit(self) -> None:
        link = find_help_link("API rate_limit exceeded")
        assert link is not None
        assert link.doc_section == "troubleshooting#rate-limits"

    def test_matches_spawn_fail(self) -> None:
        link = find_help_link("spawn process failed for agent-3")
        assert link is not None
        assert link.doc_section == "troubleshooting#spawn-failures"

    def test_matches_budget_exceed(self) -> None:
        link = find_help_link("budget exceeded: $12.50 over limit")
        assert link is not None
        assert link.doc_section == "cost-optimization#budgets"

    def test_matches_merge_conflict(self) -> None:
        link = find_help_link("merge conflict in src/main.py")
        assert link is not None
        assert link.doc_section == "troubleshooting#merge-conflicts"

    def test_matches_worktree_lock(self) -> None:
        link = find_help_link("worktree lock held by pid 1234")
        assert link is not None
        assert link.doc_section == "troubleshooting#git-locks"

    def test_matches_adapter_not_found(self) -> None:
        link = find_help_link("adapter not found: codex")
        assert link is not None
        assert link.doc_section == "adapter-guide"

    def test_matches_permission_denied(self) -> None:
        link = find_help_link("permission denied for /etc/shadow")
        assert link is not None
        assert link.doc_section == "security-hardening"

    def test_matches_timeout(self) -> None:
        link = find_help_link("operation timeout after 30s")
        assert link is not None
        assert link.doc_section == "performance-tuning#timeouts"

    def test_case_insensitive(self) -> None:
        link = find_help_link("RATE_LIMIT hit on endpoint")
        assert link is not None
        assert link.doc_section == "troubleshooting#rate-limits"

    def test_no_match_returns_none(self) -> None:
        link = find_help_link("everything is fine, no errors here")
        assert link is None

    def test_no_match_empty_string(self) -> None:
        link = find_help_link("")
        assert link is None


class TestFormatHelpSuggestion:
    """Tests for format_help_suggestion output."""

    def test_format_includes_url(self) -> None:
        link = HelpLink(
            error_pattern=r"test",
            doc_section="troubleshooting#test",
            url="https://bernstein.readthedocs.io/en/latest/troubleshooting#test",
            summary="Test docs",
        )
        result = format_help_suggestion(link)
        assert result == "See: https://bernstein.readthedocs.io/en/latest/troubleshooting#test"

    def test_format_starts_with_see(self) -> None:
        link = HELP_LINKS[0]
        result = format_help_suggestion(link)
        assert result.startswith("See: ")

    def test_format_contains_base_url(self) -> None:
        link = HELP_LINKS[0]
        result = format_help_suggestion(link)
        url_part = result.removeprefix("See: ")
        assert url_part.startswith("https://bernstein.readthedocs.io")


class TestEnrichErrorMessage:
    """Tests for enrich_error_message with and without matches."""

    def test_enriches_matching_error(self) -> None:
        error = "API rate_limit exceeded"
        result = enrich_error_message(error)
        assert error in result
        assert "See: " in result
        assert "troubleshooting#rate-limits" in result

    def test_enriched_appends_on_new_line(self) -> None:
        error = "budget exceeded for project X"
        result = enrich_error_message(error)
        lines = result.split("\n")
        assert len(lines) == 2
        assert lines[0] == error
        assert lines[1].startswith("See: ")

    def test_no_match_returns_original(self) -> None:
        error = "all good, nothing to see"
        result = enrich_error_message(error)
        assert result == error

    def test_no_match_no_extra_newline(self) -> None:
        error = "just a normal message"
        result = enrich_error_message(error)
        assert "\n" not in result


class TestHelpLinksIntegrity:
    """Sanity checks on the HELP_LINKS registry."""

    def test_all_links_are_frozen_dataclasses(self) -> None:
        for link in HELP_LINKS:
            assert isinstance(link, HelpLink)

    def test_all_urls_start_with_base(self) -> None:
        for link in HELP_LINKS:
            assert link.url.startswith("https://bernstein.readthedocs.io/en/latest")

    def test_all_patterns_compile(self) -> None:
        import re

        for link in HELP_LINKS:
            # Should not raise
            re.compile(link.error_pattern)

    def test_no_empty_summaries(self) -> None:
        for link in HELP_LINKS:
            assert link.summary.strip()
