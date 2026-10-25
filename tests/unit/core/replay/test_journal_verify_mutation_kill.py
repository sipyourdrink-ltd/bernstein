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


# ---------------------------------------------------------------------------
# Run-id barrier, resume refusals, retention and repair (#6471 follow-up)
#
# The nightly scored this target 81/120 (threshold 70%). The survivors sat in
# the run-id barrier, the refusals ``EventJournal.resume`` and
# ``repair_journal_tail`` print, the replay-retention pruner, and the strict
# reader's messages. Each test below pins one of those to an exact value.
# ---------------------------------------------------------------------------


def _write_rows(path: Path, rows: list[object]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


class TestRunIdBarrier:
    @pytest.mark.parametrize("run_id", [".", "..", "a/b", "", "x" * 65, "a b"])
    def test_an_unsafe_run_id_is_refused_by_name(self, tmp_path: Path, run_id: str) -> None:
        from bernstein.core.replay.journal import JournalPathError, run_journal_path

        with pytest.raises(JournalPathError) as exc:
            run_journal_path(tmp_path, run_id)
        assert str(exc.value) == f"unsafe run_id for journal path: {run_id!r}"

    @pytest.mark.parametrize("run_id", ["run-1", "a.b_c", "x" * 64, "..."])
    def test_a_safe_run_id_names_its_own_directory(self, tmp_path: Path, run_id: str) -> None:
        from bernstein.core.replay.journal import run_journal_path

        assert run_journal_path(tmp_path, run_id).parent.name == run_id


class TestResumeRefusals:
    def test_resume_of_a_mid_journal_discard_names_the_line(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        journal = EventJournal("resume-mid", sdd)
        journal.record("a")
        journal.record("b")
        lines = journal.path.read_text(encoding="utf-8").splitlines(keepends=True)
        journal.path.write_text(lines[0] + "not json\n" + lines[1], encoding="utf-8")

        with pytest.raises(ValueError) as exc:
            EventJournal.resume("resume-mid", sdd)
        assert str(exc.value) == f"cannot resume journal {journal.path}: reader discarded physical line(s): 1"

    def test_resume_of_a_broken_chain_quotes_the_verifier(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        journal = EventJournal("resume-broken", sdd)
        journal.record("a")
        journal.record("b")
        rows = [json.loads(line) for line in journal.path.read_text(encoding="utf-8").splitlines()]
        rows[1]["prev_hash"] = "0" * 64
        _write_rows(journal.path, rows)
        expected_errors = verify_journal(journal.path).errors
        assert expected_errors

        with pytest.raises(ValueError) as exc:
            EventJournal.resume("resume-broken", sdd)
        assert str(exc.value) == f"cannot resume journal {journal.path}: {'; '.join(expected_errors)}"

    def test_resume_of_a_clean_journal_continues_its_chain(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        journal = EventJournal("resume-ok", sdd)
        journal.record("a")
        journal.record("b")

        resumed = EventJournal.resume("resume-ok", sdd)
        assert resumed.head() == journal.head()
        assert resumed.event_count() == 2


class TestRetention:
    """``BERNSTEIN_REPLAY_RETENTION`` prunes the oldest past runs, never the active one."""

    @staticmethod
    def _runs(sdd: Path, *names: str) -> None:
        for name in names:
            (sdd / "runs" / name).mkdir(parents=True)
            (sdd / "runs" / name / "journal.jsonl").write_text("", encoding="utf-8")

    @staticmethod
    def _left(sdd: Path) -> list[str]:
        return sorted(d.name for d in (sdd / "runs").iterdir())

    @pytest.mark.parametrize("raw", ["", "   ", "abc", "0", "-3"])
    def test_unset_invalid_or_non_positive_retention_keeps_everything(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv("BERNSTEIN_REPLAY_RETENTION", raw)
        sdd = tmp_path / ".sdd"
        self._runs(sdd, "r1", "r2", "r3")
        EventJournal("r9", sdd)
        assert self._left(sdd) == ["r1", "r2", "r3", "r9"]

    def test_retention_one_keeps_only_the_active_run(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BERNSTEIN_REPLAY_RETENTION", "1")
        sdd = tmp_path / ".sdd"
        self._runs(sdd, "r1", "r2")
        EventJournal("r9", sdd)
        assert self._left(sdd) == ["r9"]

    def test_the_oldest_runs_go_first(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BERNSTEIN_REPLAY_RETENTION", "2")
        sdd = tmp_path / ".sdd"
        self._runs(sdd, "r1", "r2", "r3")
        EventJournal("r9", sdd)
        assert self._left(sdd) == ["r3", "r9"]

    def test_exactly_at_the_limit_nothing_goes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BERNSTEIN_REPLAY_RETENTION", "3")
        sdd = tmp_path / ".sdd"
        self._runs(sdd, "r1", "r2")
        EventJournal("r9", sdd)
        assert self._left(sdd) == ["r1", "r2", "r9"]

    def test_an_active_run_that_sorts_first_is_skipped_not_counted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BERNSTEIN_REPLAY_RETENTION", "2")
        sdd = tmp_path / ".sdd"
        self._runs(sdd, "r1", "r2", "r3")
        EventJournal("a0", sdd)
        assert self._left(sdd) == ["a0", "r3"]

    def test_files_beside_the_run_directories_are_neither_counted_nor_removed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BERNSTEIN_REPLAY_RETENTION", "2")
        sdd = tmp_path / ".sdd"
        self._runs(sdd, "r1")
        (sdd / "runs" / "index.json").write_text("{}", encoding="utf-8")
        EventJournal("r9", sdd)
        assert self._left(sdd) == ["index.json", "r1", "r9"]


class TestRepairRefusalsAndCut:
    def test_a_clean_journal_reports_its_count_and_head(self, tmp_path: Path) -> None:
        from bernstein.core.replay.journal import JournalRepairResult, repair_journal_tail

        path, seal = _journal(tmp_path, count=3)
        assert repair_journal_tail(path) == JournalRepairResult(repaired=False, event_count=3, head=seal.head)
        assert JournalRepairResult(repaired=False).event_count == 0

    def test_a_mid_journal_discard_is_refused_with_its_reason(self, tmp_path: Path) -> None:
        from bernstein.core.replay.journal import repair_journal_tail

        path, _ = _journal(tmp_path, count=2)
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        path.write_text(lines[0] + "garbage\n" + lines[1], encoding="utf-8")

        with pytest.raises(ValueError) as exc:
            repair_journal_tail(path)
        assert str(exc.value) == (
            f"refusing repair of {path}: reader discarded physical line(s) 1 in the middle of the journal "
            "(corruption, not a torn write); repair truncates only a trailing fragment"
        )

    def test_a_torn_tail_is_cut_to_the_exact_surviving_bytes(self, tmp_path: Path) -> None:
        from bernstein.core.replay.journal import repair_journal_tail

        path, seal = _journal(tmp_path, count=2)
        clean = path.read_bytes()
        path.write_bytes(clean + b'{"event": "torn')

        result = repair_journal_tail(path, seal=seal)

        assert result.repaired is True
        assert result.removed_line_indices == (2,)
        assert (result.event_count, result.head) == (2, seal.head)
        assert path.read_bytes() == clean

    def test_a_seal_with_the_right_count_and_a_wrong_head_refuses_before_writing(self, tmp_path: Path) -> None:
        from bernstein.core.replay.journal import repair_journal_tail

        path, seal = _journal(tmp_path, count=2)
        torn = path.read_bytes() + b'{"event": "torn'
        path.write_bytes(torn)
        wrong = JournalSeal(head="f" * 64, event_count=seal.event_count)

        with pytest.raises(ValueError) as exc:
            repair_journal_tail(path, seal=wrong)
        assert str(exc.value) == (
            f"refusing repair of {path}: truncated journal (head={seal.head}, events=2) does not match the "
            f"external seal (head={'f' * 64}, events=2); the journal is sealed and this command is not its authority"
        )
        assert path.read_bytes() == torn


class TestStrictReaderMessages:
    def test_undecodable_bytes_name_the_line(self, tmp_path: Path) -> None:
        path = tmp_path / "j.jsonl"
        path.write_bytes(b"\n\xff\xfe\n")
        with pytest.raises(JournalParseError) as exc:
            load_events(path, strict=True)
        assert str(exc.value) == "undecodable bytes at physical line 1 (not valid UTF-8)"

    @pytest.mark.parametrize(
        ("row", "message"),
        [
            ({"index": 0, "prev_hash": "", "payload_hash": "p", "event_hash": "e"}, "a missing or non-string 'event'"),
            (
                {"event": "", "index": 0, "prev_hash": "", "payload_hash": "p", "event_hash": "e"},
                "a missing or non-string 'event'",
            ),
            (
                {"event": "x", "prev_hash": "", "payload_hash": "p", "event_hash": "e"},
                "a missing or non-integer 'index'",
            ),
            (
                {"event": "x", "index": True, "prev_hash": "", "payload_hash": "p", "event_hash": "e"},
                "a missing or non-integer 'index'",
            ),
            ({"event": "x", "index": 0, "payload_hash": "p", "event_hash": "e"}, "a missing or non-string 'prev_hash'"),
        ],
    )
    def test_a_malformed_row_names_the_field(self, tmp_path: Path, row: dict[str, object], message: str) -> None:
        path = tmp_path / "j.jsonl"
        _write_rows(path, [row])
        with pytest.raises(JournalParseError) as exc:
            load_events(path, strict=True)
        assert str(exc.value) == f"row at physical line 0 has {message}"

    def test_verify_names_the_discarded_lines(self, tmp_path: Path) -> None:
        path, _ = _journal(tmp_path, count=1)
        path.write_text(path.read_text(encoding="utf-8") + "[1, 2]\n", encoding="utf-8")
        result = verify_journal(path)
        assert "reader discarded unparsable or non-object physical line(s): 1" in result.errors


class TestHashProfilesArePinnedBytes:
    """Each profile hashes a fixed pre-image; a writer and a verifier that swapped
    profiles together would still agree with each other, so the bytes are pinned
    here independently of the module.

    ``é`` separates the two: ``py-json-v1`` escapes it (``ensure_ascii``), while
    RFC 8785 keeps it as UTF-8.
    """

    def test_payload_hash_per_profile(self) -> None:
        import hashlib

        from bernstein.core.replay.journal import HASH_PROFILE_JCS_V2, HASH_PROFILE_LEGACY, _payload_hash

        legacy = hashlib.sha256(b'{"event":"step","value":"\\u00e9"}').hexdigest()
        jcs = hashlib.sha256('{"event":"step","value":"é"}'.encode()).hexdigest()
        assert _payload_hash("step", {"value": "é"}) == legacy
        assert _payload_hash("step", {"value": "é"}, HASH_PROFILE_LEGACY) == legacy
        assert _payload_hash("step", {"value": "é"}, HASH_PROFILE_JCS_V2) == jcs

    def test_event_hash_per_profile(self) -> None:
        import hashlib

        from bernstein.core.replay.journal import HASH_PROFILE_JCS_V2, compute_event_hash

        fields = {"prev_hash": "", "event_type": "é", "payload_hash": "p", "index": 0}
        legacy = hashlib.sha256(b'{"event_type":"\\u00e9","index":0,"payload_hash":"p","prev_hash":""}').hexdigest()
        jcs = hashlib.sha256('{"event_type":"é","index":0,"payload_hash":"p","prev_hash":""}'.encode()).hexdigest()
        assert compute_event_hash(**fields) == legacy  # type: ignore[arg-type]
        assert compute_event_hash(**fields, hash_profile=HASH_PROFILE_JCS_V2) == jcs  # type: ignore[arg-type]


def test_retention_with_exactly_one_excess_run_removes_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BERNSTEIN_REPLAY_RETENTION", "3")
    sdd = tmp_path / ".sdd"
    for name in ("r1", "r2", "r3"):
        (sdd / "runs" / name).mkdir(parents=True)
    EventJournal("r9", sdd)
    assert sorted(d.name for d in (sdd / "runs").iterdir()) == ["r2", "r3", "r9"]


def test_repair_of_a_journal_that_is_only_a_torn_line_empties_it(tmp_path: Path) -> None:
    from bernstein.core.replay.journal import JournalRepairResult, repair_journal_tail

    path = tmp_path / "journal.jsonl"
    path.write_bytes(b'{"event": "torn')

    assert repair_journal_tail(path) == JournalRepairResult(repaired=True, removed_line_indices=(0,))
    assert path.read_bytes() == b""
