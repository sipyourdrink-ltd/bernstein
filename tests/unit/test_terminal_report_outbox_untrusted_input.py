"""Untrusted outbox records must cost a record, never the worker.

Two crashes the durable outbox shipped with, both reached by writing lines into
``.sdd/runtime/worker_outbox.jsonl`` -- a file every agent on the node can write.
Each one took the whole queue down with it, not just the bad line:

* ``retry_delay_s`` handed the persisted attempt count straight to
  ``compute_backoff``, which evaluates ``base * 2 ** attempt`` *before* applying
  its cap. At 1025 attempts the exponent alone exceeds the largest float, so the
  call raised ``OverflowError`` from ``due()`` on every cycle; a count a few
  orders of magnitude worse spent seconds building an integer the cap was about
  to discard.
* ``_trusted_delivery`` asked a persisted branch for ``.startswith(...)``, so a
  numeric ``agent_branch`` raised ``AttributeError`` -- which ``_load`` does not
  catch, since it handles ``TypeError`` and ``ValueError``.

Both are pinned here with the shape that actually crashed, and the good-record
behaviour they must not disturb is pinned alongside: a valid branch and SHA are
delivered byte-for-byte, a branch outside the agent namespace keeps its existing
strip-the-metadata treatment, and a malformed line never takes its healthy
neighbours with it.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from bernstein.core.orchestration import terminal_report_outbox as outbox_mod
from bernstein.core.orchestration.terminal_report_outbox import (
    _MAX_PERSISTED_ATTEMPTS,
    DEFAULT_MAX_DELAY_S,
    TerminalReportOutbox,
    retry_delay_s,
)

if TYPE_CHECKING:
    from pathlib import Path


def _write(tmp_path: Path, *records: dict[str, object]) -> Path:
    """Write *records* as the persisted outbox and return the workdir."""
    workdir = tmp_path / "work"
    path = workdir / outbox_mod._OUTBOX_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return workdir


def _record(report_id: str, **overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "report_id": report_id,
        "task_id": "t-1",
        "outcome": "done",
        "result_summary": "shipped",
        "node": "node-a",
        "attempts": 1,
        "first_seen_at": 1_700_000_000.0,
        "next_attempt_at": 0.0,
        "absences": 0,
    }
    record.update(overrides)
    return record


class TestAbsurdRetryCountsCannotOverflowTheSchedule:
    """The cap has to be applied before the power, not after it."""

    @pytest.mark.parametrize("attempts", [1, 2, 5, 64, 1024, 1025, 1026, 10_000, 10**9, 10**18])
    def test_any_persisted_count_returns_a_bounded_delay(self, attempts: int) -> None:
        delay = retry_delay_s(attempts, 1.0, 900.0)
        assert 0.0 <= delay <= 900.0, f"attempts={attempts} escaped the cap"

    def test_the_count_that_used_to_raise_is_the_one_pinned(self) -> None:
        """1025 is not an arbitrary big number.

        ``2 ** 1024`` is the largest power of two a float can hold, so it is the
        first exponent at which ``base * 2 ** attempt`` stops being a number and
        becomes an exception. Pinning it keeps the regression honest about the
        mechanism rather than just "big numbers are bad".
        """
        assert retry_delay_s(1025, 1.0, 900.0) <= 900.0

    def test_a_stratospheric_count_costs_no_measurable_time(self) -> None:
        """The old failure was slow before it was fatal: it built the integer."""
        import time

        started = time.perf_counter()
        retry_delay_s(10**9, 1.0, 900.0)
        assert time.perf_counter() - started < 0.05

    def test_a_count_past_saturation_still_lands_on_the_cap(self) -> None:
        """Clamping must not silently shorten a backoff that had reached its ceiling.

        Every exponent past saturation yields exactly ``max_delay_s`` before
        jitter, so the samples have to keep spanning up to the cap -- a clamp to
        zero would look "safe" while retrying a doomed report every cycle.
        """
        ceiling = max(retry_delay_s(10**9, 1.0, 300.0) for _ in range(200))
        assert ceiling > 150.0, "full jitter collapsed toward zero"

    def test_full_jitter_survives_for_an_ordinary_count(self) -> None:
        assert len({retry_delay_s(6, 1.0, 300.0) for _ in range(40)}) > 1

    def test_a_retry_after_header_still_wins_over_the_schedule(self) -> None:
        assert retry_delay_s(10**18, 1.0, 300.0, retry_after_s=45.0) == pytest.approx(45.0)
        assert retry_delay_s(10**18, 1.0, 300.0, retry_after_s=-5.0) == 0.0

    def test_a_degenerate_config_cannot_divide_by_zero(self) -> None:
        assert retry_delay_s(10**18, 0.0, 300.0) == 0.0
        assert retry_delay_s(10**18, 600.0, 300.0) <= 300.0

    def test_a_persisted_absurd_count_is_normalized_and_still_delivers(self, tmp_path: Path) -> None:
        """The record survives with a sane count instead of being quarantined.

        Refusing it would trade a wrong delay for a lost terminal report, which is
        the trade this outbox exists to prevent.
        """
        workdir = _write(tmp_path, _record("r-huge", attempts=10**18))
        outbox = TerminalReportOutbox(workdir, node="node-a")

        owed = outbox.pending()
        assert [r.report_id for r in owed] == ["r-huge"]
        assert owed[0].attempts == _MAX_PERSISTED_ATTEMPTS
        # ``due()`` is where the old OverflowError surfaced; it must be callable.
        assert all(isinstance(r.next_attempt_at, (int, float)) for r in outbox.due())

    def test_a_non_whole_or_negative_count_is_refused_not_loaded(self, tmp_path: Path) -> None:
        for label, value in (("fractional", 2.5), ("negative", -3), ("bool", True), ("string", "many")):
            workdir = _write(tmp_path, _record(f"r-{label}", attempts=value))
            assert TerminalReportOutbox(workdir, node="node-a").pending() == [], label

    def test_a_non_clock_timestamp_is_refused_not_loaded(self, tmp_path: Path) -> None:
        """NaN is due forever and Infinity is due never; neither may be loaded.

        ``json.loads`` accepts both spellings, so this is reachable without a
        hand of malice -- and each wedges the queue in the opposite direction.
        """
        for field in ("first_seen_at", "next_attempt_at", "absences"):
            workdir = _write(tmp_path, _record("r-clock", **{field: float("nan")}))
            assert TerminalReportOutbox(workdir, node="node-a").pending() == [], field

    def test_the_default_cap_is_still_the_documented_one(self) -> None:
        assert DEFAULT_MAX_DELAY_S == 300.0


class TestMalformedDeliveryMetadataIsRefusedNotDereferenced:
    """``_trusted_delivery`` must never be the first thing to touch the value."""

    @pytest.mark.parametrize("bad", [123, 11234.5, True, ["agent/x"], {"b": "x"}, "agent/has space"])
    def test_a_branch_that_is_not_a_usable_ref_is_quarantined(self, tmp_path: Path, bad: object) -> None:
        workdir = _write(tmp_path, _record("r-bad-branch", agent_branch=bad, agent_sha="a" * 40))
        outbox = TerminalReportOutbox(workdir, node="node-a")

        assert outbox.pending() == []
        corrupt = (workdir / outbox_mod._OUTBOX_RELPATH).with_name(
            f"{outbox_mod._OUTBOX_RELPATH.split('/')[-1]}.corrupt"
        )
        assert corrupt.is_file(), "a refused record must stay on disk for diagnosis"

    @pytest.mark.parametrize("bad", [11234, 1.5, True, ["a" * 40], {"s": "a"}])
    def test_a_sha_that_is_not_a_string_is_quarantined(self, tmp_path: Path, bad: object) -> None:
        workdir = _write(tmp_path, _record("r-bad-sha", agent_branch="agent/t-1", agent_sha=bad))
        assert TerminalReportOutbox(workdir, node="node-a").pending() == []

    def test_a_non_hex_sha_is_preserved_not_refused(self, tmp_path: Path) -> None:
        """The delivery contract accepts short and non-canonical object ids.

        This repo's own writer and tests carry values like ``abc123`` and
        ``sha-11``, and whether a SHA matches its branch is settled where the
        remote can be consulted (#6538, and the janitor's artifact check in
        #6612). A hex-only rule here would quarantine deliverable reports over a
        cosmetic field, which is the opposite of what an outbox exists to do.
        """
        workdir = _write(
            tmp_path,
            _record("r-short", agent_branch="agent/t-1", agent_sha="abc123"),
            _record("r-dashed", agent_branch="agent/t-2", agent_sha="sha-11"),
            _record("r-full", agent_branch="agent/t-3", agent_sha="a" * 40),
        )
        owed = {r.report_id: r.agent_sha for r in TerminalReportOutbox(workdir, node="node-a").pending()}
        assert owed == {"r-short": "abc123", "r-dashed": "sha-11", "r-full": "a" * 40}

    def test_a_delivery_value_with_injection_characters_is_quarantined(self, tmp_path: Path) -> None:
        """Whitespace and control characters are the part worth refusing."""
        workdir = _write(
            tmp_path,
            _record("r-space", agent_branch="agent/t-1", agent_sha="a b"),
            _record("r-newline", agent_branch="agent/t-2\nref=other", agent_sha="abc123"),
        )
        assert TerminalReportOutbox(workdir, node="node-a").pending() == []

    def test_a_numeric_sha_used_to_be_stored_and_posted(self, tmp_path: Path) -> None:
        """The quiet half of the bug: this one did not crash, it delivered junk.

        A numeric SHA sailed through ``_trusted_delivery`` (only the branch is
        dereferenced) and reached the server, where the branch/SHA comparison in
        #6538 and the janitor's artifact check in #6612 would be comparing
        against a number.
        """
        workdir = _write(tmp_path, _record("r-num-sha", agent_branch="agent/t-1", agent_sha=11234))
        assert TerminalReportOutbox(workdir, node="node-a").pending() == []

    def test_missing_and_null_metadata_stay_deliverable(self, tmp_path: Path) -> None:
        """A report with no delivery metadata is what an in-agent completion looks like."""
        workdir = _write(
            tmp_path,
            _record("r-absent"),
            _record("r-null", agent_branch=None, agent_sha=None),
            _record("r-empty", agent_branch="", agent_sha=""),
            _record("r-half", agent_branch=None, agent_sha="a" * 40),
        )
        owed = {
            r.report_id: (r.agent_branch, r.agent_sha) for r in TerminalReportOutbox(workdir, node="node-a").pending()
        }
        assert set(owed) == {"r-absent", "r-null", "r-empty", "r-half"}
        assert owed["r-absent"] == (None, None)
        assert owed["r-empty"] == (None, None), "empty is absence spelled differently, not corruption"

    def test_valid_metadata_is_delivered_byte_for_byte(self, tmp_path: Path) -> None:
        branch, sha = "agent/T-9_Feature.Branch", "DEADBEEFdeadbeef0123456789abcdef0123456789"
        workdir = _write(tmp_path, _record("r-good", agent_branch=branch, agent_sha=sha))
        owed = TerminalReportOutbox(workdir, node="node-a").pending()
        assert [(r.agent_branch, r.agent_sha) for r in owed] == [(branch, sha)]

    def test_a_branch_outside_the_agent_namespace_keeps_its_old_treatment(self, tmp_path: Path) -> None:
        """A well-formed but untrustworthy ref is stripped, not discarded.

        The central refuses to fetch a non-``agent/`` branch yet still accepts the
        completion that names it, so the report must survive with the metadata
        dropped. Type validation must not quietly upgrade that to a quarantine.
        """
        workdir = _write(tmp_path, _record("r-main", agent_branch="main", agent_sha="a" * 40))
        owed = TerminalReportOutbox(workdir, node="node-a").pending()
        assert [r.report_id for r in owed] == ["r-main"]
        assert (owed[0].agent_branch, owed[0].agent_sha) == (None, None)

    def test_a_bad_line_does_not_lose_its_healthy_neighbours(self, tmp_path: Path) -> None:
        """The actual blast radius: one hostile line, every report stranded."""
        workdir = _write(
            tmp_path,
            _record("r-good-before"),
            _record("r-bad", agent_branch=123),
            _record("r-good-after"),
        )
        owed = TerminalReportOutbox(workdir, node="node-a").pending()
        assert {r.report_id for r in owed} == {"r-good-before", "r-good-after"}

    def test_the_bare_prefix_is_refused_but_a_short_ref_is_not(self, tmp_path: Path) -> None:
        """Guards against a guard written with ``strip``.

        ``"agent/a".strip("agent/")`` is empty -- the prefix is a character set,
        not a substring -- so the empty-ref check has to compare exactly, or it
        rejects legitimate short branches.
        """
        workdir = _write(tmp_path, _record("r-short", agent_branch="agent/a"), _record("r-bare", agent_branch="agent/"))
        owed = TerminalReportOutbox(workdir, node="node-a").pending()
        assert [r.report_id for r in owed] == ["r-short"]
