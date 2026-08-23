import json
from datetime import UTC, datetime
from unittest.mock import patch
from urllib.parse import quote

from scripts import trunk_health_slo
from scripts.trunk_health_slo import MIN_SAMPLE_SIZE, score_runs


def test_score_runs_counts_only_failures():
    runs = [
        {"conclusion": "success"},
        {"conclusion": "failure"},
        {"conclusion": "timed_out"},
        {"conclusion": "success"},
        {"conclusion": "cancelled"},  # excluded
        {"conclusion": "skipped"},  # excluded
        {"conclusion": None},  # excluded (in-progress)
    ]
    total, red, red_pct = score_runs(runs)
    # 7 input, 3 excluded -> 4 total
    # 2 red (failure, timed_out)
    # 2/4 = 50%
    assert total == 4
    assert red == 2
    assert red_pct == 50


def test_score_runs_empty():
    runs = []
    total, red, red_pct = score_runs(runs)
    assert total == 0
    assert red == 0
    assert red_pct == 0


def test_score_runs_all_success():
    runs = [
        {"conclusion": "success"},
        {"conclusion": "success"},
    ]
    total, red, red_pct = score_runs(runs)
    assert total == 2
    assert red == 0
    assert red_pct == 0


def test_score_runs_integer_floor():
    # 1 red out of 3 total = 33.33% -> should floor to 33%
    runs = [
        {"conclusion": "failure"},
        {"conclusion": "success"},
        {"conclusion": "success"},
    ]
    total, red, red_pct = score_runs(runs)
    assert total == 3
    assert red == 1
    assert red_pct == 33


def test_insufficient_sample_boundary():
    # MIN_SAMPLE_SIZE - 1 should be insufficient (handled by main, but test the score)
    runs = [{"conclusion": "success"} for _ in range(MIN_SAMPLE_SIZE - 1)]
    total, red, red_pct = score_runs(runs)
    assert total == MIN_SAMPLE_SIZE - 1
    assert red == 0
    assert red_pct == 0


class _FakeResponse:
    """Minimal stand-in for the object `urlopen` yields."""

    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode()

    def read(self, *args: object) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _page(n: int) -> dict:
    return {"workflow_runs": [{"conclusion": "success"} for _ in range(n)]}


def test_fetch_paginates_until_a_short_page() -> None:
    """A window wider than one page must not be silently truncated at 100 runs."""
    pages = [_page(100), _page(100), _page(7)]
    calls: list[str] = []

    def _fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        return _FakeResponse(pages[len(calls) - 1])

    with patch.object(trunk_health_slo, "urlopen", _fake_urlopen):
        runs = trunk_health_slo.fetch_ci_runs("o/r", "t", datetime(2026, 8, 20, tzinfo=UTC))

    assert len(runs) == 207
    assert len(calls) == 3
    assert [f"page={n}" in c for n, c in zip((1, 2, 3), calls, strict=True)] == [True, True, True]


def test_fetch_stops_on_an_empty_page() -> None:
    """An exactly-100-run final page is followed by an empty one, not an endless loop."""
    pages = [_page(100), _page(0)]
    calls: list[str] = []

    def _fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        return _FakeResponse(pages[len(calls) - 1])

    with patch.object(trunk_health_slo, "urlopen", _fake_urlopen):
        runs = trunk_health_slo.fetch_ci_runs("o/r", "t", datetime(2026, 8, 20, tzinfo=UTC))

    assert len(runs) == 100
    assert len(calls) == 2


def test_fetch_bounds_the_window_server_side_on_the_ci_workflow() -> None:
    """The time window is a query parameter, so the 100-run page cap cannot clip it."""
    captured: list[str] = []

    def _fake_urlopen(req, timeout=None):
        captured.append(req.full_url)
        return _FakeResponse(_page(1))

    with patch.object(trunk_health_slo, "urlopen", _fake_urlopen):
        trunk_health_slo.fetch_ci_runs("o/r", "t", datetime(2026, 8, 20, 3, 4, 5, tzinfo=UTC))

    url = captured[0]
    assert "/actions/workflows/ci.yml/runs?" in url
    assert quote(">=2026-08-20T03:04:05Z", safe="") in url
    assert "branch=main" in url


def test_fetch_passes_a_timeout() -> None:
    """A hung socket must fail the step, not hold the runner to its job timeout."""
    seen: list[object] = []

    def _fake_urlopen(req, timeout=None):
        seen.append(timeout)
        return _FakeResponse(_page(0))

    with patch.object(trunk_health_slo, "urlopen", _fake_urlopen):
        trunk_health_slo.fetch_ci_runs("o/r", "t", datetime(2026, 8, 20, tzinfo=UTC))

    assert seen == [trunk_health_slo._HTTP_TIMEOUT_S]
