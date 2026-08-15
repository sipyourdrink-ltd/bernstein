"""Adversarial coverage and sealed-identity tests for run journals (#3651)."""

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
    verify_journal,
)


def _journal(tmp_path: Path) -> tuple[Path, JournalSeal]:
    journal = EventJournal("identity-matrix", tmp_path / ".sdd")
    for index in range(4):
        journal.record("step", value=index)
    return journal.path, JournalSeal(head=journal.head(), event_count=journal.event_count())


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def _write(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_tolerant_load_reports_discarded_physical_lines(tmp_path: Path) -> None:
    path, _ = _journal(tmp_path)
    lines = _lines(path)
    lines.insert(2, "not json")
    lines.append("[]")
    _write(path, lines)

    loaded = load_events(path)

    assert len(loaded.events) == 4
    assert loaded.discarded_count == 2
    assert loaded.discarded_line_indices == (2, 5)


def test_resume_refuses_an_all_discarded_journal(tmp_path: Path) -> None:
    path = tmp_path / ".sdd" / "runs" / "all-junk" / "journal.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("not json\n[]\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r"discarded physical line\(s\): 0, 1"):
        EventJournal.resume("all-junk", tmp_path / ".sdd")


def test_missing_journal_never_matches_even_an_empty_external_seal(tmp_path: Path) -> None:
    result = verify_journal(tmp_path / "missing.jsonl", seal=JournalSeal(head="", event_count=0))

    assert result.chain_consistent
    assert result.coverage == JournalCoverageStatus.COMPLETE
    assert result.identity == JournalIdentityStatus.MISMATCHED
    assert "journal file is missing" in result.errors[0]


def test_blank_line_is_not_a_discard_or_a_false_coverage_failure(tmp_path: Path) -> None:
    """A physical-line count would reject this; the reader's report does not."""
    path, seal = _journal(tmp_path)
    lines = _lines(path)
    lines.insert(2, "")
    _write(path, lines)

    result = verify_journal(path, seal=seal)
    strict = load_events(path, strict=True)

    assert result.chain_consistent
    assert result.coverage == JournalCoverageStatus.COMPLETE
    assert result.identity == JournalIdentityStatus.VERIFIED
    assert result.discarded_line_indices == ()
    assert len(strict.events) == result.count
    assert len(_lines(path)) != result.count  # naive physical-line counting false-positives


def test_inserted_junk_is_visible_even_when_surviving_chain_and_seal_match(tmp_path: Path) -> None:
    path, seal = _journal(tmp_path)
    lines = _lines(path)
    lines.insert(2, "not json")
    _write(path, lines)

    result = verify_journal(path, seal=seal)

    assert result.chain_consistent
    assert result.coverage == JournalCoverageStatus.PARTIAL
    assert result.identity == JournalIdentityStatus.MISMATCHED
    assert result.discarded_line_indices == (2,)
    assert "physical line(s): 2" in result.errors[-1]
    with pytest.raises(JournalParseError):
        load_events(path, strict=True)


def test_partial_trailing_row_is_reported_as_partial_coverage(tmp_path: Path) -> None:
    path, seal = _journal(tmp_path)
    lines = _lines(path)
    lines[-1] = lines[-1][: len(lines[-1]) // 2]
    _write(path, lines)

    result = verify_journal(path, seal=seal)

    assert result.chain_consistent
    assert result.coverage == JournalCoverageStatus.PARTIAL
    assert result.identity == JournalIdentityStatus.MISMATCHED
    assert result.count == 3
    assert result.discarded_line_indices == (3,)
    with pytest.raises(JournalParseError):
        load_events(path, strict=True)


@pytest.mark.parametrize("removed", [1, 2])
def test_clean_boundary_truncation_requires_external_seal(tmp_path: Path, removed: int) -> None:
    """A clean prefix is undetectable from the journal alone and fails its seal."""
    path, seal = _journal(tmp_path)
    _write(path, _lines(path)[:-removed])

    strict = load_events(path, strict=True)
    unsealed = verify_journal(path)
    sealed = verify_journal(path, seal=seal)

    assert len(strict.events) == 4 - removed  # strict mode cannot see clean boundary loss
    assert unsealed.chain_consistent
    assert unsealed.coverage == JournalCoverageStatus.COMPLETE
    assert unsealed.identity == JournalIdentityStatus.UNVERIFIABLE
    assert sealed.chain_consistent
    assert sealed.coverage == JournalCoverageStatus.COMPLETE
    assert sealed.identity == JournalIdentityStatus.MISMATCHED
    assert sealed.count == 4 - removed


def test_corrupted_middle_row_preserves_chain_break_diagnostic(tmp_path: Path) -> None:
    path, seal = _journal(tmp_path)
    lines = _lines(path)
    lines[1] = "{corrupt"
    _write(path, lines)

    result = verify_journal(path, seal=seal)

    assert not result.chain_consistent
    assert result.divergent_index == 1
    assert result.errors[0] == "step 1: prev_hash break"
    assert result.discarded_line_indices == (1,)


def test_deleted_middle_row_preserves_chain_break_diagnostic(tmp_path: Path) -> None:
    path, seal = _journal(tmp_path)
    lines = _lines(path)
    del lines[1]
    _write(path, lines)

    result = verify_journal(path, seal=seal)

    assert not result.chain_consistent
    assert result.divergent_index == 1
    assert result.errors[0] == "step 1: prev_hash break"


def test_parseable_mutation_preserves_event_hash_diagnostic(tmp_path: Path) -> None:
    path, seal = _journal(tmp_path)
    lines = _lines(path)
    row = json.loads(lines[1])
    row["value"] = 99
    lines[1] = json.dumps(row)
    _write(path, lines)

    result = verify_journal(path, seal=seal)

    assert not result.chain_consistent
    assert result.divergent_index == 1
    assert result.errors[0] == "step 1: event_hash mismatch"
