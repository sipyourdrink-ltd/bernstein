"""Tests for incident signal loading and fingerprint extraction."""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.replay.diagnose import DiagnoseError
from bernstein.core.replay.diagnose_signals import (
    SIGNAL_KIND_INCIDENT,
    _fingerprint_lines,
    incident_signal,
    resolve_signal,
)


class TestFingerprintLines:
    """Tests for the internal _fingerprint_lines helper."""

    def test_extracts_error_lines_after_marker(self) -> None:
        """Only lines following an error marker are considered."""
        prompt = """Some context
Last error (trimmed):
- Error line one
- Error line two
More context"""
        result = _fingerprint_lines(prompt)
        assert "Error line one" in result
        assert "Error line two" in result

    def test_strips_bullet_prefixes(self) -> None:
        """Leading '- ' bullets are removed."""
        prompt = """Last error (trimmed):
- First error line
- Second error line"""
        result = _fingerprint_lines(prompt)
        assert result == ("First error line", "Second error line")

    def test_strips_trailing_ellipsis(self) -> None:
        """Trailing '...' is stripped from lines."""
        prompt = """Last error (trimmed):
- Error with truncation...
- Another error line..."""
        result = _fingerprint_lines(prompt)
        assert result == ("Another error line", "Error with truncation")

    def test_drops_lines_shorter_than_minimum(self) -> None:
        """Lines shorter than 12 chars are dropped."""
        prompt = """Last error (trimmed):
- Short
- This line is long enough to keep"""
        result = _fingerprint_lines(prompt)
        assert result == ("This line is long enough to keep",)

    def test_skips_pre_error_lines(self) -> None:
        """Lines before the error marker are ignored."""
        prompt = """Some preamble
More preamble
Last error (trimmed):
- Actual error line here"""
        result = _fingerprint_lines(prompt)
        assert result == ("Actual error line here",)

    def test_both_error_markers_work(self) -> None:
        """Both 'Last error (trimmed):' and 'Representative error snippets:' work."""
        prompt1 = """Last error (trimmed):
- Error from first marker line"""
        prompt2 = """Representative error snippets:
- Error from second marker line"""
        assert _fingerprint_lines(prompt1) == ("Error from first marker line",)
        assert _fingerprint_lines(prompt2) == ("Error from second marker line",)

    def test_returns_sorted_deduped(self) -> None:
        """Results are sorted and deduplicated."""
        prompt = """Last error (trimmed):
- Zebra error line
- Alpha error line
- Zebra error line"""
        result = _fingerprint_lines(prompt)
        assert result == ("Alpha error line", "Zebra error line")

    def test_empty_when_no_marker(self) -> None:
        """Empty result when no error marker present."""
        prompt = """Just some text
With no error marker"""
        assert _fingerprint_lines(prompt) == ()

    def test_empty_when_no_lines_after_marker(self) -> None:
        """Empty result when marker exists but no valid lines follow."""
        prompt = """Last error (trimmed):
- Short"""
        assert _fingerprint_lines(prompt) == ()


class TestIncidentSignal:
    """Tests for incident_signal() - loads YAML case and returns SignalPredicate."""

    def test_happy_path_loads_real_case(self, tmp_path: Path) -> None:
        """inc-f31dce50e73b.yaml has a 93-char error line that gets extracted."""
        # Copy the real case file to tmp_path for isolation
        case_file = tmp_path / "inc-f31dce50e73b.yaml"
        case_file.write_text(
            """id: inc-f31dce50e73b
severity: P2
source_incident: "dlq:9316bdb174f045b5"
owner: backend
created_at: 1788109460.768
expected_outcome: "Agent should complete the task; flake-tolerant retry is acceptable. (root cause: max_retries_exceeded)"
tags:
  - max_retries_exceeded
prompt: |
  Reproduce and resolve the following terminal failure (role=backend).
  Task: Implement PaperQA2 synthesiser adapter and corpus identity binding
  Failure reason: max_retries_exceeded
  Last error (trimmed):
  Agent backend-1ce0c6f4 died; janitor failed: ['path_exists: src/bernstein/core/orchestration/pqa_synthesiser.py (not found)']
""",
            encoding="utf-8",
        )

        predicate = incident_signal("inc-f31dce50e73b", cases_dir=tmp_path)

        assert predicate.predicate_id == "incident/v1"
        assert predicate.params["kind"] == SIGNAL_KIND_INCIDENT
        assert predicate.params["case_id"] == "inc-f31dce50e73b"
        # The error line is 93 chars - should be extracted
        needles = predicate.needles
        assert len(needles) == 1
        assert "Agent backend-1ce0c6f4 died; janitor failed" in needles[0]
        assert len(needles[0]) >= 93

    def test_unsafe_case_id_raises(self, tmp_path: Path) -> None:
        """Unsafe case IDs (path traversal, special chars) are rejected."""
        with pytest.raises(DiagnoseError, match="unsafe incident case id"):
            incident_signal("../etc/passwd", cases_dir=tmp_path)

        with pytest.raises(DiagnoseError, match="unsafe incident case id"):
            incident_signal("case;rm -rf /", cases_dir=tmp_path)

        with pytest.raises(DiagnoseError, match="unsafe incident case id"):
            incident_signal("case with spaces", cases_dir=tmp_path)

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        """Missing case file raises DiagnoseError."""
        with pytest.raises(DiagnoseError, match="no incident eval case"):
            incident_signal("nonexistent", cases_dir=tmp_path)

    def test_yaml_not_a_mapping_raises(self, tmp_path: Path) -> None:
        """YAML that isn't a mapping raises DiagnoseError."""
        case_file = tmp_path / "bad.yaml"
        case_file.write_text("- item1\n- item2\n", encoding="utf-8")

        with pytest.raises(DiagnoseError, match="is not a mapping"):
            incident_signal("bad", cases_dir=tmp_path)

    def test_no_fingerprint_lines_raises(self, tmp_path: Path) -> None:
        """Case with no valid fingerprint lines raises DiagnoseError."""
        case_file = tmp_path / "no_fingerprint.yaml"
        case_file.write_text(
            """id: no_fingerprint
prompt: |
  Some text
  Last error (trimmed):
  - Short
  - Also short
""",
            encoding="utf-8",
        )

        with pytest.raises(DiagnoseError, match="carries no matchable failure fingerprint"):
            incident_signal("no_fingerprint", cases_dir=tmp_path)

    def test_yaml_parse_error_raises(self, tmp_path: Path) -> None:
        """Malformed YAML raises DiagnoseError."""
        case_file = tmp_path / "malformed.yaml"
        case_file.write_text("{ invalid yaml: ", encoding="utf-8")

        with pytest.raises(DiagnoseError, match="cannot read incident case"):
            incident_signal("malformed", cases_dir=tmp_path)


class TestResolveSignalIncident:
    """Tests for resolve_signal() with incident: dispatch."""

    def test_resolves_incident_signal(self, tmp_path: Path) -> None:
        """resolve_signal('incident:CASE_ID') delegates to incident_signal()."""
        cases_dir = tmp_path / "src" / "bernstein" / "eval" / "cases" / "incidents"
        cases_dir.mkdir(parents=True)
        case_file = cases_dir / "test-case.yaml"
        case_file.write_text(
            """id: test-case
prompt: |
  Last error (trimmed):
  - This is a long enough error line for extraction
""",
            encoding="utf-8",
        )

        predicate = resolve_signal("incident:test-case", sdd_dir=tmp_path, workdir=tmp_path)

        assert predicate.predicate_id == "incident/v1"
        assert predicate.params["case_id"] == "test-case"
        assert "This is a long enough error line" in predicate.needles[0]

    def test_missing_case_id_raises(self, tmp_path: Path) -> None:
        """incident: without a case ID raises DiagnoseError."""
        with pytest.raises(DiagnoseError, match="incident requires a case id"):
            resolve_signal("incident:", sdd_dir=tmp_path, workdir=tmp_path)

    def test_unknown_signal_kind_raises(self, tmp_path: Path) -> None:
        """Unknown signal kind raises DiagnoseError."""
        with pytest.raises(DiagnoseError, match="unknown --signal"):
            resolve_signal("unknown:foo", sdd_dir=tmp_path, workdir=tmp_path)
