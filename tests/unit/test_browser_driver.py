"""Browser driver interface: recorded tape, profile isolation, typed refusals (#2523).

The worker fronts a :class:`~bernstein.core.orchestration.browser_driver.BrowserDriver`
so the activity boundary never imports a concrete browser tool. These tests pin
the three properties the boundary depends on:

* a recorded observation tape drives a flow with no network, so every replay and
  determinism assertion in the suite runs offline;
* per-task profiles are disjoint by construction and tear down on terminal state,
  so two concurrent browser tasks cannot bleed cookies into one another; and
* a missing optional driver is a *typed* refusal carrying the extra to install,
  never free text.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.agents.computer_use import Action, ActionKind
from bernstein.core.orchestration.browser_driver import (
    BrowserDriverError,
    BrowserDriverUnavailable,
    BrowserProfile,
    BrowserStepTimeout,
    PageState,
    RecordedBrowserDriver,
    browser_use_driver,
    observe,
)


def _tape() -> tuple[PageState, ...]:
    return (
        PageState(url="https://shop/", screenshot=b"png-0", dom=b"<html>Sign in</html>"),
        PageState(url="https://shop/login", screenshot=b"png-1", dom=b"<html>Password</html>"),
        PageState(url="https://shop/home", screenshot=b"png-2", dom=b"<html>Welcome back</html>"),
    )


# ---------------------------------------------------------------------------
# recorded tape driver
# ---------------------------------------------------------------------------


def test_recorded_driver_serves_observations_in_tape_order() -> None:
    driver = RecordedBrowserDriver(_tape())
    assert observe(driver) == _tape()[0]
    driver.navigate("https://shop/login")
    assert observe(driver) == _tape()[1]
    driver.act(Action(kind=ActionKind.CLICK, target="#submit"))
    assert observe(driver) == _tape()[2]


def test_recorded_driver_screenshot_and_dom_match_current_frame() -> None:
    driver = RecordedBrowserDriver(_tape())
    assert driver.screenshot() == b"png-0"
    assert driver.dom_snapshot() == b"<html>Sign in</html>"
    assert driver.current_url() == "https://shop/"


def test_recorded_driver_refuses_to_advance_past_the_tape() -> None:
    driver = RecordedBrowserDriver(_tape()[:1])
    with pytest.raises(BrowserDriverError, match="exhausted"):
        driver.act(Action(kind=ActionKind.CLICK, target="#next"))


def test_recorded_driver_can_be_scripted_to_time_out() -> None:
    driver = RecordedBrowserDriver(_tape(), timeout_at_step=1)
    driver.act(Action(kind=ActionKind.CLICK, target="#a"))
    with pytest.raises(BrowserStepTimeout):
        driver.act(Action(kind=ActionKind.CLICK, target="#b"))


def test_recorded_driver_close_is_idempotent() -> None:
    driver = RecordedBrowserDriver(_tape())
    driver.close()
    driver.close()
    assert driver.closed


# ---------------------------------------------------------------------------
# per-task profile isolation
# ---------------------------------------------------------------------------


def test_profiles_for_two_tasks_are_disjoint(tmp_path: Path) -> None:
    first = BrowserProfile.allocate(root=tmp_path, task_id="task-a")
    second = BrowserProfile.allocate(root=tmp_path, task_id="task-b")
    assert first.profile_dir != second.profile_dir
    assert first.profile_dir.is_dir()
    assert second.profile_dir.is_dir()


def test_profile_allocation_is_deterministic_for_a_task(tmp_path: Path) -> None:
    first = BrowserProfile.allocate(root=tmp_path, task_id="task-a")
    again = BrowserProfile.allocate(root=tmp_path, task_id="task-a")
    assert first.profile_dir == again.profile_dir


def test_cookies_written_in_one_profile_are_invisible_to_the_other(tmp_path: Path) -> None:
    first = BrowserProfile.allocate(root=tmp_path, task_id="task-a")
    second = BrowserProfile.allocate(root=tmp_path, task_id="task-b")
    first.cookie_jar_path.write_text("session=secret-a", encoding="utf-8")
    assert not second.cookie_jar_path.exists()
    assert second.profile_dir not in first.profile_dir.parents
    assert first.profile_dir not in second.profile_dir.parents


def test_teardown_removes_the_profile_tree(tmp_path: Path) -> None:
    profile = BrowserProfile.allocate(root=tmp_path, task_id="task-a")
    profile.cookie_jar_path.write_text("session=secret", encoding="utf-8")
    profile.teardown()
    assert not profile.profile_dir.exists()


def test_teardown_is_idempotent(tmp_path: Path) -> None:
    profile = BrowserProfile.allocate(root=tmp_path, task_id="task-a")
    profile.teardown()
    profile.teardown()
    assert not profile.profile_dir.exists()


# ---------------------------------------------------------------------------
# optional driver refusal
# ---------------------------------------------------------------------------


def test_missing_browser_extra_raises_a_typed_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("bernstein.core.orchestration.browser_driver._import_browser_use", lambda: None)
    with pytest.raises(BrowserDriverUnavailable) as excinfo:
        browser_use_driver(profile_dir=tmp_path)
    refusal = excinfo.value
    assert refusal.driver_name == "browser_use"
    assert refusal.extra == "browser"
    # The refusal names the pip package to install (the backend is not vendored
    # via a bernstein extra) rather than leaving the operator to guess.
    assert "browser-use" in str(refusal)
    assert "pip install" in str(refusal)
