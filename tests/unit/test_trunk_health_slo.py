import json
from datetime import UTC, datetime
from unittest.mock import patch
from urllib.parse import quote

from scripts import trunk_health_slo
from scripts.trunk_health_slo import (
    MIN_SAMPLE_SIZE,
    marker_should_open,
    score_runs,
    trunk_is_red_now,
)


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


# ---------------------------------------------------------------------------
# The andon decision
# ---------------------------------------------------------------------------


def test_one_red_run_does_not_hold_the_repo() -> None:
    """The incident this guard exists for.

    A `docker compose version` probe timed out on a contended runner, the
    resulting single red run scored 1/19 = 5.26%, and the 5% threshold
    opened a marker that held every merge in the repo until it aged out of
    the 24h window. At these sample sizes a percentage threshold below
    1/MIN_SAMPLE_SIZE is a zero-tolerance gate wearing a rate's clothes.
    """
    assert marker_should_open(total=19, red=1, red_pct=5, threshold_pct=5, trunk_red_now=True) is False


def test_a_second_red_run_does_hold_the_repo() -> None:
    """Positive control: the gate must still fire on a trunk that is red.

    Without this the test above is satisfied by a function that never opens
    a marker at all.
    """
    assert marker_should_open(total=19, red=2, red_pct=10, threshold_pct=5, trunk_red_now=True) is True


def test_reds_under_the_threshold_do_not_hold_the_repo() -> None:
    """Two reds are necessary, not sufficient - the rate still has to cross."""
    assert marker_should_open(total=100, red=2, red_pct=2, threshold_pct=5, trunk_red_now=True) is False


def test_a_thin_sample_never_holds_the_repo() -> None:
    """Below MIN_SAMPLE_SIZE there is no rate to speak of."""
    assert marker_should_open(
        total=MIN_SAMPLE_SIZE - 1,
        red=MIN_SAMPLE_SIZE - 1,
        red_pct=100,
        threshold_pct=5,
        trunk_red_now=True,
    ) is False


def _run(conclusion: str, created_at: str) -> dict[str, str]:
    return {"conclusion": conclusion, "created_at": created_at}


def test_latest_verdict_is_the_newest_completed_run() -> None:
    runs = [
        _run("success", "2026-08-26T20:00:00Z"),
        _run("failure", "2026-08-26T11:00:00Z"),
        _run("cancelled", "2026-08-26T21:00:00Z"),
    ]
    assert trunk_is_red_now(runs) is False


def test_latest_verdict_reads_red_when_the_newest_completed_run_failed() -> None:
    runs = [
        _run("failure", "2026-08-26T20:00:00Z"),
        _run("success", "2026-08-26T11:00:00Z"),
    ]
    assert trunk_is_red_now(runs) is True


def test_marker_stays_shut_when_trunk_is_green_again() -> None:
    """Two infrastructure failures held every merge in the repository for a
    full day while main itself was green: the rate alone latches on history.
    The andon exists to stop merges piling onto a broken main, so a main whose
    newest run is green releases it.
    """
    assert marker_should_open(
        total=27, red=2, red_pct=7, threshold_pct=5, trunk_red_now=False
    ) is False


def test_marker_opens_when_the_rate_is_over_and_trunk_is_red_now() -> None:
    assert marker_should_open(
        total=27, red=2, red_pct=7, threshold_pct=5, trunk_red_now=True
    ) is True
