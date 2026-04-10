"""Tests for changelog_display: parsing, filtering, formatting, breaking detection."""

from __future__ import annotations

import pytest

from bernstein.cli.changelog_display import (
    ChangelogEntry,
    filter_changelog,
    format_changelog_display,
    has_breaking_changes,
    parse_changelog,
)

# ---------------------------------------------------------------------------
# Sample changelog content
# ---------------------------------------------------------------------------

_MULTI_VERSION_CHANGELOG = """\
# Changelog

## [2.0.0] - 2026-04-01
- Rewrote task server with async
- BREAKING: Removed legacy /v1 endpoints
- Added new dashboard

## [1.8.0] - 2026-03-15
- Added plan validation command
- Improved adapter auto-detection
- ⚠ Config format changed to TOML

## [1.7.0] - 2026-03-01
- Fixed race condition in merge queue
- Added cost anomaly alerts

## [1.6.3] - 2026-02-15
- Patch: fixed CLI crash on empty backlog
"""


# ---------------------------------------------------------------------------
# parse_changelog
# ---------------------------------------------------------------------------


class TestParseChangelog:
    """Tests for the CHANGELOG.md parser."""

    def test_multi_version_markdown(self) -> None:
        entries = parse_changelog(_MULTI_VERSION_CHANGELOG)
        assert len(entries) == 4

        # First entry (newest)
        assert entries[0].version == "2.0.0"
        assert entries[0].date == "2026-04-01"
        assert "Rewrote task server with async" in entries[0].changes
        assert "Added new dashboard" in entries[0].changes
        assert "Removed legacy /v1 endpoints" in entries[0].breaking
        # The BREAKING line should not appear in changes
        assert all("BREAKING" not in c for c in entries[0].changes)

        # Second entry — check warning-prefix breaking change
        assert entries[1].version == "1.8.0"
        assert entries[1].date == "2026-03-15"
        assert "Config format changed to TOML" in entries[1].breaking

        # Third entry — no breaking changes
        assert entries[2].version == "1.7.0"
        assert entries[2].breaking == []
        assert len(entries[2].changes) == 2

        # Fourth entry
        assert entries[3].version == "1.6.3"
        assert entries[3].changes == ["Patch: fixed CLI crash on empty backlog"]

    def test_empty_content(self) -> None:
        entries = parse_changelog("")
        assert entries == []

    def test_no_version_headers(self) -> None:
        entries = parse_changelog("Just some random text\n- a bullet\n")
        assert entries == []

    def test_version_with_no_changes(self) -> None:
        content = "## [3.0.0] - 2026-05-01\n\n## [2.9.0] - 2026-04-28\n- One change\n"
        entries = parse_changelog(content)
        assert len(entries) == 2
        assert entries[0].version == "3.0.0"
        assert entries[0].changes == []
        assert entries[0].breaking == []
        assert entries[1].changes == ["One change"]

    def test_asterisk_bullets(self) -> None:
        content = "## [1.0.0] - 2026-01-01\n* Star bullet\n* BREAKING: Star breaking\n"
        entries = parse_changelog(content)
        assert len(entries) == 1
        assert entries[0].changes == ["Star bullet"]
        assert entries[0].breaking == ["Star breaking"]


# ---------------------------------------------------------------------------
# filter_changelog
# ---------------------------------------------------------------------------


class TestFilterChangelog:
    """Tests for version-range filtering."""

    def test_filter_between_versions(self) -> None:
        entries = parse_changelog(_MULTI_VERSION_CHANGELOG)
        # from_version=1.6.3 (exclusive), to_version=1.8.0 (inclusive)
        filtered = filter_changelog(entries, "1.6.3", "1.8.0")
        versions = [e.version for e in filtered]
        assert "1.7.0" in versions
        assert "1.8.0" in versions
        assert "1.6.3" not in versions
        assert "2.0.0" not in versions

    def test_filter_single_version(self) -> None:
        entries = parse_changelog(_MULTI_VERSION_CHANGELOG)
        filtered = filter_changelog(entries, "1.7.0", "1.8.0")
        assert len(filtered) == 1
        assert filtered[0].version == "1.8.0"

    def test_filter_no_match(self) -> None:
        entries = parse_changelog(_MULTI_VERSION_CHANGELOG)
        filtered = filter_changelog(entries, "2.0.0", "2.0.0")
        assert filtered == []

    def test_filter_empty_entries(self) -> None:
        filtered = filter_changelog([], "1.0.0", "2.0.0")
        assert filtered == []

    def test_filter_all_versions(self) -> None:
        entries = parse_changelog(_MULTI_VERSION_CHANGELOG)
        filtered = filter_changelog(entries, "0.0.0", "99.0.0")
        assert len(filtered) == 4


# ---------------------------------------------------------------------------
# format_changelog_display
# ---------------------------------------------------------------------------


class TestFormatChangelogDisplay:
    """Tests for Rich-formatted output."""

    def test_produces_readable_output(self) -> None:
        entries = parse_changelog(_MULTI_VERSION_CHANGELOG)
        filtered = filter_changelog(entries, "1.6.3", "1.8.0")
        output = format_changelog_display(filtered)

        # Should contain version headers
        assert "v1.7.0" in output
        assert "v1.8.0" in output

        # Should contain change text
        assert "Fixed race condition in merge queue" in output
        assert "Added plan validation command" in output

        # Should highlight breaking changes
        assert "BREAKING:" in output
        assert "Config format changed to TOML" in output

    def test_empty_entries(self) -> None:
        output = format_changelog_display([])
        assert "No changelog entries" in output

    def test_entry_without_breaking(self) -> None:
        entry = ChangelogEntry(
            version="1.0.0",
            date="2026-01-01",
            changes=["Added feature X"],
            breaking=[],
        )
        output = format_changelog_display([entry])
        assert "v1.0.0" in output
        assert "Added feature X" in output
        assert "BREAKING" not in output


# ---------------------------------------------------------------------------
# has_breaking_changes
# ---------------------------------------------------------------------------


class TestHasBreakingChanges:
    """Tests for breaking-change detection."""

    def test_detects_breaking_entries(self) -> None:
        entries = parse_changelog(_MULTI_VERSION_CHANGELOG)
        # 2.0.0 and 1.8.0 have breaking changes
        assert has_breaking_changes(entries) is True

    def test_no_breaking_changes(self) -> None:
        entries = [
            ChangelogEntry(version="1.0.0", date="2026-01-01", changes=["Safe change"], breaking=[]),
            ChangelogEntry(version="0.9.0", date="2025-12-01", changes=["Another safe change"], breaking=[]),
        ]
        assert has_breaking_changes(entries) is False

    def test_empty_list(self) -> None:
        assert has_breaking_changes([]) is False

    def test_only_breaking(self) -> None:
        entries = [
            ChangelogEntry(version="5.0.0", date="2026-06-01", changes=[], breaking=["Everything changed"]),
        ]
        assert has_breaking_changes(entries) is True
