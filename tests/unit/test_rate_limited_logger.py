"""Tests for rate-limited logging — LogDeduplicator, RateLimitedLogFilter, installer."""

from __future__ import annotations

import logging

from bernstein.core.rate_limited_logger import (
    LogDeduplicator,
    RateLimitedLogFilter,
    install_rate_limited_filter,
)

# ---------------------------------------------------------------------------
# LogDeduplicator — should_log / record
# ---------------------------------------------------------------------------


class TestLogDeduplicatorShouldLog:
    """Tests for LogDeduplicator.should_log and record."""

    def test_first_occurrence_is_allowed(self) -> None:
        d = LogDeduplicator(window_seconds=60.0, max_per_window=3)
        assert d.should_log("boom", now=0.0) is True

    def test_under_limit_is_allowed(self) -> None:
        d = LogDeduplicator(window_seconds=60.0, max_per_window=3)
        for t in range(3):
            d.record("boom", now=float(t))
        # 3 already recorded → next should be suppressed
        assert d.should_log("boom", now=3.0) is False

    def test_over_limit_is_suppressed(self) -> None:
        d = LogDeduplicator(window_seconds=60.0, max_per_window=2)
        d.record("err", now=0.0)
        d.record("err", now=1.0)
        assert d.should_log("err", now=2.0) is False

    def test_different_messages_independent(self) -> None:
        d = LogDeduplicator(window_seconds=60.0, max_per_window=1)
        d.record("err-a", now=0.0)
        assert d.should_log("err-a", now=1.0) is False
        assert d.should_log("err-b", now=1.0) is True


# ---------------------------------------------------------------------------
# LogDeduplicator — window expiry
# ---------------------------------------------------------------------------


class TestLogDeduplicatorWindowExpiry:
    """Tests that old entries fall out of the sliding window."""

    def test_old_entries_expire(self) -> None:
        d = LogDeduplicator(window_seconds=10.0, max_per_window=2)
        d.record("x", now=0.0)
        d.record("x", now=1.0)
        assert d.should_log("x", now=2.0) is False
        # After the window slides past, entries expire
        assert d.should_log("x", now=11.0) is True

    def test_partial_expiry(self) -> None:
        d = LogDeduplicator(window_seconds=10.0, max_per_window=2)
        d.record("x", now=0.0)
        d.record("x", now=5.0)
        # At t=11 the first entry has expired but the second hasn't
        assert d.should_log("x", now=11.0) is True


# ---------------------------------------------------------------------------
# LogDeduplicator — suppressed count / summary
# ---------------------------------------------------------------------------


class TestLogDeduplicatorSuppression:
    """Tests for get_suppressed_count and get_summary."""

    def test_suppressed_count_zero_initially(self) -> None:
        d = LogDeduplicator(window_seconds=60.0, max_per_window=1)
        assert d.get_suppressed_count("unknown") == 0

    def test_suppressed_count_increments(self) -> None:
        d = LogDeduplicator(window_seconds=60.0, max_per_window=1)
        d.record("x", now=0.0)  # allowed (1st)
        d.record("x", now=1.0)  # suppressed
        d.record("x", now=2.0)  # suppressed
        assert d.get_suppressed_count("x") == 2

    def test_summary_none_when_not_suppressed(self) -> None:
        d = LogDeduplicator(window_seconds=60.0, max_per_window=5)
        d.record("ok", now=0.0)
        assert d.get_summary("ok") is None

    def test_summary_text(self) -> None:
        d = LogDeduplicator(window_seconds=60.0, max_per_window=1)
        d.record("fail", now=0.0)
        d.record("fail", now=1.0)
        d.record("fail", now=2.0)
        summary = d.get_summary("fail")
        assert summary is not None
        assert "fail" in summary
        assert "2" in summary
        assert "60.0s" in summary


# ---------------------------------------------------------------------------
# LogDeduplicator — flush_all / reset
# ---------------------------------------------------------------------------


class TestLogDeduplicatorFlushReset:
    """Tests for flush_all and reset."""

    def test_flush_all_returns_summaries(self) -> None:
        d = LogDeduplicator(window_seconds=60.0, max_per_window=1)
        d.record("a", now=0.0)
        d.record("a", now=1.0)  # suppressed
        d.record("b", now=0.0)
        d.record("b", now=1.0)  # suppressed
        d.record("c", now=0.0)  # not suppressed

        summaries = d.flush_all()
        assert len(summaries) == 2
        texts = " ".join(summaries)
        assert "a" in texts
        assert "b" in texts
        # c had no suppressions
        assert "c" not in texts

    def test_flush_all_resets_state(self) -> None:
        d = LogDeduplicator(window_seconds=60.0, max_per_window=1)
        d.record("x", now=0.0)
        d.record("x", now=1.0)
        d.flush_all()
        # After flush, state is empty
        assert d.should_log("x", now=2.0) is True
        assert d.get_suppressed_count("x") == 0

    def test_reset_clears_everything(self) -> None:
        d = LogDeduplicator(window_seconds=60.0, max_per_window=1)
        d.record("z", now=0.0)
        d.record("z", now=1.0)
        d.reset()
        assert d.should_log("z", now=2.0) is True
        assert d.get_suppressed_count("z") == 0


# ---------------------------------------------------------------------------
# RateLimitedLogFilter — with mock LogRecord
# ---------------------------------------------------------------------------


def _make_record(msg: str, level: int = logging.ERROR) -> logging.LogRecord:
    """Create a minimal LogRecord for testing."""
    return logging.LogRecord(
        name="test",
        level=level,
        pathname="test.py",
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )


class TestRateLimitedLogFilter:
    """Tests for RateLimitedLogFilter.filter."""

    def test_first_message_passes(self) -> None:
        dedup = LogDeduplicator(window_seconds=60.0, max_per_window=2)
        filt = RateLimitedLogFilter(dedup)
        record = _make_record("connection lost")
        assert filt.filter(record) is True

    def test_excess_messages_suppressed(self) -> None:
        dedup = LogDeduplicator(window_seconds=60.0, max_per_window=1)
        filt = RateLimitedLogFilter(dedup)

        r1 = _make_record("connection lost")
        assert filt.filter(r1) is True

        r2 = _make_record("connection lost")
        assert filt.filter(r2) is False

    def test_summary_injected_after_suppression(self) -> None:
        dedup = LogDeduplicator(window_seconds=10.0, max_per_window=1)
        filt = RateLimitedLogFilter(dedup)

        # Emit first — allowed
        r1 = _make_record("timeout")
        filt.filter(r1)

        # Second through fourth — suppressed
        for _ in range(3):
            dedup.record("timeout")

        # After window expires, the next one should pass and contain a summary
        r_resume = logging.LogRecord(
            name="test",
            level=logging.ERROR,
            pathname="test.py",
            lineno=1,
            msg="timeout",
            args=(),
            exc_info=None,
        )
        # Force the window to expire by manipulating state timestamps
        state = dedup._state["timeout"]
        state.timestamps = []  # clear so should_log returns True

        result = filt.filter(r_resume)
        assert result is True
        assert "repeated" in str(r_resume.msg)

    def test_different_messages_tracked_independently(self) -> None:
        dedup = LogDeduplicator(window_seconds=60.0, max_per_window=1)
        filt = RateLimitedLogFilter(dedup)

        r_a = _make_record("error A")
        r_b = _make_record("error B")
        assert filt.filter(r_a) is True
        assert filt.filter(r_b) is True

        r_a2 = _make_record("error A")
        r_b2 = _make_record("error B")
        assert filt.filter(r_a2) is False
        assert filt.filter(r_b2) is False


# ---------------------------------------------------------------------------
# install_rate_limited_filter convenience
# ---------------------------------------------------------------------------


class TestInstallRateLimitedFilter:
    """Tests for the install_rate_limited_filter convenience function."""

    def test_installs_filter(self) -> None:
        name = "bernstein.test.rate_limit_install"
        target = logging.getLogger(name)
        original_count = len(target.filters)

        filt = install_rate_limited_filter(name, window=30.0, max_per_window=3)
        assert isinstance(filt, RateLimitedLogFilter)
        assert len(target.filters) == original_count + 1

        # Cleanup
        target.removeFilter(filt)
        if hasattr(target, "_bernstein_rate_limit_filter"):
            delattr(target, "_bernstein_rate_limit_filter")

    def test_idempotent(self) -> None:
        name = "bernstein.test.rate_limit_idempotent"
        target = logging.getLogger(name)

        f1 = install_rate_limited_filter(name)
        f2 = install_rate_limited_filter(name)
        assert f1 is f2
        # Only one filter added
        rate_filters = [f for f in target.filters if isinstance(f, RateLimitedLogFilter)]
        assert len(rate_filters) == 1

        # Cleanup
        target.removeFilter(f1)
        if hasattr(target, "_bernstein_rate_limit_filter"):
            delattr(target, "_bernstein_rate_limit_filter")
