"""Unit tests for deterministic recurrence canonicalisation (#2302).

A recurring goal fire folds its recurrence rule (RFC-5545 ``RRULE`` or a
simple cron expression) into the projection. Canonicalisation must be a
pure, order-stable function so two operators who wrote the same rule in a
different token order land on the byte-identical canonical text - and
therefore the byte-identical graph hash.
"""

from __future__ import annotations

import pytest

from bernstein.core.orchestration.recurrence import (
    RecurrenceParseError,
    canonicalise_recurrence,
)


class TestCronCanonicalisation:
    def test_bare_cron_gets_prefix(self) -> None:
        assert canonicalise_recurrence("0 9 * * *") == "cron:0 9 * * *"

    def test_cron_prefix_idempotent(self) -> None:
        assert canonicalise_recurrence("cron:0 9 * * *") == "cron:0 9 * * *"

    def test_cron_whitespace_normalised(self) -> None:
        assert canonicalise_recurrence("  0 9 * * *  ") == "cron:0 9 * * *"

    def test_invalid_cron_raises(self) -> None:
        with pytest.raises(RecurrenceParseError):
            canonicalise_recurrence("not a cron")


class TestRruleCanonicalisation:
    def test_part_order_independent(self) -> None:
        a = canonicalise_recurrence("RRULE:INTERVAL=2;FREQ=DAILY")
        b = canonicalise_recurrence("RRULE:FREQ=DAILY;INTERVAL=2")
        assert a == b
        assert a == "RRULE:FREQ=DAILY;INTERVAL=2"

    def test_freq_prefix_optional_on_input(self) -> None:
        a = canonicalise_recurrence("FREQ=DAILY")
        b = canonicalise_recurrence("RRULE:FREQ=DAILY")
        assert a == b == "RRULE:FREQ=DAILY"

    def test_by_list_sorted_numerically(self) -> None:
        a = canonicalise_recurrence("RRULE:FREQ=DAILY;BYHOUR=9,3,12")
        assert a == "RRULE:FREQ=DAILY;BYHOUR=3,9,12"

    def test_by_list_order_independent(self) -> None:
        a = canonicalise_recurrence("RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR")
        b = canonicalise_recurrence("RRULE:FREQ=WEEKLY;BYDAY=FR,MO,WE")
        assert a == b

    def test_part_name_case_normalised(self) -> None:
        a = canonicalise_recurrence("rrule:freq=daily;byhour=3")
        assert a == "RRULE:FREQ=DAILY;BYHOUR=3"

    def test_signed_bysetpos_sorted(self) -> None:
        a = canonicalise_recurrence("RRULE:FREQ=MONTHLY;BYSETPOS=3,-1,1")
        assert a == "RRULE:FREQ=MONTHLY;BYSETPOS=-1,1,3"

    def test_unknown_freq_raises(self) -> None:
        with pytest.raises(RecurrenceParseError):
            canonicalise_recurrence("RRULE:FREQ=FORTNIGHTLY")

    def test_missing_freq_raises(self) -> None:
        with pytest.raises(RecurrenceParseError):
            canonicalise_recurrence("RRULE:INTERVAL=2")

    def test_malformed_part_raises(self) -> None:
        with pytest.raises(RecurrenceParseError):
            canonicalise_recurrence("RRULE:FREQ=DAILY;BOGUS")

    def test_duplicate_part_raises(self) -> None:
        with pytest.raises(RecurrenceParseError):
            canonicalise_recurrence("RRULE:FREQ=DAILY;FREQ=WEEKLY")

    def test_invalid_byday_token_raises(self) -> None:
        with pytest.raises(RecurrenceParseError):
            canonicalise_recurrence("RRULE:FREQ=WEEKLY;BYDAY=XX")

    def test_signed_byday_token_accepted(self) -> None:
        a = canonicalise_recurrence("RRULE:FREQ=MONTHLY;BYDAY=-1SU")
        assert a == "RRULE:FREQ=MONTHLY;BYDAY=-1SU"


class TestEmpty:
    def test_empty_returns_empty(self) -> None:
        assert canonicalise_recurrence("") == ""
        assert canonicalise_recurrence("   ") == ""
