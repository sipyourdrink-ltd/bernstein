"""Contracts for the Copr build watcher (issue #3325).

The first end-to-end publish run submitted its build in under a second and
then sat 45 minutes watching a build that never left the Copr queue, until the
job's own ``timeout-minutes`` killed it. ``copr-cli``'s built-in watch has no
deadline of its own, so the wait outlived the budget the job had for it.

This watcher owns the deadline. A build that reaches a terminal failure inside
the window fails the job; a build still queued at the deadline is handed to
``reconcile-release.yml``, which compares the Copr version daily.

No network: the transport is injected.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "copr_build_watch.py"

BUILD_ID = 10802217


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("copr_build_watch", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mod() -> ModuleType:
    assert SCRIPT.exists(), f"{SCRIPT} must exist"
    return _load_module()


class FakeClock:
    """Monotonic clock that only advances when the watcher sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _fetcher(states: list[Any]) -> Any:
    """Return a transport replaying ``states``; an exception instance is raised."""
    remaining = list(states)

    def fetch(build_id: int) -> str:
        assert build_id == BUILD_ID
        item = remaining.pop(0) if remaining else remaining_last
        if isinstance(item, Exception):
            raise item
        return str(item)

    remaining_last = states[-1] if states else "pending"
    return fetch


def test_succeeded_build_passes(mod: ModuleType) -> None:
    clock = FakeClock()
    code = mod.watch(
        BUILD_ID,
        fetch=_fetcher(["pending", "running", "succeeded"]),
        deadline_seconds=600,
        poll_seconds=30,
        time_fn=clock.time,
        sleep_fn=clock.sleep,
    )
    assert code == 0


@pytest.mark.parametrize("state", ["canceled", "cancelled", "skipped"])
def test_terminal_failure_fails_the_job(mod: ModuleType, state: str, capsys: pytest.CaptureFixture[str]) -> None:
    """A build that ends badly must fail the job, not warn inside a green run."""
    clock = FakeClock()
    code = mod.watch(
        BUILD_ID,
        fetch=_fetcher(["pending", state]),
        deadline_seconds=600,
        poll_seconds=30,
        time_fn=clock.time,
        sleep_fn=clock.sleep,
    )
    assert code == 1

    out = capsys.readouterr().out
    assert "::error::" in out
    assert state in out
    assert str(BUILD_ID) in out


# A `failed` aggregate says only that some chroot failed, never which. Copr
# reports it whether a released Fedora target broke or rawhide churned, and
# publish.yml submits rawhide precisely because it does not gate the release.
# These pin which of those a failed aggregate is allowed to mean.

GATING_PUBLISHED = {
    "fedora-43-x86_64": "succeeded",
    "fedora-44-x86_64": "succeeded",
    "epel-9-x86_64": "succeeded",
    "epel-10-x86_64": "succeeded",
}
RAWHIDE_FAILED = {
    "fedora-rawhide-x86_64": "failed",
    "fedora-rawhide-aarch64": "failed",
}
FEDORA_45_FAILED = {
    "fedora-45-x86_64": "failed",
    "fedora-45-aarch64": "failed",
}


def _chroots(states: dict[str, str]):
    def fetch(build_id: int) -> dict[str, str]:
        assert build_id == BUILD_ID
        return dict(states)

    return fetch


def test_failure_confined_to_rawhide_does_not_hold_the_release(
    mod: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every released chroot published; only rawhide broke. That ships."""
    clock = FakeClock()
    code = mod.watch(
        BUILD_ID,
        fetch=_fetcher(["pending", "failed"]),
        fetch_chroot_states=_chroots({**GATING_PUBLISHED, **RAWHIDE_FAILED}),
        deadline_seconds=600,
        poll_seconds=30,
        time_fn=clock.time,
        sleep_fn=clock.sleep,
    )
    assert code == 0

    out = capsys.readouterr().out
    assert "::error::" not in out
    assert "::notice::" in out
    assert "fedora-rawhide-x86_64" in out


def test_failure_on_a_released_chroot_still_fails_the_job(mod: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    """A broken Fedora 44 is a broken release, whatever rawhide did."""
    clock = FakeClock()
    code = mod.watch(
        BUILD_ID,
        fetch=_fetcher(["failed"]),
        fetch_chroot_states=_chroots({**GATING_PUBLISHED, "fedora-44-x86_64": "failed", **RAWHIDE_FAILED}),
        deadline_seconds=600,
        poll_seconds=30,
        time_fn=clock.time,
        sleep_fn=clock.sleep,
    )
    assert code == 1

    out = capsys.readouterr().out
    assert "::error::" in out
    assert "fedora-44-x86_64" in out


def test_failure_confined_to_fedora_45_does_not_hold_the_release(
    mod: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every gating chroot published; only fedora-45 broke. That ships."""
    clock = FakeClock()
    code = mod.watch(
        BUILD_ID,
        fetch=_fetcher(["pending", "failed"]),
        fetch_chroot_states=_chroots({**GATING_PUBLISHED, **FEDORA_45_FAILED}),
        deadline_seconds=600,
        poll_seconds=30,
        time_fn=clock.time,
        sleep_fn=clock.sleep,
    )
    assert code == 0

    out = capsys.readouterr().out
    assert "::error::" not in out
    assert "::notice::" in out
    assert "fedora-45-x86_64" in out


def test_failure_on_fedora_45_and_rawhide_does_not_hold_the_release(
    mod: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    """Both pre-release chroots broke; the gating chroots still published."""
    clock = FakeClock()
    code = mod.watch(
        BUILD_ID,
        fetch=_fetcher(["pending", "failed"]),
        fetch_chroot_states=_chroots({**GATING_PUBLISHED, **FEDORA_45_FAILED, **RAWHIDE_FAILED}),
        deadline_seconds=600,
        poll_seconds=30,
        time_fn=clock.time,
        sleep_fn=clock.sleep,
    )
    assert code == 0

    out = capsys.readouterr().out
    assert "::error::" not in out
    assert "::notice::" in out
    assert "fedora-45-x86_64" in out
    assert "fedora-rawhide-x86_64" in out


def test_failure_with_no_gating_chroot_published_fails_the_job(mod: ModuleType) -> None:
    """Tolerating rawhide must not tolerate a build that published nothing."""
    clock = FakeClock()
    code = mod.watch(
        BUILD_ID,
        fetch=_fetcher(["failed"]),
        fetch_chroot_states=_chroots(RAWHIDE_FAILED),
        deadline_seconds=600,
        poll_seconds=30,
        time_fn=clock.time,
        sleep_fn=clock.sleep,
    )
    assert code == 1


def test_failure_with_unreadable_chroots_fails_the_job(mod: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    """A failure with no evidence about where is not something to wave through."""

    def unreadable(build_id: int) -> dict[str, str]:
        raise OSError("connection reset")

    clock = FakeClock()
    code = mod.watch(
        BUILD_ID,
        fetch=_fetcher(["failed"]),
        fetch_chroot_states=unreadable,
        deadline_seconds=600,
        poll_seconds=30,
        time_fn=clock.time,
        sleep_fn=clock.sleep,
    )
    assert code == 1
    assert "::error::" in capsys.readouterr().out


def test_build_still_queued_at_the_deadline_hands_off_to_the_reconciler(
    mod: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    """Copr's queue latency is not a release failure.

    The submission already succeeded; the outcome is unknown, not bad. Failing
    here would paint releases red for a public build queue, so the still-queued
    case is handed to the daily Copr comparison instead.
    """
    clock = FakeClock()
    code = mod.watch(
        BUILD_ID,
        fetch=_fetcher(["pending"]),
        deadline_seconds=120,
        poll_seconds=30,
        time_fn=clock.time,
        sleep_fn=clock.sleep,
    )
    assert code == 0

    out = capsys.readouterr().out
    assert "::error::" not in out
    assert "::notice::" in out
    assert "reconcile-release" in out
    assert str(BUILD_ID) in out


def test_watch_stops_at_its_own_deadline(mod: ModuleType) -> None:
    """The watch must end on its own budget rather than run until the job is
    killed - that is the defect this watcher exists for."""
    clock = FakeClock()
    mod.watch(
        BUILD_ID,
        fetch=_fetcher(["pending"]),
        deadline_seconds=120,
        poll_seconds=30,
        time_fn=clock.time,
        sleep_fn=clock.sleep,
    )
    assert clock.now <= 120


def test_transient_read_errors_do_not_end_the_watch(mod: ModuleType) -> None:
    """One failed API read is not a build failure."""
    clock = FakeClock()
    code = mod.watch(
        BUILD_ID,
        fetch=_fetcher([OSError("connection reset"), "pending", "succeeded"]),
        deadline_seconds=600,
        poll_seconds=30,
        time_fn=clock.time,
        sleep_fn=clock.sleep,
    )
    assert code == 0


def test_unreadable_state_never_reports_success_of_its_own(mod: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    """An API that never answers leaves the outcome unknown, not succeeded."""
    clock = FakeClock()
    code = mod.watch(
        BUILD_ID,
        fetch=_fetcher([OSError("connection reset")]),
        deadline_seconds=120,
        poll_seconds=30,
        time_fn=clock.time,
        sleep_fn=clock.sleep,
    )
    assert code == 0

    out = capsys.readouterr().out
    assert "::notice::" in out
    assert "reconcile-release" in out


def test_unknown_states_are_treated_as_still_running(mod: ModuleType) -> None:
    """Copr may add states; an unrecognised one must not read as terminal."""
    assert mod.classify("importing") == "pending"
    assert mod.classify("waiting") == "pending"
    assert mod.classify("some-new-state") == "pending"
    assert mod.classify("succeeded") == "success"
    assert mod.classify("failed") == "failure"
