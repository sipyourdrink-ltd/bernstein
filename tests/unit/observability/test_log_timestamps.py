"""Timestamp extraction for the local log search index."""

from datetime import UTC, datetime

import pytest

from bernstein.core.observability.log_search import _parse_log_timestamp


@pytest.mark.parametrize(
    ("prefix", "seconds_after_noon"),
    [
        ("2026-01-02T12:00:00Z", 0),
        ("2026-01-02T13:00:00+01:00", 0),
        ("2026-01-02T13:00:00.500+01:00", 0.5),
        ("[2026-01-02 07:00:00-0500]", 0),
    ],
)
def test_log_timestamp_preserves_offset_and_fraction(prefix: str, seconds_after_noon: float) -> None:
    noon_utc = datetime(2026, 1, 2, 12, tzinfo=UTC).timestamp()
    assert _parse_log_timestamp(f"{prefix} INFO event") == noon_utc + seconds_after_noon
