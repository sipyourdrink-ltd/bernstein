"""Unit tests for absence-claim coverage verification (issue #3771)."""

from __future__ import annotations

from pathlib import Path

from bernstein.core.lineage.coverage import anchor_coverage_record, find_coverage_for_tool_call
from bernstein.core.lineage.identity import AgentCard, generate_keypair
from bernstein.core.lineage.signed_write import SignedLineageLog
from bernstein.core.lineage.store import LineageStore
from bernstein.core.quality.absence_coverage import (
    classify_completion_coverage,
    verify_journal_absence_coverage,
    verify_tool_absence_coverage,
)
from bernstein.core.quality.janitor import evaluate_signal
from bernstein.core.replay.journal import (
    EventJournal,
    JournalCoverageStatus,
    verify_journal,
)
from bernstein.core.tasks.models import CompletionSignal
from bernstein.core.tools.coverage import ToolCoverageRecord, compute_corpus_digest


def _create_clean_journal(sdd_dir: Path, run_id: str = "run-cov-1") -> tuple[EventJournal, Path]:
    journal = EventJournal(run_id, sdd_dir=sdd_dir)
    journal.record("task_start", task_id="T-1")
    journal.record("checkpoint", task_id="T-1", step=1)
    journal.record("task_complete", task_id="T-1")
    return journal, journal.path


def _corrupt_journal_row(path: Path) -> None:
    """Corrupt one physical line in the middle of the journal so tolerant reader discards it."""
    raw = path.read_text(encoding="utf-8").splitlines()
    assert len(raw) >= 3
    # Replace line 1 (middle line) with invalid JSON
    raw[1] = "NOT_VALID_JSON_CORRUPTED_ROW"
    path.write_text("\n".join(raw) + "\n", encoding="utf-8")


def test_corrupted_journal_partial_coverage_reads_unverified(tmp_path: Path) -> None:
    """The #3636 replay: a journal with a dropped row produces PARTIAL coverage and reads unverified."""
    _, jpath = _create_clean_journal(tmp_path)
    _corrupt_journal_row(jpath)

    # Replay through verify_journal()
    result = verify_journal(jpath)
    assert result.coverage == JournalCoverageStatus.PARTIAL
    assert len(result.discarded_line_indices) > 0

    # Assert resulting completion is not readable as a verified absence
    passed, detail, _ = verify_journal_absence_coverage(jpath)
    assert passed is False
    assert "unverified" in detail.lower()
    assert "partial" in detail.lower()

    verdict = classify_completion_coverage(verify_result=result)
    assert verdict.is_verified is False
    assert verdict.status == "unverified"


def test_clean_journal_complete_coverage_reads_verified(tmp_path: Path) -> None:
    """Control case: an uncorrupted journal's existing passing classification is unchanged."""
    _, jpath = _create_clean_journal(tmp_path)

    result = verify_journal(jpath)
    assert result.coverage == JournalCoverageStatus.COMPLETE
    assert result.chain_consistent is True

    passed, detail, _ = verify_journal_absence_coverage(jpath)
    assert passed is True
    assert "verified" in detail.lower()

    verdict = classify_completion_coverage(verify_result=result)
    assert verdict.is_verified is True
    assert verdict.status == "verified"


def test_glob_exists_absence_without_coverage_reads_unverified(tmp_path: Path) -> None:
    """An agent-reported glob_exists 'no matches' with no coverage record is unverified."""
    workdir = tmp_path / "workdir"
    workdir.mkdir()

    signal = CompletionSignal(type="glob_exists", value="*.missing")
    passed, detail = evaluate_signal(signal, workdir)
    assert passed is False
    assert "unverified" in detail.lower()

    # Standalone verifier check
    passed_cov, detail_cov = verify_tool_absence_coverage(
        tool_name="list_dir",
        pattern_or_query="*.missing",
        workdir=workdir,
        coverage_record=None,
    )
    assert passed_cov is False
    assert "unverified" in detail_cov.lower()


def test_glob_exists_absence_with_coverage_reads_verified(tmp_path: Path) -> None:
    """The same absence claim WITH a valid coverage record passes verified."""
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    (workdir / "file1.txt").write_text("hello")
    (workdir / "file2.txt").write_text("world")

    cov = ToolCoverageRecord(
        file_count=2,
        corpus_digest=compute_corpus_digest(["file1.txt", "file2.txt"]),
        coverage="complete",
        truncated=False,
        truncation_reason=None,
        exit_status=0,
        exit_checked=True,
    )

    signal = CompletionSignal(type="glob_exists", value="*.missing")
    passed, detail = evaluate_signal(signal, workdir, coverage_record=cov)
    assert passed is True
    assert "verified" in detail.lower()

    # A lineage-anchored coverage record commits to a content_hash, not to
    # the payload itself (see LineageEntry / entry.py), so it stays
    # discoverable by tool_call_id but must NOT verify from the anchor
    # alone: there is nothing recoverable to check the absence claim
    # against. See test_dangling_coverage_entry_does_not_spoof_verification
    # for the direct regression.
    sdd_lineage = workdir / ".sdd" / "lineage"
    sdd_lineage.mkdir(parents=True)
    priv_pem, pub_pem = generate_keypair()
    card = AgentCard(agent_id="agent:cov", kid="k1", public_key_pem=pub_pem)
    store = LineageStore(sdd_lineage)
    recorder = SignedLineageLog(store=store, operator_hmac_key=b"0" * 64)
    anchor_coverage_record(
        recorder,
        tool_name="list_dir",
        tool_call_id="tc-glob-1",
        coverage=cov,
        agent_id=card.agent_id,
        agent_card=card,
        private_key_pem=priv_pem,
    )

    found_entry = find_coverage_for_tool_call(store, "tc-glob-1")
    assert found_entry is not None, "anchored coverage entry must still be discoverable by tool_call_id"

    passed_lineage, detail_lineage = evaluate_signal(signal, workdir, lineage_store=store)
    assert passed_lineage is False
    assert "unverified" in detail_lineage.lower()


def test_dangling_coverage_entry_does_not_spoof_verification(tmp_path: Path) -> None:
    """A coverage-kind lineage entry with no recoverable payload must fail
    closed, not read as a "complete, zero-file" verified absence.

    Regression for a spoof where any lineage entry anchored with
    ``artefact_kind="coverage"`` - even one with garbage content, or one
    whose payload was simply never written back to the log - verified
    every absence claim in the project as covered, because the anchor
    carries only a ``content_hash`` commitment and the reader fabricated a
    passing record whenever it could not recover the real payload.
    """
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    sdd_lineage = workdir / ".sdd" / "lineage"
    sdd_lineage.mkdir(parents=True)

    priv_pem, pub_pem = generate_keypair()
    card = AgentCard(agent_id="agent:cov", kid="k1", public_key_pem=pub_pem)
    store = LineageStore(sdd_lineage)
    recorder = SignedLineageLog(store=store, operator_hmac_key=b"0" * 64)

    # Anchor a coverage-kind entry whose content is not a coverage record at
    # all (simulates a dangling / unrecoverable reference: whatever the
    # entry once described is not readable back from the store).
    recorder.record_write(
        artefact_path="coverage/list_dir/tc-dangling",
        new_content=b"not a coverage record",
        agent_id=card.agent_id,
        agent_card=card,
        private_key_pem=priv_pem,
        tool_call_id="tc-dangling",
        span_id="0000000000000000",
        artefact_kind="coverage",
    )
    assert find_coverage_for_tool_call(store, "tc-dangling") is not None

    signal = CompletionSignal(type="glob_exists", value="*.missing")
    passed, detail = evaluate_signal(signal, workdir, lineage_store=store)
    assert passed is False
    assert "unverified" in detail.lower()


def test_truncated_walk_coverage_reads_unverified(tmp_path: Path) -> None:
    """A truncated corpus walk degrades to unverified rather than reporting verified absence."""
    workdir = tmp_path / "workdir"
    workdir.mkdir()

    truncated_cov = ToolCoverageRecord(
        file_count=5,
        corpus_digest=compute_corpus_digest(["a.py"]),
        coverage="partial",
        truncated=True,
        truncation_reason="timeout",
        exit_status="timeout",
        exit_checked=True,
    )

    signal = CompletionSignal(type="glob_exists", value="*.missing")
    passed, detail = evaluate_signal(signal, workdir, coverage_record=truncated_cov)
    assert passed is False
    assert "unverified" in detail.lower()
    assert "partial" in detail.lower() or "truncated" in detail.lower()
