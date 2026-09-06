"""The seal records whether execution had actually stopped.

Finalization seals the journal head into the lineage spine and writes the run
receipt. Nothing recorded whether the processes the run started had exited, so
a tool that outlived its wrapper could still write into a worktree or the
integration branch after the receipt covering the run was produced — and the
record said nothing (#5272).

The distinction these tests defend is between *checked and clean* and *could
not check*. Both are `verified: False` cases when they are not clean, and only
`method` separates them; a report that collapsed them would let a platform
that cannot probe read as a run that was quiet.
"""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

from bernstein.core.config.platform_compat import IS_WINDOWS, kill_process_group, process_group_alive
from bernstein.core.orchestration import quiescence
from bernstein.core.orchestration.quiescence import (
    METHOD_PROCESS_GROUP,
    METHOD_UNSUPPORTED,
    check_quiescence,
)


@pytest.fixture(autouse=True)
def _posix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default to the POSIX path; the Windows case is asserted explicitly."""
    monkeypatch.setattr(quiescence, "IS_WINDOWS", False)


def test_no_sessions_is_verified_quiet() -> None:
    """A run that started nothing has nothing outstanding."""
    report = check_quiescence({})
    assert report.verified is True
    assert report.residual == ()
    assert report.method == METHOD_PROCESS_GROUP
    assert report.checked == 0


def test_every_group_gone_is_verified_quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(quiescence, "process_group_alive", lambda pgid: False)
    report = check_quiescence({"s-1": 100, "s-2": 200})
    assert report.verified is True
    assert report.checked == 2


def test_a_surviving_group_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    """The row an operator reads to find what was still running."""
    monkeypatch.setattr(quiescence, "process_group_alive", lambda pgid: pgid == 200)
    report = check_quiescence({"s-1": 100, "s-2": 200})
    assert report.verified is False
    assert [g.to_dict() for g in report.residual] == [{"session_id": "s-2", "pgid": 200}]
    assert report.method == METHOD_PROCESS_GROUP


def test_a_session_with_no_process_is_not_counted_as_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing to probe is not the same as probed and empty."""
    monkeypatch.setattr(quiescence, "process_group_alive", lambda pgid: False)
    report = check_quiescence({"s-1": None, "s-2": 0, "s-3": 300})
    assert report.checked == 1


def test_a_platform_without_process_groups_never_reports_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The silent true the issue rules out.

    `process_group_alive` falls back to the lead pid on Windows, which answers
    a narrower question than this record claims. Reporting `verified: True`
    off that fallback would make "we could not look" indistinguishable from
    "nothing was there".
    """
    monkeypatch.setattr(quiescence, "IS_WINDOWS", True)
    monkeypatch.setattr(quiescence, "process_group_alive", lambda pgid: False)
    report = check_quiescence({"s-1": 100})
    assert report.verified is False
    assert report.method == METHOD_UNSUPPORTED
    assert report.residual == ()
    assert report.checked == 0


def test_the_serialization_carries_the_fields_the_journal_row_needs() -> None:
    """`verified`, `residual`, `method` — the shape #5272 specifies."""
    report = check_quiescence({})
    assert set(report.to_dict()) == {"verified", "residual", "method", "checked"}


def test_residual_order_is_stable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two seals over the same state produce the same row."""
    monkeypatch.setattr(quiescence, "process_group_alive", lambda pgid: True)
    first = check_quiescence({"s-2": 200, "s-1": 100})
    second = check_quiescence({"s-1": 100, "s-2": 200})
    assert first.to_dict() == second.to_dict()
    assert [g.session_id for g in first.residual] == ["s-1", "s-2"]


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX process groups")
def test_a_real_surviving_group_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end, with a real orphan rather than a patched probe."""
    monkeypatch.setattr(quiescence, "process_group_alive", process_group_alive)
    wrapper = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import subprocess,sys;subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);sys.exit(0)",
        ],
        start_new_session=True,
    )
    try:
        wrapper.wait(timeout=30)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not process_group_alive(wrapper.pid):
            time.sleep(0.05)

        report = check_quiescence({"s-live": wrapper.pid})
        assert report.verified is False
        assert [g.pgid for g in report.residual] == [wrapper.pid]
    finally:
        kill_process_group(wrapper.pid, 9)
