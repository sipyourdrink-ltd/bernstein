"""Unit tests for #3115: Playwright browser driver with pinned build identity and profile isolation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from click.testing import CliRunner

from bernstein.cli.main import cli
from bernstein.core.agents.computer_use import (
    GENESIS_ANCHOR,
    Action,
    ActionKind,
    compute_action_anchor,
    compute_observation_hash,
    digest_typed_value,
)
from bernstein.core.orchestration import browser_driver as bd
from bernstein.core.orchestration.browser_check import dom_digest_of
from bernstein.core.orchestration.browser_driver import (
    UNKNOWN_BUILD_VERSION,
    BrowserDriver,
    BrowserDriverError,
    BrowserDriverUnavailable,
    BrowserProfile,
    BrowserStepTimeout,
    PageState,
    PlaywrightBrowserDriver,
    RecordedBrowserDriver,
    get_driver_factory,
    list_drivers,
    observe,
    playwright_browser_driver,
    record_tape_from_driver,
    verify_driver_conformance,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path


# ---------------------------------------------------------------------------
# Mock Playwright surface, hermetic: no native binaries, no network.
#
# The mocks mirror only the Playwright members the driver actually calls, and
# every mock records what it was handed, so a test can assert what the driver
# asked the browser to do rather than only what it returned.
# ---------------------------------------------------------------------------


class MockPlaywrightPage:
    def __init__(self, *, url: str = "https://example.com/start") -> None:
        self.url = url
        self._dom = "<html>start</html>"
        self.keyboard = MockKeyboard()
        self.filled: list[tuple[str, str]] = []
        #: Every page call the driver made, in order.
        self.calls: list[str] = []
        #: The options each ``screenshot`` call was made with.
        self.screenshot_kwargs: list[dict[str, object]] = []

    def goto(self, url: str) -> None:
        self.calls.append("goto")
        self.url = url
        self._dom = f"<html>{url.split('/')[-1]}</html>"

    def click(self, target: str) -> None:
        self.calls.append("click")
        self.url = f"https://example.com/{target}"
        self._dom = f"<html>clicked-{target}</html>"

    def fill(self, target: str, value: str) -> None:
        self.calls.append("fill")
        self.filled.append((target, value))
        self._dom = f"<html>filled-{target}-{value}</html>"

    def screenshot(self, **kwargs: object) -> bytes:
        self.calls.append("screenshot")
        self.screenshot_kwargs.append(dict(kwargs))
        return f"PNG_{self.url}".encode()

    def content(self) -> str:
        self.calls.append("content")
        return self._dom


class AnyAttributePage(MockPlaywrightPage):
    """A page that answers to any attribute name and records what was asked of it.

    A real Playwright ``Page`` does expose ``screenshot``, and near-misses like
    ``select_option`` sit one name away from an ``ActionKind`` value, so a driver
    that resolves an action with ``getattr(page, str(kind))`` can dispatch a real
    browser call with an argument shape nobody checked. Answering to everything
    turns that dispatch into something a test can see, instead of an
    ``AttributeError`` that makes the wrong code look defensive.
    """

    def __getattr__(self, name: str) -> Callable[..., None]:
        if name.startswith("_"):
            raise AttributeError(name)
        calls = self.__dict__["calls"]
        calls.append(name)

        def _recorded(*args: object, **kwargs: object) -> None:
            return None

        return _recorded


class MockKeyboard:
    def __init__(self) -> None:
        self.pressed: list[str] = []

    def press(self, key: str) -> None:
        self.pressed.append(key)


class MockBrowser:
    def __init__(self, version: str) -> None:
        self.version = version


class MockPlaywrightContext:
    def __init__(self, user_data_dir: str, *, version: str = "120.0.6099.28") -> None:
        self.user_data_dir = user_data_dir
        self.pages = [MockPlaywrightPage()]
        self.closed = False
        self.browser = MockBrowser(version) if version else None
        # A persistent context is scoped to its own user_data_dir, so a cookie
        # written here is reachable only through this object.
        self.cookies: list[dict[str, Any]] = []

    def close(self) -> None:
        self.closed = True


class MockBrowserType:
    """Records every ``launch_persistent_context`` call the factory makes."""

    def __init__(self, *, version: str = "120.0.6099.28", fail: Exception | None = None) -> None:
        self.launched: list[str] = []
        self._version = version
        self._fail = fail

    def launch_persistent_context(self, user_data_dir: str, headless: bool = True) -> MockPlaywrightContext:
        self.launched.append(user_data_dir)
        if self._fail is not None:
            raise self._fail
        return MockPlaywrightContext(user_data_dir, version=self._version)


class MockPlaywright:
    def __init__(self, browser_type: MockBrowserType) -> None:
        self.chromium = browser_type
        self.stopped = 0

    def stop(self) -> None:
        self.stopped += 1


class MockSyncPlaywright:
    """Stands in for ``playwright.sync_api.sync_playwright``."""

    def __init__(self, browser_type: MockBrowserType) -> None:
        self.instance = MockPlaywright(browser_type)

    def __call__(self) -> MockSyncPlaywright:
        return self

    def start(self) -> MockPlaywright:
        return self.instance


#: The frames the mock page above reproduces when the conformance kit drives it.
#:
#: The kit compares every read verb byte-exact against the frame the driver is
#: supposed to be sitting on, so a backend has to be pointed at a fixture that
#: reproduces *a* tape -- it cannot reproduce the kit's default one, whose bytes
#: no page emits. Frame 1's URL is what the kit navigates to and frame 2 is what
#: the mock lands on after the kit's ``CLICK #conformance``, so the three frames
#: stay distinct and an ordering violation still diverges from a frame it does
#: not match.
_MOCK_CONFORMANCE_TAPE: tuple[PageState, ...] = (
    PageState(
        url="https://example.com/start",
        screenshot=b"PNG_https://example.com/start",
        dom=b"<html>start</html>",
    ),
    PageState(
        url="https://example.com/next",
        screenshot=b"PNG_https://example.com/next",
        dom=b"<html>next</html>",
    ),
    PageState(
        url="https://example.com/#conformance",
        screenshot=b"PNG_https://example.com/#conformance",
        dom=b"<html>clicked-#conformance</html>",
    ),
)


def _install_fake_playwright(monkeypatch: pytest.MonkeyPatch, browser_type: MockBrowserType) -> MockSyncPlaywright:
    """Point the import seam at a fake backend and return it."""
    fake = MockSyncPlaywright(browser_type)
    monkeypatch.setattr(bd, "_import_playwright", lambda: fake)
    return fake


def _driver_over_mock(profile_dir: Path) -> PlaywrightBrowserDriver:
    ctx = MockPlaywrightContext(str(profile_dir))
    return PlaywrightBrowserDriver(context=ctx, page=ctx.pages[0], profile_dir=profile_dir, build_id="chromium-120.0")


def _anchor_chain(frames: Sequence[PageState], actions: Sequence[Action]) -> str:
    """Fold *frames* and *actions* into the run's head anchor.

    Mirrors ``build_step_record``: each action anchors to the observation that
    preceded it, so two sessions agree on the head only if every screenshot,
    every DOM snapshot and every action matched byte for byte.
    """
    anchor = GENESIS_ANCHOR
    for frame, action in zip(frames, actions, strict=True):
        observation = compute_observation_hash(screenshot_bytes=frame.screenshot, dom_digest=dom_digest_of(frame.dom))
        anchor = compute_action_anchor(prev_anchor=anchor, observation_hash=observation, action=action)
    return anchor


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_playwright_driver_registered() -> None:
    assert "playwright" in list_drivers()
    factory = get_driver_factory("playwright")
    assert factory is playwright_browser_driver


def test_playwright_driver_conformance(tmp_path: Path) -> None:
    def factory(profile_dir: Path) -> BrowserDriver:
        return _driver_over_mock(profile_dir)

    verify_driver_conformance(factory, root_dir=tmp_path, expected_tape=_MOCK_CONFORMANCE_TAPE)


def test_registered_factory_binds_the_profile_dir_as_a_keyword(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The registry calls a factory as ``factory(profile_dir=...)`` and nothing else.

    The activity boundary knows nothing about a backend but its name, so it can
    only call it one way. A ``profile_dir`` this factory could not accept by
    keyword would raise ``TypeError`` before the worker had a chance to classify
    it, past the worker's typed-error handling and out of the closed terminal
    state set.
    """
    _install_fake_playwright(monkeypatch, MockBrowserType())

    driver = get_driver_factory("playwright")(profile_dir=tmp_path)

    assert isinstance(driver, PlaywrightBrowserDriver)
    driver.close()


def test_registered_factory_passes_the_conformance_kit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The kit runs against the factory that is registered, not a hand-built stand-in."""
    _install_fake_playwright(monkeypatch, MockBrowserType())

    verify_driver_conformance(get_driver_factory("playwright"), root_dir=tmp_path, expected_tape=_MOCK_CONFORMANCE_TAPE)


def test_selecting_playwright_by_name_lands_on_a_terminal_state_not_a_type_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--driver playwright`` reaches the worker as a classified terminal state.

    Two calling conventions meet on this path and they are different on purpose:
    a registry entry is called ``factory(profile_dir=...)``, while
    ``BrowserWorker.run`` calls its injected ``driver_factory`` positionally. The
    CLI is what adapts one to the other, so a keyword-only factory -- which is
    what ``browser_use_driver`` already is -- is correct rather than a mismatch.

    This drives the whole chain end to end with the backend absent. The only way
    it can end is a refusal classified into the closed terminal-state set; a
    ``TypeError`` from a factory called the wrong way would escape the worker's
    typed-error handling and produce no report at all.
    """
    monkeypatch.setattr(bd, "_import_playwright", lambda: None)
    flow_file = tmp_path / "flow.json"
    flow_file.write_text(
        '{"flow_id": "f1", "start_url": "https://example.com",'
        ' "steps": [{"action": {"kind": "click", "target": "#go"}}]}'
    )

    res = CliRunner().invoke(
        cli,
        [
            "activity",
            "browser",
            "run",
            "--flow",
            str(flow_file),
            "--run",
            "r1",
            "--driver",
            "playwright",
            "--workdir",
            str(tmp_path),
        ],
    )

    assert not isinstance(res.exception, TypeError), res.exception
    assert "terminal=refused" in res.output
    assert "reason=driver_unavailable" in res.output


def test_kit_leaves_no_profile_behind_when_the_playwright_factory_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A machine without the backend must not accumulate throwaway profiles.

    The kit allocates its profiles on disk before it calls the factory, and this
    factory refuses on every machine where the backend is not installed -- the
    common case -- so a refused build has to leave the root as it found it.
    """
    monkeypatch.setattr(bd, "_import_playwright", lambda: None)

    with pytest.raises(BrowserDriverUnavailable):
        verify_driver_conformance(get_driver_factory("playwright"), root_dir=tmp_path)

    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# Action mapping
# ---------------------------------------------------------------------------


def test_key_action_presses_through_the_keyboard(tmp_path: Path) -> None:
    """A key action reaches ``page.keyboard.press`` instead of crashing on lookup."""
    ctx = MockPlaywrightContext(str(tmp_path))
    page = ctx.pages[0]
    driver = PlaywrightBrowserDriver(context=ctx, page=page, profile_dir=tmp_path)

    driver.act(Action(kind=ActionKind.KEY, target="Enter"))

    assert page.keyboard.pressed == ["Enter"]


@pytest.mark.parametrize(
    "kind", [ActionKind.SCROLL, ActionKind.SELECT, ActionKind.SUBMIT, ActionKind.WAIT, ActionKind.SCREENSHOT]
)
def test_unmapped_action_kind_is_a_typed_refusal(tmp_path: Path, kind: ActionKind) -> None:
    """An unmapped kind is refused before the page is touched at all.

    Asserting only that *something* was raised is not enough: a dynamic
    ``getattr(page, str(kind))`` also raises for names the page lacks, while
    silently succeeding for the ones it has. The page must record no call.
    """
    ctx = MockPlaywrightContext(str(tmp_path))
    page = AnyAttributePage()
    driver = PlaywrightBrowserDriver(context=ctx, page=page, profile_dir=tmp_path)

    with pytest.raises(BrowserDriverError) as exc_info:
        driver.act(Action(kind=kind, target="#target"))

    assert kind.value in str(exc_info.value)
    assert page.calls == []
    assert page.keyboard.pressed == []


def test_type_action_never_fills_the_value_digest(tmp_path: Path) -> None:
    """A type action refuses rather than typing the SHA-256 of the value into the field.

    ``Action.value_digest`` is the digest of the typed value and the raw value is
    never carried, so there is nothing to fill; writing the digest would put 64
    hex characters into the field and report the step as successful.
    """
    ctx = MockPlaywrightContext(str(tmp_path))
    page = ctx.pages[0]
    driver = PlaywrightBrowserDriver(context=ctx, page=page, profile_dir=tmp_path)
    digest = digest_typed_value("hunter2")

    with pytest.raises(BrowserDriverError) as exc_info:
        driver.act(Action(kind=ActionKind.TYPE, target="#password", value_digest=digest))

    assert page.filled == []
    assert digest not in str(exc_info.value)


def test_playwright_timeout_maps_to_step_timeout(tmp_path: Path) -> None:
    """Playwright's own TimeoutError is a deadline, not a generic driver fault.

    ``playwright.sync_api.TimeoutError`` does not derive from the builtin, so a
    bare ``except TimeoutError`` never fires and every deadline would be reported
    as FAILED instead of TIMED_OUT.
    """

    class TimeoutError(Exception):  # deliberately shadows the builtin, as the backend's class does
        pass

    TimeoutError.__module__ = "playwright._impl._errors"

    class SlowPage(MockPlaywrightPage):
        def goto(self, url: str) -> None:
            raise TimeoutError("Timeout 30000ms exceeded")

    ctx = MockPlaywrightContext(str(tmp_path))
    driver = PlaywrightBrowserDriver(context=ctx, page=SlowPage(), profile_dir=tmp_path)

    with pytest.raises(BrowserStepTimeout):
        driver.navigate("https://example.com/slow")


# ---------------------------------------------------------------------------
# Pinned build identity
# ---------------------------------------------------------------------------


def test_build_id_pins_the_launched_browser_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The build id is read off the launched browser, not defaulted by the caller."""
    _install_fake_playwright(monkeypatch, MockBrowserType(version="120.0.6099.28"))

    driver = playwright_browser_driver(profile_dir=tmp_path)

    assert driver.build_id == "chromium-120.0.6099.28"


def test_build_id_says_unknown_rather_than_inventing_a_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A backend that cannot name its version yields an unpinned marker, not a number.

    A run whose evidence cannot name its renderer is not reproducible; reporting a
    version-shaped string nobody pinned makes it look like it is.
    """
    _install_fake_playwright(monkeypatch, MockBrowserType(version=""))

    driver = playwright_browser_driver(profile_dir=tmp_path)

    assert driver.build_id == f"chromium-{UNKNOWN_BUILD_VERSION}"


# ---------------------------------------------------------------------------
# Browser-enforced profile isolation
# ---------------------------------------------------------------------------


def test_each_task_launches_against_its_own_user_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolation is enforced by the browser: each task's own directory is the user_data_dir.

    The assertion is on what the driver asked Playwright to do. A driver that
    ignored the per-task profile and launched against a shared directory would
    still return a working driver, so only the launch argument proves it.
    """
    browser_type = MockBrowserType()
    _install_fake_playwright(monkeypatch, browser_type)

    profile_a = BrowserProfile.allocate(root=tmp_path, task_id="run-1\x00stage-1\x00flow")
    profile_b = BrowserProfile.allocate(root=tmp_path, task_id="run-2\x00stage-1\x00flow")

    driver_a = playwright_browser_driver(profile_dir=profile_a.profile_dir)
    driver_b = playwright_browser_driver(profile_dir=profile_b.profile_dir)

    assert browser_type.launched == [str(profile_a.profile_dir), str(profile_b.profile_dir)]
    assert len(set(browser_type.launched)) == 2
    assert driver_a.profile_dir != driver_b.profile_dir


def test_cookies_written_by_one_task_are_unreachable_from_the_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task A's cookie jar is not reachable through task B's context.

    The two contexts are distinct only because the factory launched them against
    distinct ``user_data_dir`` values; the assertion below therefore fails if the
    driver ever collapses two tasks onto one profile.
    """
    browser_type = MockBrowserType()
    _install_fake_playwright(monkeypatch, browser_type)

    profile_a = BrowserProfile.allocate(root=tmp_path, task_id="task-a")
    profile_b = BrowserProfile.allocate(root=tmp_path, task_id="task-b")
    driver_a = playwright_browser_driver(profile_dir=profile_a.profile_dir)
    driver_b = playwright_browser_driver(profile_dir=profile_b.profile_dir)

    ctx_a = driver_a.context
    ctx_b = driver_b.context
    assert isinstance(ctx_a, MockPlaywrightContext)
    assert isinstance(ctx_b, MockPlaywrightContext)
    ctx_a.cookies.append({"name": "session_id", "value": "secret-a"})

    assert ctx_a is not ctx_b
    assert ctx_a.user_data_dir != ctx_b.user_data_dir
    assert not any(c.get("value") == "secret-a" for c in ctx_b.cookies)


# ---------------------------------------------------------------------------
# Factory failure paths
# ---------------------------------------------------------------------------


def test_missing_backend_is_a_typed_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The refusal names Playwright's install, not the other backend's.

    A single hardcoded install command would tell an operator who asked for
    Playwright to install ``browser-use``, which does not make their run work.
    """
    monkeypatch.setattr(bd, "_import_playwright", lambda: None)

    with pytest.raises(BrowserDriverUnavailable) as exc_info:
        playwright_browser_driver(profile_dir=tmp_path)

    assert "playwright>=1.40" in str(exc_info.value)
    assert "playwright install chromium" in str(exc_info.value)
    assert "browser-use" not in str(exc_info.value)


def test_launch_failure_is_a_driver_error_not_a_missing_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A browser that failed to launch is FAILED, not REFUSED with an install hint.

    ``BrowserDriverUnavailable`` maps onto REFUSED and tells the operator to run
    ``pip install``; a crashed launch of an installed backend is neither.
    """
    _install_fake_playwright(monkeypatch, MockBrowserType(fail=RuntimeError("browser exited")))

    with pytest.raises(BrowserDriverError) as exc_info:
        playwright_browser_driver(profile_dir=tmp_path)

    assert not isinstance(exc_info.value, BrowserDriverUnavailable)
    assert "pip install" not in str(exc_info.value)


def test_failed_construction_stops_the_playwright_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A refused construction leaves no Playwright node process behind."""
    fake = _install_fake_playwright(monkeypatch, MockBrowserType(fail=RuntimeError("browser exited")))

    with pytest.raises(BrowserDriverError):
        playwright_browser_driver(profile_dir=tmp_path)

    assert fake.instance.stopped == 1


def test_successful_construction_keeps_the_runtime_alive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ownership transfers to the driver: the runtime stops on close, not before."""
    fake = _install_fake_playwright(monkeypatch, MockBrowserType())

    driver = playwright_browser_driver(profile_dir=tmp_path)
    context = driver.context
    assert isinstance(context, MockPlaywrightContext)
    assert fake.instance.stopped == 0
    assert not context.closed

    driver.close()
    driver.close()
    assert fake.instance.stopped == 1
    assert context.closed


def test_unknown_browser_type_is_a_typed_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install_fake_playwright(monkeypatch, MockBrowserType())

    with pytest.raises(BrowserDriverUnavailable):
        playwright_browser_driver(profile_dir=tmp_path, browser_type="webkit")

    assert fake.instance.stopped == 1


# ---------------------------------------------------------------------------
# Evidence ordering and replay parity
# ---------------------------------------------------------------------------


def test_screenshot_captures_the_viewport_not_the_whole_document(tmp_path: Path) -> None:
    """``screenshot()`` returns viewport bytes, as the protocol declares.

    ``BrowserDriver.screenshot`` is documented as the current viewport, and the
    observation hash binds an action to the bytes the decision was made on.
    Asking Playwright for ``full_page=True`` folds off-screen document content
    into that hash, so a check bound to it moves whenever content below the fold
    changes -- content the decision never saw.
    """
    ctx = MockPlaywrightContext(str(tmp_path))
    page = ctx.pages[0]
    driver = PlaywrightBrowserDriver(context=ctx, page=page, profile_dir=tmp_path)

    driver.screenshot()

    assert page.screenshot_kwargs == [{}]


def test_evidence_ordering_pre_action_snapshot(tmp_path: Path) -> None:
    ctx = MockPlaywrightContext(str(tmp_path))
    driver = PlaywrightBrowserDriver(context=ctx, page=ctx.pages[0], profile_dir=tmp_path)

    driver.navigate("https://example.com/start")

    # Observation captured BEFORE the action.
    obs_before = observe(driver)
    assert obs_before.url == "https://example.com/start"
    assert obs_before.dom == b"<html>start</html>"

    action = Action(kind=ActionKind.CLICK, target="next_page")
    driver.act(action)

    obs_after = observe(driver)
    assert obs_after.url == "https://example.com/next_page"
    assert obs_after.dom == b"<html>clicked-next_page</html>"
    assert obs_before.dom != obs_after.dom


def test_recorded_tape_holds_the_pre_action_frame_for_every_step(tmp_path: Path) -> None:
    """Frame *i* of the tape is the state that justified step *i*, not the state after it."""
    ctx = MockPlaywrightContext(str(tmp_path))
    driver = PlaywrightBrowserDriver(context=ctx, page=ctx.pages[0], profile_dir=tmp_path)
    steps = [Action(kind=ActionKind.CLICK, target="step1"), Action(kind=ActionKind.CLICK, target="step2")]

    tape = record_tape_from_driver(driver, steps, start_url="https://example.com/start")

    assert [f.url for f in tape] == [
        "https://example.com/start",
        "https://example.com/step1",
        "https://example.com/step2",
    ]
    assert [f.dom for f in tape] == [
        b"<html>start</html>",
        b"<html>clicked-step1</html>",
        b"<html>clicked-step2</html>",
    ]
    assert all(f.screenshot == f"PNG_{f.url}".encode() for f in tape)


def test_live_and_replayed_sessions_agree_byte_for_byte_and_on_the_head_anchor(tmp_path: Path) -> None:
    """Replaying a recorded tape reproduces the live session's evidence and head anchor.

    Every frame is compared byte for byte, and the anchor chain is folded over
    both sessions: a replay that lost a screenshot, reordered the frames or
    started one step late lands on a different head anchor.
    """
    ctx = MockPlaywrightContext(str(tmp_path))
    live = PlaywrightBrowserDriver(context=ctx, page=ctx.pages[0], profile_dir=tmp_path)
    steps = [Action(kind=ActionKind.CLICK, target="step1"), Action(kind=ActionKind.CLICK, target="step2")]

    tape = record_tape_from_driver(live, steps, start_url="https://example.com/start")
    assert len(tape) == len(steps) + 1

    # The recording navigated to the start url before frame 0, so the replay is
    # already at frame 0 and must not re-issue that navigation.
    replay = RecordedBrowserDriver(tape, profile_dir=tmp_path)
    replayed: list[PageState] = [observe(replay)]
    for step in steps:
        replay.act(step)
        replayed.append(observe(replay))

    assert tuple(replayed) == tape
    anchor_actions = [*steps, Action(kind=ActionKind.SCREENSHOT)]
    assert _anchor_chain(replayed, anchor_actions) == _anchor_chain(tape, anchor_actions)
    assert _anchor_chain(tape, anchor_actions) != GENESIS_ANCHOR


def test_playwright_unavailable_typed_refusal() -> None:
    exc = BrowserDriverUnavailable(driver_name="playwright", extra="browser")
    assert "playwright>=1.40" in str(exc)
    assert "playwright install chromium" in str(exc)
