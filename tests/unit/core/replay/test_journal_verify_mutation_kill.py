"""Mutation-killing tests for journal verification (issue #3654).

The journal verifier's job is to return failure when its evidence is
absent, unreadable, or re-signable. These tests exercise the negative
controls - each mutation class the verifier claims to catch - and assert
the verifier rejects each with the expected failure, plus the control:
the unmutated journal passes.

Every test here exists to kill a specific class of mutation survivor
identified by mutmut_critical.py. The tests assert on the *precise*
rejection reason, not just "raises" - a verifier that rejects everything
would pass a lazy version of this suite.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.replay.journal import (
    EventJournal,
    JournalCoverageStatus,
    JournalIdentityStatus,
    JournalParseError,
    JournalSeal,
    load_events,
    verify_events,
    verify_journal,
)


def _journal(tmp_path: Path, count: int = 4) -> tuple[Path, JournalSeal]:
    """Create a journal with *count* events and return its path + seal."""
    journal = EventJournal("verify-matrix", tmp_path / ".sdd")
    for index in range(count):
        journal.record("step", value=index)
    return journal.path, JournalSeal(head=journal.head(), event_count=journal.event_count())


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def _write(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Count field survivors: lines 258, 944, 959, 1126
# ---------------------------------------------------------------------------


class TestVerifyResultCountField:
    """Kill mutations that zero out the ``count`` field in verify results.

    Issue #3651: ``verify_journal`` returned ``ok=True`` with ``count``
    silently dropping from 4 to 3. A mutant that hardcodes ``count = 0``
    would survive if nothing asserts on the field.
    """

    def test_verify_result_count_matches_parsed_event_count(self, tmp_path: Path) -> None:
        """The ``count`` field must equal the number of parsed events."""
        path, seal = _journal(tmp_path, count=4)

        result = verify_journal(path, seal=seal)

        assert result.count == 4
        assert result.chain_consistent
        assert result.identity == JournalIdentityStatus.VERIFIED

    def test_verify_result_count_reflects_partial_journal(self, tmp_path: Path) -> None:
        """When the journal is truncated, ``count`` must reflect the actual parsed rows."""
        path, _ = _journal(tmp_path, count=4)
        _write(path, _lines(path)[:2])  # Keep only first 2 events

        result = verify_journal(path)

        assert result.count == 2, "count must reflect the truncated journal"
        assert result.chain_consistent

    def test_verify_events_count_matches_input_length(self, tmp_path: Path) -> None:
        """verify_events must report the exact count of input rows."""
        path, _ = _journal(tmp_path, count=3)
        events = load_events(path).events

        result = verify_events(events)

        assert result.count == 3
        assert result.count == len(events)

    def test_empty_journal_reports_zero_count(self, tmp_path: Path) -> None:
        """An empty journal must have count=0, not a defaulted-but-wrong value."""
        path = tmp_path / ".sdd" / "runs" / "empty" / "journal.jsonl"
        path.parent.mkdir(parents=True)
        path.touch()

        result = verify_journal(path)

        assert result.count == 0
        assert result.chain_consistent
        assert result.head == ""

    def test_seal_mismatch_error_mentions_exact_counts(self, tmp_path: Path) -> None:
        """When count mismatches a seal, the error must name both counts."""
        path, _ = _journal(tmp_path, count=4)
        _write(path, _lines(path)[:2])  # Truncate to 2 events
        seal = JournalSeal(head="fakehead", event_count=4)

        result = verify_journal(path, seal=seal)

        assert result.count == 2
        assert result.identity == JournalIdentityStatus.MISMATCHED
        error_text = " ".join(result.errors)
        assert "2 events" in error_text
        assert "4" in error_text


# ---------------------------------------------------------------------------
# Strict validation survivors: lines 1013-1023
# ---------------------------------------------------------------------------


class TestStrictRowValidation:
    """Kill mutations in _validate_strict_row that would let malformed rows pass.

    A tolerant reader skips bad rows; a strict reader must refuse them by
    naming the physical line. Mutations like "or" → "and" in the guard
    clauses would let incomplete rows through.
    """

    def test_strict_mode_rejects_row_with_empty_event(self, tmp_path: Path) -> None:
        """A row with event="" must be rejected in strict mode."""
        path, _ = _journal(tmp_path, count=1)
        lines = _lines(path)
        row = json.loads(lines[0])
        row["event"] = ""
        lines[0] = json.dumps(row)
        _write(path, lines)

        with pytest.raises(JournalParseError, match=r"non-string 'event'"):
            load_events(path, strict=True)

    def test_strict_mode_rejects_row_with_missing_event(self, tmp_path: Path) -> None:
        """A row missing 'event' must be rejected in strict mode."""
        path, _ = _journal(tmp_path, count=1)
        lines = _lines(path)
        row = json.loads(lines[0])
        del row["event"]
        lines[0] = json.dumps(row)
        _write(path, lines)

        with pytest.raises(JournalParseError, match=r"missing.*'event'"):
            load_events(path, strict=True)

    def test_strict_mode_rejects_row_with_non_integer_index(self, tmp_path: Path) -> None:
        """A row with index="0" must be rejected in strict mode."""
        path, _ = _journal(tmp_path, count=1)
        lines = _lines(path)
        row = json.loads(lines[0])
        row["index"] = "0"
        lines[0] = json.dumps(row)
        _write(path, lines)

        with pytest.raises(JournalParseError, match=r"non-integer 'index'"):
            load_events(path, strict=True)

    def test_strict_mode_rejects_row_with_boolean_as_index(self, tmp_path: Path) -> None:
        """A row with index=True must be rejected (bool is int subclass in Python)."""
        path, _ = _journal(tmp_path, count=1)
        lines = _lines(path)
        row = json.loads(lines[0])
        row["index"] = True
        lines[0] = json.dumps(row)
        _write(path, lines)

        with pytest.raises(JournalParseError, match=r"non-integer 'index'"):
            load_events(path, strict=True)

    def test_strict_mode_rejects_row_with_missing_prev_hash(self, tmp_path: Path) -> None:
        """A row missing 'prev_hash' must be rejected in strict mode."""
        path, _ = _journal(tmp_path, count=1)
        lines = _lines(path)
        row = json.loads(lines[0])
        del row["prev_hash"]
        lines[0] = json.dumps(row)
        _write(path, lines)

        with pytest.raises(JournalParseError, match=r"missing.*'prev_hash'"):
            load_events(path, strict=True)

    def test_strict_mode_rejects_row_with_non_string_prev_hash(self, tmp_path: Path) -> None:
        """A row with prev_hash=123 must be rejected in strict mode."""
        path, _ = _journal(tmp_path, count=1)
        lines = _lines(path)
        row = json.loads(lines[0])
        row["prev_hash"] = 123
        lines[0] = json.dumps(row)
        _write(path, lines)

        with pytest.raises(JournalParseError, match=r"non-string 'prev_hash'"):
            load_events(path, strict=True)

    def test_strict_mode_rejects_row_with_empty_payload_hash(self, tmp_path: Path) -> None:
        """A row with payload_hash="" must be rejected in strict mode."""
        path, _ = _journal(tmp_path, count=1)
        lines = _lines(path)
        row = json.loads(lines[0])
        row["payload_hash"] = ""
        lines[0] = json.dumps(row)
        _write(path, lines)

        with pytest.raises(JournalParseError, match=r"missing or empty.*payload_hash"):
            load_events(path, strict=True)

    def test_strict_mode_rejects_row_with_missing_event_hash(self, tmp_path: Path) -> None:
        """A row missing 'event_hash' must be rejected in strict mode."""
        path, _ = _journal(tmp_path, count=1)
        lines = _lines(path)
        row = json.loads(lines[0])
        del row["event_hash"]
        lines[0] = json.dumps(row)
        _write(path, lines)

        with pytest.raises(JournalParseError, match=r"missing.*event_hash"):
            load_events(path, strict=True)

    def test_tolerant_mode_survives_all_strict_failures(self, tmp_path: Path) -> None:
        """The tolerant reader must skip rows the strict reader rejects."""
        path, _ = _journal(tmp_path, count=3)
        lines = _lines(path)

        # Make a truly unparsable row (not just missing a field)
        lines.insert(1, "not json at all")
        _write(path, lines)

        loaded = load_events(path, strict=False)

        assert len(loaded.events) == 3  # All 3 real events survived
        assert loaded.discarded_line_indices == (1,)  # The junk line was discarded


# ---------------------------------------------------------------------------
# Chain consistency survivors
# ---------------------------------------------------------------------------


class TestChainConsistencyDiscrimination:
    """Kill mutations that would let chain breaks pass as consistent.

    verify_journal has separate verdicts for chain_consistent, coverage,
    and identity. Mutations in the chain walker must be caught by tests
    asserting on all three dimensions.
    """

    def test_verify_distinguishes_chain_break_from_coverage_failure(self, tmp_path: Path) -> None:
        """A corrupted middle row must report chain_consistent=False, not just coverage failure."""
        path, _ = _journal(tmp_path, count=3)
        lines = _lines(path)
        row = json.loads(lines[1])
        row["value"] = 999  # Change payload → hash mismatch
        lines[1] = json.dumps(row)
        _write(path, lines)

        result = verify_journal(path)

        assert not result.chain_consistent, "chain is broken, not just incomplete"
        assert result.divergent_index == 1
        assert "event_hash mismatch" in result.errors[0]

    def test_verify_reports_prev_hash_break_separately(self, tmp_path: Path) -> None:
        """A broken prev_hash link must be diagnosed distinctly from event_hash."""
        path, _ = _journal(tmp_path, count=3)
        lines = _lines(path)
        row = json.loads(lines[1])
        row["prev_hash"] = "0" * 64  # Break the link
        lines[1] = json.dumps(row)
        _write(path, lines)

        result = verify_journal(path)

        assert not result.chain_consistent
        assert result.divergent_index == 1
        assert "prev_hash break" in result.errors[0]

    def test_verify_reports_head_of_verified_prefix_on_divergence(self, tmp_path: Path) -> None:
        """When the chain breaks, head must be the last verified hash, not empty."""
        path, _ = _journal(tmp_path, count=3)
        lines = _lines(path)
        first_hash = json.loads(lines[0])["event_hash"]

        # Corrupt second row
        row = json.loads(lines[1])
        row["value"] = 999
        lines[1] = json.dumps(row)
        _write(path, lines)

        result = verify_journal(path)

        assert not result.chain_consistent
        assert result.head == first_hash, "head must be the last verified hash"
        assert result.divergent_index == 1


# ---------------------------------------------------------------------------
# Identity status survivors
# ---------------------------------------------------------------------------


class TestIdentityStatusDiscrimination:
    """Kill mutations in identity checks that would misreport verification status.

    Identity has three states: VERIFIED, MISMATCHED, UNVERIFIABLE. The
    logic combining chain_consistent + coverage + seal must distinguish
    all three.
    """

    def test_unverifiable_when_no_seal_provided(self, tmp_path: Path) -> None:
        """A consistent chain with no seal must report UNVERIFIABLE, not VERIFIED."""
        path, _ = _journal(tmp_path, count=2)

        result = verify_journal(path, seal=None)

        assert result.chain_consistent
        assert result.coverage == JournalCoverageStatus.COMPLETE
        assert result.identity == JournalIdentityStatus.UNVERIFIABLE

    def test_mismatched_when_head_differs_from_seal(self, tmp_path: Path) -> None:
        """A consistent chain with wrong head must report MISMATCHED."""
        path, seal = _journal(tmp_path, count=2)
        wrong_seal = JournalSeal(head="wrong" + seal.head[5:], event_count=seal.event_count)

        result = verify_journal(path, seal=wrong_seal)

        assert result.chain_consistent
        assert result.coverage == JournalCoverageStatus.COMPLETE
        assert result.identity == JournalIdentityStatus.MISMATCHED
        assert "does not match sealed head" in " ".join(result.errors)

    def test_mismatched_when_count_differs_from_seal(self, tmp_path: Path) -> None:
        """A consistent chain with wrong count must report MISMATCHED."""
        path, _ = _journal(tmp_path, count=2)
        wrong_seal = JournalSeal(head="anyhash", event_count=5)

        result = verify_journal(path, seal=wrong_seal)

        assert result.identity == JournalIdentityStatus.MISMATCHED
        assert "2 events" in " ".join(result.errors)
        assert "5" in " ".join(result.errors)

    def test_verified_requires_all_three_checks(self, tmp_path: Path) -> None:
        """VERIFIED requires chain_consistent AND complete coverage AND matching seal."""
        path, seal = _journal(tmp_path, count=2)

        result = verify_journal(path, seal=seal)

        assert result.chain_consistent
        assert result.coverage == JournalCoverageStatus.COMPLETE
        assert result.identity == JournalIdentityStatus.VERIFIED
        assert not result.errors


# ---------------------------------------------------------------------------
# Coverage status survivors
# ---------------------------------------------------------------------------


class TestCoverageStatusDiscrimination:
    """Kill mutations that would misreport reader coverage.

    Coverage is COMPLETE when every non-blank line reached verification;
    PARTIAL when any were discarded.
    """

    def test_partial_coverage_when_tolerant_reader_discards(self, tmp_path: Path) -> None:
        """A journal with unparsable rows must report PARTIAL coverage."""
        path, _ = _journal(tmp_path, count=3)
        lines = _lines(path)
        lines.insert(1, "not json")
        _write(path, lines)

        result = verify_journal(path)

        assert result.coverage == JournalCoverageStatus.PARTIAL
        assert result.discarded_line_indices == (1,)

    def test_complete_coverage_for_clean_journal(self, tmp_path: Path) -> None:
        """A clean journal must report COMPLETE coverage."""
        path, _ = _journal(tmp_path, count=2)

        result = verify_journal(path)

        assert result.coverage == JournalCoverageStatus.COMPLETE
        assert result.discarded_line_indices == ()


# ---------------------------------------------------------------------------
# Controlled positive: the unmutated verifier accepts valid input
# ---------------------------------------------------------------------------


class TestUnmutatedVerifierAcceptsValidJournal:
    """The control: a clean journal with a matching seal must verify."""

    def test_clean_journal_with_matching_seal_verifies(self, tmp_path: Path) -> None:
        """The baseline: everything green."""
        path, seal = _journal(tmp_path, count=4)

        result = verify_journal(path, seal=seal)

        assert result.chain_consistent
        assert result.coverage == JournalCoverageStatus.COMPLETE
        assert result.identity == JournalIdentityStatus.VERIFIED
        assert result.count == 4
        assert result.divergent_index is None
        assert not result.errors
