"""Unit tests for #3113: Browser driver selection, registry, and conformance kit."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from bernstein.cli.main import cli
from bernstein.core.agents.computer_use import Action, ActionKind
from bernstein.core.orchestration.browser_driver import (
    BrowserDriver,
    BrowserDriverError,
    BrowserDriverUnavailable,
    BrowserProfile,
    ConformanceFailure,
    PageState,
    RecordedBrowserDriver,
    get_driver_factory,
    list_drivers,
    register_driver,
    verify_driver_conformance,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Broken drivers for Acceptance Criterion 3
# ---------------------------------------------------------------------------


class StaleUrlDriver:
    """Deliberately broken driver: current_url does not update after navigate."""

    def __init__(self, profile_dir: Path) -> None:
        self.profile_dir = profile_dir
        self._url = "https://example.com/initial"

    def navigate(self, url: str) -> None:
        # Ignore url, stay stale
        pass

    def act(self, action: Action) -> None:
        pass

    def screenshot(self) -> bytes:
        return b"PNG_STALE"

    def dom_snapshot(self) -> bytes:
        return b"<html>start</html>"

    def current_url(self) -> str:
        return self._url

    def close(self) -> None:
        pass


class StaleDomDriver:
    """Deliberately broken driver: dom_snapshot returns empty or wrong DOM bytes."""

    def __init__(self, profile_dir: Path) -> None:
        self.profile_dir = profile_dir
        self._url = "https://example.com/start"

    def navigate(self, url: str) -> None:
        self._url = url

    def act(self, action: Action) -> None:
        self._url = str(action.target)

    def screenshot(self) -> bytes:
        return b"PNG_OK"

    def dom_snapshot(self) -> bytes:
        # Return wrong/empty DOM
        return b""

    def current_url(self) -> str:
        return self._url

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Conformance Kit Tests
# ---------------------------------------------------------------------------


def test_conformance_kit_passes_valid_recorded_driver(tmp_path: Path) -> None:
    frames = [
        PageState(url="https://example.com/start", screenshot=b"PNG1", dom=b"<html>start</html>"),
        PageState(url="https://example.com/next", screenshot=b"PNG2", dom=b"<html>next</html>"),
    ]

    def factory(profile_dir: Path) -> BrowserDriver:
        return RecordedBrowserDriver(frames, profile_dir=profile_dir)

    verify_driver_conformance(factory, root_dir=tmp_path)


def test_conformance_kit_fails_stale_url_driver(tmp_path: Path) -> None:
    def factory(profile_dir: Path) -> BrowserDriver:
        return StaleUrlDriver(profile_dir)

    with pytest.raises(ConformanceFailure) as exc_info:
        verify_driver_conformance(factory, root_dir=tmp_path)

    assert exc_info.value.verb == "current_url"
    assert "Conformance failure in verb 'current_url'" in str(exc_info.value)


def test_conformance_kit_fails_stale_dom_driver(tmp_path: Path) -> None:
    def factory(profile_dir: Path) -> BrowserDriver:
        return StaleDomDriver(profile_dir)

    with pytest.raises(ConformanceFailure) as exc_info:
        verify_driver_conformance(factory, root_dir=tmp_path)

    assert exc_info.value.verb == "dom_snapshot"
    assert "Conformance failure in verb 'dom_snapshot'" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Driver Registry & Selection Tests (Acceptance Criterion 1)
# ---------------------------------------------------------------------------


def test_driver_registry_list_and_get() -> None:
    drivers = list_drivers()
    assert "browser_use" in drivers
    assert "recorded" in drivers

    factory = get_driver_factory("recorded")
    assert factory is RecordedBrowserDriver


def test_unknown_driver_raises_error() -> None:
    with pytest.raises(BrowserDriverError) as exc_info:
        get_driver_factory("nonexistent_driver")
    assert "Unknown browser driver 'nonexistent_driver'" in str(exc_info.value)
    assert "'browser_use'" in str(exc_info.value)


def test_cli_refuses_unknown_driver(tmp_path: Path) -> None:
    flow_file = tmp_path / "flow.json"
    flow_file.write_text('{"flow_id": "f1", "start_url": "https://example.com", "steps": []}')
    runner = CliRunner()
    res = runner.invoke(
        cli, ["activity", "browser", "run", "--flow", str(flow_file), "--run", "r1", "--driver", "unknown_driver"]
    )
    assert res.exit_code != 0
    assert (
        "Unknown browser driver 'unknown_driver'" in res.output
        or "Unknown browser driver 'unknown_driver'" in res.stderr
    )


# ---------------------------------------------------------------------------
# Cross-Driver Replay Parity (Acceptance Criterion 2)
# ---------------------------------------------------------------------------


def test_cross_driver_replay_parity(tmp_path: Path) -> None:
    frames = [
        PageState(url="https://example.com/start", screenshot=b"PNG1", dom=b"<html>start</html>"),
        PageState(url="https://example.com/start", screenshot=b"PNG1", dom=b"<html>start</html>"),
        PageState(url="https://example.com/next", screenshot=b"PNG2", dom=b"<html>next</html>"),
    ]

    # Two separate registered driver wrappers around the same tape data
    def driver_factory_a(profile_dir: Path) -> BrowserDriver:
        return RecordedBrowserDriver(frames, profile_dir=profile_dir)

    def driver_factory_b(profile_dir: Path) -> BrowserDriver:
        return RecordedBrowserDriver(frames, profile_dir=profile_dir)

    register_driver("recorded_a", driver_factory_a)
    register_driver("recorded_b", driver_factory_b)

    d1 = get_driver_factory("recorded_a")(tmp_path / "p1")
    d2 = get_driver_factory("recorded_b")(tmp_path / "p2")

    # Both navigate & act through the identical flow
    d1.navigate("https://example.com/start")
    d2.navigate("https://example.com/start")
    assert d1.current_url() == d2.current_url()

    action = Action(kind=ActionKind.NAVIGATE, target="https://example.com/next")
    d1.act(action)
    d2.act(action)

    assert d1.current_url() == d2.current_url()
    assert d1.dom_snapshot() == d2.dom_snapshot()
    assert d1.screenshot() == d2.screenshot()


# ---------------------------------------------------------------------------
# Profile Isolation (Acceptance Criterion 4)
# ---------------------------------------------------------------------------


def test_profile_isolation_per_driver(tmp_path: Path) -> None:
    profile1 = BrowserProfile.allocate(root=tmp_path, task_id="task-101")
    profile2 = BrowserProfile.allocate(root=tmp_path, task_id="task-102")

    assert profile1.profile_dir != profile2.profile_dir
    assert profile1.profile_dir.exists()
    assert profile2.profile_dir.exists()

    profile1.teardown()
    assert not profile1.profile_dir.exists()
    assert profile2.profile_dir.exists()

    profile2.teardown()
    assert not profile2.profile_dir.exists()


# ---------------------------------------------------------------------------
# Unavailable Driver Refusal (Acceptance Criterion 5)
# ---------------------------------------------------------------------------


def test_driver_unavailable_refusal_message() -> None:
    exc = BrowserDriverUnavailable(driver_name="browser_use", extra="browser")
    assert "browser-use>=0.7" in str(exc)
    assert "pip install" in str(exc)
