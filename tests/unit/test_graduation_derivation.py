"""Read-only graduation observations require the complete, independently sealed journal."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from bernstein.core.quality import graduation
from bernstein.core.replay.journal import EventJournal, JournalSeal, load_events

if TYPE_CHECKING:
    from pathlib import Path


def _journal(tmp_path: Path, events: tuple[str, ...]) -> tuple[Path, JournalSeal]:
    journal = EventJournal("observed-run", tmp_path / ".sdd")
    for index, event in enumerate(events):
        journal.record(event, task_id=f"task-{index}")
    return journal.path, JournalSeal(head=journal.head(), event_count=journal.event_count())


def _derive(path: Path, seal: JournalSeal | None, first: int = 0, last: int = 2):
    window = graduation.GraduationEvidenceWindow("observed-run", seal, first, last)
    return graduation.derive_graduation_observations_from_path(
        path, window=window, stage=graduation.GraduationStage.SANDBOX
    )


def test_stored_counter_mutation_cannot_change_observed_counts(tmp_path: Path) -> None:
    path, seal = _journal(tmp_path, ("task_completed", "task_failed", "task_completed"))
    store = graduation.GraduationStore(tmp_path / ".sdd")
    record = store.get_or_create("observed-run")
    before = _derive(path, seal)
    record.current_metrics().tasks_completed = 999
    record.current_metrics().tasks_failed = 999
    store.save(record)
    after = _derive(path, seal)
    assert before.canonical_bytes() == after.canonical_bytes()
    comparison = after.compare_stored(record.current_metrics())
    assert comparison["stage_metrics_parity"] == "not_established"
    assert comparison["observed_count_comparisons"]["tasks_completed"] == {
        "stored": 999,
        "observed": 2,
        "matches_observed_count": False,
    }
    assert "hours_elapsed" in comparison["unsupported_metrics"]


def test_same_window_is_byte_identical_without_clock_or_cwd_reads(tmp_path: Path, monkeypatch) -> None:
    path, seal = _journal(tmp_path, ("task_completed", "task_failed", "task_failed"))
    monkeypatch.setattr("time.time", lambda: 0.0)
    first = _derive(path, seal).canonical_bytes()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr("time.time", lambda: 99999999999.0)
    assert _derive(path, seal).canonical_bytes() == first


@pytest.mark.parametrize("damage", ["payload", "truncated-row", "torn-tail", "discarded-middle"])
def test_damaged_full_journal_refuses_even_when_selected_prefix_survives(tmp_path: Path, damage: str) -> None:
    path, seal = _journal(tmp_path, ("task_completed", "task_failed", "task_failed"))
    lines = path.read_text().splitlines()
    if damage == "payload":
        row = json.loads(lines[2])
        row["task_id"] = "altered"
        lines[2] = json.dumps(row)
    elif damage == "truncated-row":
        lines.pop()
    elif damage == "torn-tail":
        lines[2] = '{"event":'
    else:
        lines.insert(1, "not-json")
    path.write_text("\n".join(lines) + "\n")
    with pytest.raises(graduation.GraduationEvidenceError, match=r"observed-run\[0:0\]"):
        _derive(path, seal, first=0, last=0)


@pytest.mark.parametrize("seal_kind", ["absent", "wrong-head", "wrong-count"])
def test_missing_or_mismatched_independent_seal_refuses(tmp_path: Path, seal_kind: str) -> None:
    path, seal = _journal(tmp_path, ("task_completed", "task_failed", "task_completed"))
    candidate = None
    if seal_kind == "wrong-head":
        candidate = JournalSeal("different-head", seal.event_count)
    elif seal_kind == "wrong-count":
        candidate = JournalSeal(seal.head, seal.event_count + 1)
    with pytest.raises(graduation.GraduationEvidenceError, match=r"observed-run\[0:2\]"):
        _derive(path, candidate)


@pytest.mark.parametrize("first,last", [(-1, 1), (2, 1), (0, 3), (True, 2), (0, False)])
def test_invalid_or_empty_window_refuses(tmp_path: Path, first: int, last: int) -> None:
    path, seal = _journal(tmp_path, ("task_completed", "task_failed", "task_failed"))
    with pytest.raises(graduation.GraduationEvidenceError, match="observed-run"):
        _derive(path, seal, first, last)


def test_observed_terminal_counts_and_streak_cite_absolute_indices(tmp_path: Path) -> None:
    events = (
        "task_failed",
        "task_completed",
        "task_failed",
        "task_verification_failed",
        "task_completed",
        "task_failed",
        "future_outcome",
        "task_failed",
    )
    path, seal = _journal(tmp_path, events)
    projection = _derive(path, seal, first=1, last=7)
    doc = projection.to_dict()
    assert doc["observed_counts"] == {
        "task_completed": {"count": 2, "event_index_range": {"first": 1, "last": 4}},
        "task_failed": {"count": 3, "event_index_range": {"first": 2, "last": 7}},
        "trailing_task_failed": {"count": 2, "event_index_range": {"first": 5, "last": 7}},
    }
    assert doc["unsupported_event_types"] == ["future_outcome", "task_verification_failed"]
    assert doc["stage_metrics_parity"] == "not_established"
    assert doc["evidence_window"] == {
        "journal_id": "observed-run",
        "first_index": 1,
        "last_index": 7,
        "sealed_head": seal.head,
        "sealed_event_count": 8,
    }
    assert "rule_version" in doc
    for unsupported in ("hours_elapsed", "success_rate", "total_cost_usd", "total_duration_s"):
        assert unsupported in doc["unsupported_metrics"]


def test_nonterminal_rejection_is_not_an_observed_terminal_failure(tmp_path: Path) -> None:
    path, seal = _journal(tmp_path, ("task_verification_failed", "task_retried", "task_claimed"))
    doc = _derive(path, seal).to_dict()
    for metric in doc["observed_counts"].values():
        assert metric == {"count": 0, "event_index_range": {"first": None, "last": None}}
    assert doc["unsupported_event_types"] == ["task_claimed", "task_retried", "task_verification_failed"]
    assert "terminal_outcome_coverage" in doc["unsupported_metrics"]


def test_unauthenticated_row_index_cannot_rewrite_citations(tmp_path: Path) -> None:
    path, seal = _journal(tmp_path, ("task_completed", "task_failed", "task_completed"))
    before = _derive(path, seal).canonical_bytes()
    rows = load_events(path).events
    for row in rows:
        row["index"] = 1000
        row["ts"] = 9999999999.0
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    assert _derive(path, seal).canonical_bytes() == before


def test_pure_derivation_uses_the_supplied_complete_snapshot(tmp_path: Path, monkeypatch) -> None:
    path, seal = _journal(tmp_path, ("task_completed", "task_failed", "task_completed"))
    loaded = load_events(path)
    expected = _derive(path, seal).canonical_bytes()
    path.unlink()
    window = graduation.GraduationEvidenceWindow("observed-run", seal, 0, 2)
    monkeypatch.setattr("time.time", lambda: pytest.fail("derivation read the wall clock"))
    actual = graduation.derive_graduation_observations(loaded, window=window, stage=graduation.GraduationStage.SANDBOX)
    assert actual.canonical_bytes() == expected


def test_unknown_rule_version_refuses(tmp_path: Path) -> None:
    path, seal = _journal(tmp_path, ("task_completed", "task_failed", "task_completed"))
    window = graduation.GraduationEvidenceWindow("observed-run", seal, 0, 2)
    with pytest.raises(graduation.GraduationEvidenceError, match="rule"):
        graduation.derive_graduation_observations(
            load_events(path),
            window=window,
            stage=graduation.GraduationStage.SANDBOX,
            rule_version="unrecognised-rule",
        )


def test_comparison_refuses_different_stage_metadata(tmp_path: Path) -> None:
    path, seal = _journal(tmp_path, ("task_completed", "task_failed", "task_completed"))
    stored = graduation.StageMetrics(stage=graduation.GraduationStage.SHADOW)
    with pytest.raises(graduation.GraduationEvidenceError, match="stage"):
        _derive(path, seal).compare_stored(stored)
