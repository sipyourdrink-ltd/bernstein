"""Browser driver selection, registry, and conformance kit (#3113).

The browser boundary was designed for several drivers and admitted exactly one.
These tests prove the bar the issue sets:

* a driver is selected by registered name, and an unknown name is refused with the
  registered names listed, before a browser is started (AC1);
* the same flow over the same recorded tape under two *different* driver
  implementations produces an identical ``head_anchor`` and byte-identical
  canonical report bytes (AC2);
* the conformance kit fails deliberately broken drivers -- a stale
  ``current_url``, a DOM that lags the action, a DOM that leads it -- and names
  the violated verb (AC3);
* profile isolation holds per driver and each profile is removed at its task's
  terminal state (AC4); and
* a driver that cannot be built refuses with a typed error naming what to do,
  never an ``ImportError`` or a ``TypeError`` from a constructor (AC5).

Every negative case here is a driver the kit used to pass. A kit that only passes
good drivers proves nothing.
"""

from __future__ import annotations

import inspect
import shutil
import threading
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from bernstein.cli.main import cli
from bernstein.core.agents.computer_use import Action, ActionKind
from bernstein.core.orchestration import browser_driver as browser_driver_module
from bernstein.core.orchestration.activity_modalities import ContentStore
from bernstein.core.orchestration.browser_check import CheckKind, report_to_canonical_bytes
from bernstein.core.orchestration.browser_driver import (
    CONFORMANCE_TAPE,
    CONFORMANCE_VERBS,
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
from bernstein.core.orchestration.browser_worker import (
    BrowserBudget,
    BrowserWorker,
    CheckSpec,
    FlowStep,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path


@pytest.fixture(autouse=True)
def _restore_driver_registry() -> Iterator[None]:
    """Undo registrations a test makes.

    The registry is module-global by design -- a backend registers itself at
    import time -- so a test that registers a driver would otherwise leak it into
    every later test in the session and make ``list_drivers`` order-dependent.
    Reaching for the private dict is deliberate: there is no unregister verb on
    the public surface, and adding one just for tests would widen the API.
    """
    saved = dict(browser_driver_module._DRIVER_REGISTRY)
    yield
    browser_driver_module._DRIVER_REGISTRY.clear()
    browser_driver_module._DRIVER_REGISTRY.update(saved)


# ---------------------------------------------------------------------------
# Drivers under test
# ---------------------------------------------------------------------------


class TapeDriver:
    """A conforming driver over a fixed tape, independent of RecordedBrowserDriver.

    Deliberately not a subclass, a wrapper, or a copy of
    :class:`RecordedBrowserDriver`: it keeps its own action log and derives the
    current frame from the log's length rather than from a cursor the verbs
    mutate. Two instances of one class agree with each other no matter what that
    class does, so a parity assertion between them proves nothing; this is the
    second implementation that makes the assertion mean something.
    """

    def __init__(self, frames: Sequence[PageState], *, profile_dir: Path | None = None) -> None:
        self._frames = tuple(frames)
        self.profile_dir = profile_dir
        self._log: list[str] = []
        self.closed = False

    def _frame(self) -> PageState:
        index = len(self._log)
        if index >= len(self._frames):
            raise BrowserDriverError(f"tape exhausted at frame {index}")
        return self._frames[index]

    def navigate(self, url: str) -> None:
        self._log.append(f"navigate:{url}")

    def act(self, action: Action) -> None:
        self._log.append(f"act:{action.kind}:{action.target}")

    def screenshot(self) -> bytes:
        return self._frame().screenshot

    def dom_snapshot(self) -> bytes:
        return self._frame().dom

    def current_url(self) -> str:
        return self._frame().url

    def close(self) -> None:
        self.closed = True


class StaleUrlDriver(TapeDriver):
    """Broken driver: ``current_url`` never advances past the start frame."""

    def current_url(self) -> str:
        return self._frames[0].url


class LaggingDomDriver(TapeDriver):
    """Broken driver: ``dom_snapshot`` is frozen at the start frame.

    The DOM lags every action. This is the driver the kit has to fail: its URLs
    and screenshots are correct, so only a byte-exact DOM comparison against the
    expected frame catches it.
    """

    def dom_snapshot(self) -> bytes:
        return self._frames[0].dom


class LeadingDomDriver(TapeDriver):
    """Broken driver: ``dom_snapshot`` returns the DOM *after* the next action.

    This is AC3's second named case -- the snapshot leads the action instead of
    describing the state the decision was made against.
    """

    def dom_snapshot(self) -> bytes:
        index = min(len(self._log) + 1, len(self._frames) - 1)
        return self._frames[index].dom


class LaggingScreenshotDriver(TapeDriver):
    """Broken driver: ``screenshot`` is frozen at the start frame.

    The URL and the DOM advance correctly, so only a byte-exact comparison of the
    screenshot against the expected frame catches it. Without this case the kit's
    screenshot equality check is unconstrained: deleting it leaves the suite
    green, because every other broken driver here is caught by an earlier verb.
    """

    def screenshot(self) -> bytes:
        return self._frames[0].screenshot


class NoNavigateDriver(TapeDriver):
    """Broken driver: does not implement the ``navigate`` verb at all."""

    navigate = None  # type: ignore[assignment]


class NonIdempotentCloseDriver(TapeDriver):
    """Broken driver: ``close`` raises on the second call."""

    def close(self) -> None:
        if self.closed:
            raise RuntimeError("close called twice")
        self.closed = True


class StringDomDriver(TapeDriver):
    """Broken driver: ``dom_snapshot`` returns ``str`` where the protocol says bytes.

    An annotation is not a guarantee -- a third-party backend is only checked at
    runtime -- so the kit has to reject the wrong type rather than let it reach
    the content store and be hashed as whatever it encodes to.
    """

    def dom_snapshot(self) -> bytes:
        return self._frames[len(self._log)].dom.decode()  # type: ignore[return-value]


class EmptyScreenshotDriver(TapeDriver):
    """Broken driver: ``screenshot`` returns no bytes at all."""

    def screenshot(self) -> bytes:
        return b""


def _tape_factory(driver_cls: type[TapeDriver]) -> browser_driver_module.DriverFactory:
    """Build a registry-shaped factory for *driver_cls* over the conformance tape."""

    def factory(*, profile_dir: Path) -> BrowserDriver:
        return driver_cls(CONFORMANCE_TAPE, profile_dir=profile_dir)

    return factory


# ---------------------------------------------------------------------------
# AC3: the conformance kit fails broken drivers and names the verb
# ---------------------------------------------------------------------------


def test_kit_passes_a_conforming_recorded_driver(tmp_path: Path) -> None:
    def factory(*, profile_dir: Path) -> BrowserDriver:
        return RecordedBrowserDriver(CONFORMANCE_TAPE, profile_dir=profile_dir)

    verify_driver_conformance(factory, root_dir=tmp_path)


def test_kit_passes_the_independent_tape_driver(tmp_path: Path) -> None:
    verify_driver_conformance(_tape_factory(TapeDriver), root_dir=tmp_path)


def test_kit_fails_a_stale_url_driver(tmp_path: Path) -> None:
    with pytest.raises(ConformanceFailure) as exc_info:
        verify_driver_conformance(_tape_factory(StaleUrlDriver), root_dir=tmp_path)

    # The driver's *initial* URL is correct, so this is a genuine staleness
    # failure after navigate -- not a driver that was wrong before it moved.
    assert exc_info.value.verb == "current_url"
    assert "after navigate" in str(exc_info.value)


def test_kit_fails_a_dom_that_lags_the_action(tmp_path: Path) -> None:
    with pytest.raises(ConformanceFailure) as exc_info:
        verify_driver_conformance(_tape_factory(LaggingDomDriver), root_dir=tmp_path)

    assert exc_info.value.verb == "dom_snapshot"
    assert "after navigate" in str(exc_info.value)


def test_kit_fails_a_dom_that_leads_the_action(tmp_path: Path) -> None:
    with pytest.raises(ConformanceFailure) as exc_info:
        verify_driver_conformance(_tape_factory(LeadingDomDriver), root_dir=tmp_path)

    assert exc_info.value.verb == "dom_snapshot"
    assert "initial state" in str(exc_info.value)


def test_kit_fails_a_screenshot_that_lags_the_action(tmp_path: Path) -> None:
    """The screenshot is compared against the expected frame, not merely non-empty.

    A stale screenshot is the same class of ordering violation as a stale DOM: the
    evidence a decision was recorded against describes a state the driver has
    already left.
    """
    with pytest.raises(ConformanceFailure) as exc_info:
        verify_driver_conformance(_tape_factory(LaggingScreenshotDriver), root_dir=tmp_path)

    assert exc_info.value.verb == "screenshot"
    assert "after navigate" in str(exc_info.value)


def test_kit_fails_a_driver_returning_str_where_the_protocol_says_bytes(tmp_path: Path) -> None:
    with pytest.raises(ConformanceFailure) as exc_info:
        verify_driver_conformance(_tape_factory(StringDomDriver), root_dir=tmp_path)

    assert exc_info.value.verb == "dom_snapshot"
    assert "non-bytes" in str(exc_info.value)


def test_kit_fails_a_driver_returning_an_empty_screenshot(tmp_path: Path) -> None:
    with pytest.raises(ConformanceFailure) as exc_info:
        verify_driver_conformance(_tape_factory(EmptyScreenshotDriver), root_dir=tmp_path)

    assert exc_info.value.verb == "screenshot"
    assert "empty" in str(exc_info.value)


def test_kit_fails_a_factory_that_hands_out_one_shared_driver(tmp_path: Path) -> None:
    """Two tasks must get two drivers, not one instance handed out twice."""
    shared: list[TapeDriver] = []

    def factory(*, profile_dir: Path) -> BrowserDriver:
        if not shared:
            shared.append(TapeDriver(CONFORMANCE_TAPE, profile_dir=profile_dir))
        return shared[0]

    with pytest.raises(ConformanceFailure) as exc_info:
        verify_driver_conformance(factory, root_dir=tmp_path)

    # The second driver is the same object, already advanced past frame 0.
    assert "second driver, initial state" in str(exc_info.value)


def test_kit_fails_a_factory_whose_second_driver_is_broken(tmp_path: Path) -> None:
    """A working first session must not cover for a broken second one."""
    built = 0

    def factory(*, profile_dir: Path) -> BrowserDriver:
        nonlocal built
        built += 1
        cls = TapeDriver if built == 1 else LaggingDomDriver
        return cls(CONFORMANCE_TAPE, profile_dir=profile_dir)

    with pytest.raises(ConformanceFailure) as exc_info:
        verify_driver_conformance(factory, root_dir=tmp_path)

    assert exc_info.value.verb == "dom_snapshot"
    assert "second driver" in str(exc_info.value)


def test_kit_fails_when_only_the_second_driver_is_missing_a_verb(tmp_path: Path) -> None:
    """Both drivers are checked for the verbs, and the failure stays typed.

    Without the check the missing verb surfaces later as a raw ``TypeError``
    from the drive loop rather than a ``ConformanceFailure`` naming ``navigate``.
    """
    built = 0

    def factory(*, profile_dir: Path) -> BrowserDriver:
        nonlocal built
        built += 1
        cls = TapeDriver if built == 1 else NoNavigateDriver
        return cls(CONFORMANCE_TAPE, profile_dir=profile_dir)

    with pytest.raises(ConformanceFailure) as exc_info:
        verify_driver_conformance(factory, root_dir=tmp_path)

    assert exc_info.value.verb == "navigate"
    assert "second driver" in str(exc_info.value)


def test_kit_fails_a_driver_that_writes_into_a_sibling_profile(tmp_path: Path) -> None:
    """Driving one task must leave a concurrent task's profile untouched."""

    class SiblingWritingDriver(TapeDriver):
        def navigate(self, url: str) -> None:
            super().navigate(url)
            assert self.profile_dir is not None
            for sibling in self.profile_dir.parent.iterdir():
                if sibling != self.profile_dir:
                    (sibling / "leaked-cookie.txt").write_text("session=stolen")

    with pytest.raises(ConformanceFailure) as exc_info:
        verify_driver_conformance(_tape_factory(SiblingWritingDriver), root_dir=tmp_path / "root")

    assert exc_info.value.verb == "profile"
    assert "changed another task" in str(exc_info.value)


def test_kit_fails_a_driver_that_rewrites_a_sibling_profile_file(tmp_path: Path) -> None:
    """Non-interference means unchanged bytes, not an unchanged file listing.

    A profile that already holds a cookie jar -- every real backend writes one
    when the session opens -- can have it rewritten in place by a concurrent
    task. The directory listing is identical afterwards, so comparing names alone
    passes a driver that has just hijacked another task's session.
    """

    class SiblingRewritingDriver(TapeDriver):
        def __init__(self, frames: Sequence[PageState], *, profile_dir: Path | None = None) -> None:
            super().__init__(frames, profile_dir=profile_dir)
            assert profile_dir is not None
            (profile_dir / "cookies.txt").write_bytes(b"session=mine")

        def navigate(self, url: str) -> None:
            super().navigate(url)
            assert self.profile_dir is not None
            for sibling in self.profile_dir.parent.iterdir():
                if sibling != self.profile_dir:
                    (sibling / "cookies.txt").write_bytes(b"session=stolen")

    with pytest.raises(ConformanceFailure) as exc_info:
        verify_driver_conformance(_tape_factory(SiblingRewritingDriver), root_dir=tmp_path / "root")

    assert exc_info.value.verb == "profile"
    assert "changed another task" in str(exc_info.value)


@pytest.mark.parametrize("offending_session", [1, 2])
def test_kit_fails_a_driver_that_interferes_on_only_one_session(tmp_path: Path, offending_session: int) -> None:
    """Both drive-then-compare checks carry weight; neither is the other's mirror.

    A backend that writes outside its own profile on only one of its two sessions
    -- a first-run migration on a cold start, or a handler that only fires once
    another session exists -- is invisible to whichever of the two checks does not
    bracket it. Parametrised so removing either check fails this test.
    """
    built = 0

    class SelectiveStomper(TapeDriver):
        def __init__(self, frames: Sequence[PageState], *, profile_dir: Path | None = None) -> None:
            super().__init__(frames, profile_dir=profile_dir)
            nonlocal built
            built += 1
            self.ordinal = built

        def navigate(self, url: str) -> None:
            super().navigate(url)
            if self.ordinal != offending_session:
                return
            assert self.profile_dir is not None
            for sibling in self.profile_dir.parent.iterdir():
                if sibling != self.profile_dir:
                    (sibling / "leaked-state.txt").write_text("written by a concurrent task")

    with pytest.raises(ConformanceFailure) as exc_info:
        verify_driver_conformance(_tape_factory(SelectiveStomper), root_dir=tmp_path / "root")

    assert exc_info.value.verb == "profile"
    assert "changed another task" in str(exc_info.value)


def test_kit_closes_every_driver_even_when_one_close_raises(tmp_path: Path) -> None:
    """A leaked session is the point of the idempotency check, not a side effect."""
    closed: list[int] = []
    built = 0

    class CountingCloseDriver(TapeDriver):
        def __init__(self, frames: Sequence[PageState], *, profile_dir: Path | None = None) -> None:
            super().__init__(frames, profile_dir=profile_dir)
            nonlocal built
            built += 1
            self.ordinal = built

        def close(self) -> None:
            closed.append(self.ordinal)
            if self.ordinal == 1:
                raise RuntimeError("first driver refuses to close")
            super().close()

    with pytest.raises(ConformanceFailure) as exc_info:
        verify_driver_conformance(_tape_factory(CountingCloseDriver), root_dir=tmp_path)

    assert exc_info.value.verb == "close"
    # The second session was closed despite the first one raising.
    assert 2 in closed


def test_kit_closes_every_session_when_the_run_fails_mid_flow(tmp_path: Path) -> None:
    """A conformance failure must not leave browser sessions open."""
    closed: list[int] = []
    built = 0

    class LeakProbe(TapeDriver):
        def __init__(self, frames: Sequence[PageState], *, profile_dir: Path | None = None) -> None:
            super().__init__(frames, profile_dir=profile_dir)
            nonlocal built
            built += 1
            self.ordinal = built

        def current_url(self) -> str:
            return self._frames[0].url  # stale: fails partway through the flow

        def close(self) -> None:
            closed.append(self.ordinal)
            super().close()

    with pytest.raises(ConformanceFailure):
        verify_driver_conformance(_tape_factory(LeakProbe), root_dir=tmp_path)

    assert built == 2
    assert sorted(set(closed)) == [1, 2]


def test_kit_closes_the_first_session_when_the_factory_refuses_the_second(tmp_path: Path) -> None:
    """A factory that refuses a second session must not strand the first."""
    closed: list[int] = []
    built = 0

    class CloseProbe(TapeDriver):
        def close(self) -> None:
            closed.append(1)
            super().close()

    def factory(*, profile_dir: Path) -> BrowserDriver:
        nonlocal built
        built += 1
        if built == 2:
            raise BrowserDriverError("second session refused")
        return CloseProbe(CONFORMANCE_TAPE, profile_dir=profile_dir)

    with pytest.raises(BrowserDriverError):
        verify_driver_conformance(factory, root_dir=tmp_path)

    assert closed == [1]


def test_kit_fails_a_driver_missing_a_verb(tmp_path: Path) -> None:
    with pytest.raises(ConformanceFailure) as exc_info:
        verify_driver_conformance(_tape_factory(NoNavigateDriver), root_dir=tmp_path)

    assert exc_info.value.verb == "navigate"


def test_kit_fails_a_non_idempotent_close(tmp_path: Path) -> None:
    with pytest.raises(ConformanceFailure) as exc_info:
        verify_driver_conformance(_tape_factory(NonIdempotentCloseDriver), root_dir=tmp_path)

    assert exc_info.value.verb == "close"


def test_kit_exercises_every_protocol_verb(tmp_path: Path) -> None:
    """Every member of CONFORMANCE_VERBS is actually called, not just declared."""
    called: list[str] = []

    class RecordingDriver(TapeDriver):
        def navigate(self, url: str) -> None:
            called.append("navigate")
            super().navigate(url)

        def act(self, action: Action) -> None:
            called.append("act")
            super().act(action)

        def screenshot(self) -> bytes:
            called.append("screenshot")
            return super().screenshot()

        def dom_snapshot(self) -> bytes:
            called.append("dom_snapshot")
            return super().dom_snapshot()

        def current_url(self) -> str:
            called.append("current_url")
            return super().current_url()

        def close(self) -> None:
            called.append("close")
            super().close()

    verify_driver_conformance(_tape_factory(RecordingDriver), root_dir=tmp_path)
    assert set(CONFORMANCE_VERBS) <= set(called)


def test_kit_drives_act_with_a_non_navigation_action(tmp_path: Path) -> None:
    """The kit's ``act`` payload is pinned: a CLICK, not a disguised navigation.

    The protocol cannot make a driver's *use* of the payload observable -- ``act``
    returns nothing, and the only readback is the next frame, which a legitimate
    replay driver advances by cursor rather than by payload. What can be pinned is
    the kit's own side of the contract: ``act`` is exercised with the
    non-navigation action ``BrowserWorker`` routes through ``act`` instead of
    ``navigate``, on every driver it builds. Without this, turning the kit's
    action into a ``NAVIGATE`` would stop exercising that route silently.
    """
    seen: list[Action] = []

    class PayloadRecorder(TapeDriver):
        def act(self, action: Action) -> None:
            seen.append(action)
            super().act(action)

    verify_driver_conformance(_tape_factory(PayloadRecorder), root_dir=tmp_path)

    assert [action.kind for action in seen] == [ActionKind.CLICK, ActionKind.CLICK]
    assert {action.target for action in seen} == {"#conformance"}


def test_kit_leaves_no_profile_behind_when_a_driver_fails(tmp_path: Path) -> None:
    """A failing driver must not leak its profile directory."""
    root = tmp_path / "profiles"
    root.mkdir()
    with pytest.raises(ConformanceFailure):
        verify_driver_conformance(_tape_factory(StaleUrlDriver), root_dir=root)

    assert list(root.iterdir()) == []


def test_two_kit_invocations_on_one_root_get_their_own_directories(tmp_path: Path) -> None:
    """`BrowserProfile.allocate` is deterministic in the task id.

    Constant task ids therefore resolve two invocations to the same two
    directories, and the first invocation's teardown removes the second's live
    profiles mid-run.
    """
    seen: list[Path] = []

    class ProfileRecorder(TapeDriver):
        def __init__(self, frames: Sequence[PageState], *, profile_dir: Path | None = None) -> None:
            super().__init__(frames, profile_dir=profile_dir)
            assert profile_dir is not None
            seen.append(profile_dir)

    verify_driver_conformance(_tape_factory(ProfileRecorder), root_dir=tmp_path)
    verify_driver_conformance(_tape_factory(ProfileRecorder), root_dir=tmp_path)

    assert len(seen) == 4
    assert len(set(seen)) == 4, "two invocations shared profile directories"


def test_concurrent_kit_invocations_on_one_root_do_not_disturb_each_other(tmp_path: Path) -> None:
    """The reported harm directly: overlapping runs against a shared root."""
    errors: list[BaseException] = []
    start = threading.Barrier(4)

    def run() -> None:
        try:
            start.wait(timeout=10)
            for _ in range(5):
                verify_driver_conformance(_tape_factory(TapeDriver), root_dir=tmp_path)
        except BaseException as exc:  # reported from the main thread
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert errors == []


def test_kit_refuses_a_tape_that_is_not_three_frames(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="exactly 3 frames"):
        verify_driver_conformance(_tape_factory(TapeDriver), root_dir=tmp_path, expected_tape=CONFORMANCE_TAPE[:2])


# ---------------------------------------------------------------------------
# AC1: registry, selection, and the calling contract that makes it usable
# ---------------------------------------------------------------------------


def test_driver_registry_lists_the_built_ins() -> None:
    drivers = list_drivers()
    assert "browser_use" in drivers
    assert "playwright" in drivers
    assert "recorded" in drivers
    assert drivers == sorted(drivers)


def test_unknown_driver_raises_error_listing_what_is_registered() -> None:
    with pytest.raises(BrowserDriverError) as exc_info:
        get_driver_factory("nonexistent_driver")
    assert "Unknown browser driver 'nonexistent_driver'" in str(exc_info.value)
    assert "'browser_use'" in str(exc_info.value)


@pytest.mark.parametrize("name", list_drivers())
def test_registered_factory_binds_profile_dir_as_a_keyword(name: str) -> None:
    """Every registry entry is callable as ``factory(profile_dir=...)``.

    The activity boundary knows nothing about a backend but its name, so it can
    only call it one way. A factory that needs more than a profile directory --
    ``RecordedBrowserDriver`` needs a tape -- cannot be a registry entry: calling
    it raises ``TypeError`` from a half-applied constructor instead of refusing.
    Binding the signature proves the contract without constructing anything.
    """
    inspect.signature(get_driver_factory(name)).bind(profile_dir="/tmp/probe")


def test_registration_is_last_write_wins_including_over_a_built_in() -> None:
    """Documented, not guarded: whichever module imports last decides.

    Pinned so the behaviour is a stated property of the registry rather than an
    accident a plugin discovers by shadowing a built-in.
    """
    before = get_driver_factory("browser_use")

    def replacement(*, profile_dir: Path) -> BrowserDriver:
        return TapeDriver(CONFORMANCE_TAPE, profile_dir=profile_dir)

    register_driver("browser_use", replacement)
    assert get_driver_factory("browser_use") is replacement
    assert get_driver_factory("browser_use") is not before
    assert list_drivers().count("browser_use") == 1


def test_recorded_driver_refuses_selection_by_name_with_a_typed_error() -> None:
    factory = get_driver_factory("recorded")
    with pytest.raises(BrowserDriverError) as exc_info:
        factory(profile_dir="/tmp/probe")
    assert "--recording" in str(exc_info.value)


def test_kit_runs_against_a_factory_taken_from_the_registry(tmp_path: Path) -> None:
    """The kit and the registry agree on how a factory is called."""
    register_driver("conformance_probe", _tape_factory(TapeDriver))
    verify_driver_conformance(get_driver_factory("conformance_probe"), root_dir=tmp_path)


def test_cli_refuses_unknown_driver(tmp_path: Path) -> None:
    flow_file = tmp_path / "flow.json"
    flow_file.write_text('{"flow_id": "f1", "start_url": "https://example.com", "steps": []}')
    runner = CliRunner()
    res = runner.invoke(
        cli,
        ["activity", "browser", "run", "--flow", str(flow_file), "--run", "r1", "--driver", "unknown_driver"],
    )
    assert res.exit_code != 0
    assert "Unknown browser driver 'unknown_driver'" in res.output
    assert "'browser_use'" in res.output


def test_cli_refuses_the_recorded_driver_selected_by_name(tmp_path: Path) -> None:
    """The refusal must reach the operator, not arrive as a driver_error state.

    ``--driver recorded`` used to raise ``TypeError`` out of a half-applied
    constructor. Refusing it inside the worker instead would classify it as a
    ``driver_error`` terminal state, which never mentions the tape.
    """
    flow_file = tmp_path / "flow.json"
    flow_file.write_text('{"flow_id": "f1", "start_url": "https://example.com", "steps": []}')
    runner = CliRunner()
    res = runner.invoke(
        cli,
        ["activity", "browser", "run", "--flow", str(flow_file), "--run", "r1", "--driver", "recorded"],
    )
    assert res.exit_code != 0
    assert "--recording" in res.output
    assert "driver_error" not in res.output


@pytest.mark.parametrize("driver_name", ["unknown_driver", "playwright"])
def test_cli_refuses_driver_together_with_recording(tmp_path: Path, driver_name: str) -> None:
    """--recording must not silently win and leave an unknown --driver unrefused.

    Parametrised over a name the registry does not know and a live backend it
    does: both name the backend a second time, so the refusal cannot depend on
    whether the name happens to resolve. Accepting a resolvable live driver and
    then driving the tape would report a run as having used a backend it never
    touched.
    """
    flow_file = tmp_path / "flow.json"
    flow_file.write_text('{"flow_id": "f1", "start_url": "https://example.com", "steps": []}')
    tape_file = tmp_path / "tape.json"
    tape_file.write_text('{"frames": [{"url": "https://example.com", "screenshot_b64": "", "dom_b64": ""}]}')
    runner = CliRunner()
    res = runner.invoke(
        cli,
        [
            "activity",
            "browser",
            "run",
            "--flow",
            str(flow_file),
            "--run",
            "r1",
            "--recording",
            str(tape_file),
            "--driver",
            driver_name,
        ],
    )
    assert res.exit_code != 0
    assert "both select the backend" in res.output


# ---------------------------------------------------------------------------
# AC2: cross-driver replay parity through the worker
# ---------------------------------------------------------------------------

_PARITY_TAPE: tuple[PageState, ...] = (
    PageState(url="https://shop/", screenshot=b"png-landing", dom=b"<html>Sign in</html>"),
    PageState(url="https://shop/login", screenshot=b"png-form", dom=b"<html>Password</html>"),
    PageState(url="https://shop/home", screenshot=b"png-home", dom=b"<html>Welcome back</html>"),
)

_PARITY_STEPS: tuple[FlowStep, ...] = (
    FlowStep(
        action=Action(kind=ActionKind.NAVIGATE, target="https://shop/login"),
        checks=(CheckSpec(check_id="landing-has-signin", kind=CheckKind.DOM_CONTAINS, operand="Sign in"),),
    ),
    FlowStep(
        action=Action(kind=ActionKind.CLICK, target="#submit"),
        checks=(CheckSpec(check_id="form-has-password", kind=CheckKind.DOM_CONTAINS, operand="Password"),),
    ),
)


def _run_flow_under(driver_name: str, *, root: Path, run_id: str):
    """Drive the parity flow through the worker using the registered driver."""
    worker = BrowserWorker(
        store=ContentStore(root / "cas"),
        budget=BrowserBudget(max_steps=10),
        profile_root=root / "profiles",
    )
    factory = get_driver_factory(driver_name)
    return worker.run(
        flow_id="login-flow",
        run_id=run_id,
        stage_id="browser-0",
        start_url="https://shop/",
        steps=_PARITY_STEPS,
        driver_factory=lambda profile_dir: factory(profile_dir=profile_dir),
        final_checks=(CheckSpec(check_id="logged-in", kind=CheckKind.DOM_CONTAINS, operand="Welcome back"),),
    )


def test_two_different_drivers_over_one_tape_produce_the_same_head_anchor(tmp_path: Path) -> None:
    """AC2: parity across two *implementations*, not two instances of one class."""
    register_driver(
        "parity_recorded",
        lambda *, profile_dir: RecordedBrowserDriver(_PARITY_TAPE, profile_dir=profile_dir),
    )
    register_driver(
        "parity_tape",
        lambda *, profile_dir: TapeDriver(_PARITY_TAPE, profile_dir=profile_dir),
    )
    assert get_driver_factory("parity_recorded") is not get_driver_factory("parity_tape")

    run_a = _run_flow_under("parity_recorded", root=tmp_path / "a", run_id="run-a")
    run_b = _run_flow_under("parity_tape", root=tmp_path / "b", run_id="run-b")

    assert run_a.report.head_anchor == run_b.report.head_anchor
    assert run_a.report.head_anchor != ""
    assert report_to_canonical_bytes(run_a.report) == report_to_canonical_bytes(run_b.report)
    assert run_a.result.artifact_hash == run_b.result.artifact_hash
    assert run_a.result.evidence_set_hash == run_b.result.evidence_set_hash
    assert [s.anchor for s in run_a.report.steps] == [s.anchor for s in run_b.report.steps]
    assert [s.observation_hash for s in run_a.report.steps] == [s.observation_hash for s in run_b.report.steps]


def test_a_driver_that_observes_the_wrong_frame_breaks_parity(tmp_path: Path) -> None:
    """The parity assertion has teeth: a divergent driver diverges the anchor.

    Without this, ``test_two_different_drivers_...`` could not be distinguished
    from a tautology -- two identical implementations agree unconditionally.
    """
    register_driver(
        "parity_recorded",
        lambda *, profile_dir: RecordedBrowserDriver(_PARITY_TAPE, profile_dir=profile_dir),
    )
    register_driver(
        "parity_skewed",
        lambda *, profile_dir: TapeDriver(_PARITY_TAPE[1:] + _PARITY_TAPE[:1], profile_dir=profile_dir),
    )

    run_a = _run_flow_under("parity_recorded", root=tmp_path / "a", run_id="run-a")
    run_b = _run_flow_under("parity_skewed", root=tmp_path / "b", run_id="run-b")

    assert run_a.report.head_anchor != run_b.report.head_anchor


# ---------------------------------------------------------------------------
# AC4: profile isolation
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


def test_kit_fails_a_driver_that_reaches_outside_its_own_profile(tmp_path: Path) -> None:
    """A driver that clears the profile root takes a concurrent task's profile."""

    class ProfileStompingDriver(TapeDriver):
        def __init__(self, frames: Sequence[PageState], *, profile_dir: Path | None = None) -> None:
            super().__init__(frames, profile_dir=profile_dir)
            assert profile_dir is not None
            for sibling in profile_dir.parent.iterdir():
                if sibling != profile_dir:
                    shutil.rmtree(sibling, ignore_errors=True)

    with pytest.raises(ConformanceFailure) as exc_info:
        verify_driver_conformance(_tape_factory(ProfileStompingDriver), root_dir=tmp_path / "root")

    assert exc_info.value.verb == "profile"
    # Caught while both tasks are live, so the message names what actually
    # happened rather than blaming the teardown that ran afterwards.
    assert "does not exist" in str(exc_info.value)


def test_kit_fails_a_driver_whose_close_removes_a_concurrent_profile(tmp_path: Path) -> None:
    """Isolation must still hold at the terminal state, not only while running."""

    class StompingCloseDriver(TapeDriver):
        def close(self) -> None:
            super().close()
            assert self.profile_dir is not None
            for sibling in self.profile_dir.parent.iterdir():
                shutil.rmtree(sibling, ignore_errors=True)

    with pytest.raises(ConformanceFailure) as exc_info:
        verify_driver_conformance(_tape_factory(StompingCloseDriver), root_dir=tmp_path / "root")

    assert exc_info.value.verb == "profile"
    assert "another task" in str(exc_info.value)


# ---------------------------------------------------------------------------
# AC5: unavailable driver stays a typed refusal
# ---------------------------------------------------------------------------


def test_driver_unavailable_refusal_message() -> None:
    exc = BrowserDriverUnavailable(driver_name="browser_use", extra="browser")
    assert "browser-use>=0.7" in str(exc)
    assert "pip install" in str(exc)
    assert isinstance(exc, BrowserDriverError)
