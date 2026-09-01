"""Unit tests for repository flow stagnation detection.

Covers :mod:`bernstein.core.observability.stagnation`:

* work-ready rule — ``open_prs > 0`` AND ``mergeable is True``
* left-inclusive window boundary — a sample exactly at
  ``latest_ts - window_s`` is counted
* stagnation flagging when every in-window sample is work-ready
* no false positive when work is not ready (no open PRs, or
  unmergeable PRs)
* no false positive when a merge occurred (``mergeable`` flips
  True → False)
* insufficient data (empty, single sample) returns ``None``
* evidence_rows faithfully preserve the deciding samples
"""

from __future__ import annotations

from bernstein.core.observability.stagnation import (
    RepositoryFlowSample,
    StagnationFinding,
    detect_stagnation,
)


def _sample(ts: float, open_prs: int = 1, mergeable: bool = True) -> RepositoryFlowSample:
    return RepositoryFlowSample(timestamp=ts, open_prs=open_prs, mergeable=mergeable)


# ---------------------------------------------------------------------------
# Stagnation detection
# ---------------------------------------------------------------------------


def test_flags_sustained_work_ready() -> None:
    samples = [_sample(100.0), _sample(110.0), _sample(120.0)]
    finding = detect_stagnation(samples, window_s=30.0)
    assert finding is not None
    assert isinstance(finding, StagnationFinding)
    assert finding.sample_count == 3
    assert finding.evidence_rows == tuple(samples)
    assert finding.window_start == 100.0
    assert finding.window_end == 120.0


def test_no_work_ready_when_no_open_prs() -> None:
    samples = [_sample(100.0, open_prs=0), _sample(110.0, open_prs=0), _sample(120.0, open_prs=0)]
    assert detect_stagnation(samples, window_s=30.0) is None


def test_no_work_ready_when_not_mergeable() -> None:
    samples = [_sample(100.0, mergeable=False), _sample(110.0, mergeable=False)]
    assert detect_stagnation(samples, window_s=30.0) is None


def test_older_non_work_ready_outside_window_is_irrelevant() -> None:
    # The window only considers samples in [latest_ts - window_s, latest_ts].
    # The older sample at 1.0 is outside the window, so stagnation is detected.
    samples = [_sample(1.0, mergeable=False), _sample(100.0), _sample(120.0)]
    finding = detect_stagnation(samples, window_s=30.0)
    assert finding is not None
    assert finding.sample_count == 2
    assert [s.timestamp for s in finding.evidence_rows] == [100.0, 120.0]


def test_merge_breaks_stagnation() -> None:
    # The True → False flip means the PR was merged mid-window.
    samples = [_sample(100.0), _sample(110.0), _sample(120.0, mergeable=False)]
    assert detect_stagnation(samples, window_s=30.0) is None


# ---------------------------------------------------------------------------
# Window boundary
# ---------------------------------------------------------------------------


def test_window_requires_at_least_two_samples() -> None:
    assert detect_stagnation([_sample(100.0)], window_s=30.0) is None


def test_empty_samples_is_none() -> None:
    assert detect_stagnation([], window_s=30.0) is None


def test_only_latest_window_counts_stagnation() -> None:
    # Two samples inside the window, one older sample outside.
    samples = [_sample(50.0, open_prs=0), _sample(100.0), _sample(120.0)]
    finding = detect_stagnation(samples, window_s=30.0)
    assert finding is not None
    assert finding.sample_count == 2
    assert [s.timestamp for s in finding.evidence_rows] == [100.0, 120.0]
    # The older non-work-ready sample must not have poisoned the window.
    assert finding.window_start == 100.0


def test_left_boundary_is_inclusive() -> None:
    # latest_ts = 130.0, window_s = 30.0 → cutoff = 100.0 exactly.
    # The sample at 100.0 must be included (left-inclusive boundary).
    samples = [_sample(100.0), _sample(115.0), _sample(130.0)]
    finding = detect_stagnation(samples, window_s=30.0)
    assert finding is not None
    assert finding.sample_count == 3
    assert finding.window_start == 100.0


def test_just_outside_window_is_excluded() -> None:
    # cutoff = 100.0, sample at 99.0 falls just outside.
    samples = [_sample(99.0), _sample(100.0), _sample(130.0)]
    finding = detect_stagnation(samples, window_s=30.0)
    assert finding is not None
    assert finding.sample_count == 2
    assert [s.timestamp for s in finding.evidence_rows] == [100.0, 130.0]


# ---------------------------------------------------------------------------
# Evidence and serialization
# ---------------------------------------------------------------------------


def test_finding_to_dict_round_trip() -> None:
    samples = [_sample(100.0), _sample(120.0)]
    finding = detect_stagnation(samples, window_s=30.0)
    assert finding is not None
    d = finding.to_dict()
    assert d["window_start"] == 100.0
    assert d["window_end"] == 120.0
    assert d["sample_count"] == 2
    assert d["evidence_rows"][0] == {
        "timestamp": 100.0,
        "open_prs": 1,
        "mergeable": True,
    }


def test_untouched_samples_are_preserved() -> None:
    rows = [_sample(100.0), _sample(105.0), _sample(110.0), _sample(120.0)]
    finding = detect_stagnation(rows, window_s=25.0)
    assert finding is not None
    assert finding.evidence_rows == tuple(rows)


def test_deterministic_same_series_twice_produces_equal_findings() -> None:
    samples = [_sample(100.0), _sample(115.0), _sample(130.0)]
    first = detect_stagnation(samples, window_s=30.0)
    second = detect_stagnation(samples, window_s=30.0)
    assert first is not None
    assert second is not None
    assert first == second


def test_merge_flips_true_to_false_between_consecutive_samples() -> None:
    samples = [_sample(100.0), _sample(110.0), _sample(120.0, mergeable=False)]
    assert detect_stagnation(samples, window_s=30.0) is None
