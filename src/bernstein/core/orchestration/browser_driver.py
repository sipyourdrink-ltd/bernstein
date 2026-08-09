"""Driver interface and per-task profile isolation for browser activities (#2523).

The browser worker never imports a concrete browser tool. It drives a
:class:`BrowserDriver` -- four verbs (``navigate``, ``act``, ``screenshot``,
``dom_snapshot``) plus ``current_url`` and ``close`` -- so a second driver can be
added without touching the activity boundary, and so the whole determinism and
tamper-evidence suite runs against a recorded observation tape with no network.

Three pieces live here:

* :class:`BrowserDriver` -- the protocol, and :func:`observe`, the helper that
  folds one driver's three read verbs into a single :class:`PageState`.
* :class:`RecordedBrowserDriver` -- a driver over a fixed tape of recorded
  observations. This is what makes replay determinism *checkable*: re-running a
  flow over the same tape must reproduce a byte-identical action sequence, and any
  divergence is a hash mismatch at an exact index rather than a flaky assertion.
* :class:`BrowserProfile` -- per-task profile isolation. Each task's profile
  directory is derived from its task id -- the scheduler's ``(run_id, stage_id)``
  coordinates, composed by the worker -- so two concurrent browser tasks hold
  disjoint directories by construction (no allocator, no shared counter, no
  coordination) even when they drive the same flow document, and the profile is
  torn down when the task reaches a terminal state so no cookie survives into
  another task.

Failure is typed, never free text. :class:`BrowserDriverUnavailable` names the
pip package to install, :class:`BrowserStepTimeout` marks a step that did not
complete, and both derive from :class:`BrowserDriverError` so the worker maps
them onto the closed
:class:`~bernstein.core.orchestration.activity.TerminalState` set.
"""

from __future__ import annotations

import hashlib
import shutil
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable
from uuid import uuid4

from bernstein.core.agents.computer_use import Action, ActionKind

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

__all__ = [
    "BrowserDriver",
    "BrowserDriverError",
    "BrowserDriverUnavailable",
    "BrowserProfile",
    "BrowserStepTimeout",
    "BrowserUseDriver",
    "ConformanceFailure",
    "PageState",
    "RecordedBrowserDriver",
    "browser_use_driver",
    "get_driver_factory",
    "list_drivers",
    "observe",
    "register_driver",
    "verify_driver_conformance",
]

#: The bernstein extra namespace the live browser driver belongs to. Declared
#: empty in pyproject (like ``graphics``) so a default install and the project
#: lock stay lean and license-clean; the live backend is installed on demand.
BROWSER_EXTRA = "browser"

#: The pip package that backs the live driver. Named in the typed refusal so an
#: operator is told exactly what to install rather than left to guess.
BROWSER_DRIVER_PACKAGE = "browser-use>=0.7"


# ---------------------------------------------------------------------------
# Typed failures
# ---------------------------------------------------------------------------


class BrowserDriverError(RuntimeError):
    """A browser driver could not complete a step.

    The worker maps this onto :attr:`~bernstein.core.orchestration.activity.TerminalState.FAILED`
    with the ``driver_error`` reason code, so a driver fault is a closed terminal
    state rather than a free-text log line.
    """


class BrowserStepTimeout(BrowserDriverError):
    """A browser step did not complete inside its deadline.

    Mapped onto :attr:`~bernstein.core.orchestration.activity.TerminalState.TIMED_OUT`
    with the ``driver_timeout`` reason code.
    """


class BrowserDriverUnavailable(BrowserDriverError):
    """The requested driver is not installed.

    Mapped onto :attr:`~bernstein.core.orchestration.activity.TerminalState.REFUSED`
    with the ``driver_unavailable`` reason code. The message names the pip package
    to install so the refusal is actionable -- the backend is not vendored via a
    bernstein extra, so pointing at the extra alone would install nothing.

    Attributes:
        driver_name: The driver that could not be constructed.
        extra: The bernstein extra namespace it belongs to (declared empty).
    """

    def __init__(self, *, driver_name: str, extra: str) -> None:
        self.driver_name = driver_name
        self.extra = extra
        super().__init__(
            f"Browser driver {driver_name!r} is not installed. Install it with: pip install '{BROWSER_DRIVER_PACKAGE}'."
        )


# ---------------------------------------------------------------------------
# Observation primitive
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PageState:
    """One observed browser state: the exact bytes behind a decision.

    Attributes:
        url: The page URL at observation time (provenance only -- never hashed
            into an anchor, so a redirect chain cannot silently change identity).
        screenshot: The exact screenshot bytes the worker saw.
        dom: The exact DOM / accessibility snapshot bytes the worker saw.
    """

    url: str
    screenshot: bytes
    dom: bytes


@runtime_checkable
class BrowserDriver(Protocol):
    """The four-verb surface a browser activity drives.

    Deliberately small: a concrete driver maps its own vocabulary onto
    :class:`~bernstein.core.agents.computer_use.ActionKind` before handing an
    action here, so the boundary stays driver-agnostic. Every verb either
    succeeds or raises a :class:`BrowserDriverError` subclass -- there is no
    partial-success or string-status return.
    """

    def navigate(self, url: str) -> None:
        """Navigate to *url*."""

    def act(self, action: Action) -> None:
        """Perform *action* against the current page."""

    def screenshot(self) -> bytes:
        """Return the current viewport screenshot bytes."""

    def dom_snapshot(self) -> bytes:
        """Return the current DOM / accessibility snapshot bytes."""

    def current_url(self) -> str:
        """Return the current page URL."""

    def close(self) -> None:
        """Release the driver. Must be idempotent."""


def observe(driver: BrowserDriver) -> PageState:
    """Capture one :class:`PageState` from *driver*.

    The screenshot and the DOM snapshot are read back-to-back so both describe
    the same decision point; the worker hashes them together into the step's
    observation hash.

    Args:
        driver: The driver to read.

    Returns:
        The observed :class:`PageState`.
    """
    return PageState(url=driver.current_url(), screenshot=driver.screenshot(), dom=driver.dom_snapshot())


# ---------------------------------------------------------------------------
# Recorded tape driver
# ---------------------------------------------------------------------------


class RecordedBrowserDriver:
    """A driver over a fixed tape of recorded observations.

    Each action advances the tape by one frame; the read verbs always return the
    current frame. Because the tape is fixed, a flow driven over it is a pure
    function of the flow definition and the recording, which is what makes
    "re-running the same flow against the same recorded observations reproduces a
    byte-identical action sequence and verdict" a testable property rather than a
    claim. It is also the offline replay driver: a recorded run replays with the
    network disabled.

    Args:
        frames: The recorded observations, in capture order.
        profile_dir: The per-task profile directory the driver was handed. Kept
            so isolation assertions can read it back; the tape itself holds no
            profile state.
        timeout_at_step: When set, the action at this zero-based step index
            raises :class:`BrowserStepTimeout` instead of advancing. Used to
            exercise the typed timeout path without a real deadline.
    """

    def __init__(
        self,
        frames: Sequence[PageState],
        *,
        profile_dir: Path | None = None,
        timeout_at_step: int | None = None,
    ) -> None:
        self._frames = tuple(frames)
        self.profile_dir = profile_dir
        self._timeout_at_step = timeout_at_step
        self._cursor = 0
        self._acted = 0
        self.closed = False

    def _frame(self) -> PageState:
        if self._cursor >= len(self._frames):
            raise BrowserDriverError(f"recorded tape exhausted at frame {self._cursor}")
        return self._frames[self._cursor]

    def _advance(self) -> None:
        if self._timeout_at_step is not None and self._acted == self._timeout_at_step:
            raise BrowserStepTimeout(f"recorded step {self._acted} timed out")
        if self._cursor + 1 >= len(self._frames):
            raise BrowserDriverError(f"recorded tape exhausted after {self._cursor + 1} frames")
        self._cursor += 1
        self._acted += 1

    def navigate(self, url: str) -> None:
        """Advance the tape as a navigation step."""
        self._advance()

    def act(self, action: Action) -> None:
        """Advance the tape as an action step."""
        self._advance()

    def screenshot(self) -> bytes:
        """Return the current frame's screenshot bytes."""
        return self._frame().screenshot

    def dom_snapshot(self) -> bytes:
        """Return the current frame's DOM bytes."""
        return self._frame().dom

    def current_url(self) -> str:
        """Return the current frame's URL."""
        return self._frame().url

    def close(self) -> None:
        """Mark the driver closed. Idempotent."""
        self.closed = True


# ---------------------------------------------------------------------------
# Per-task profile isolation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BrowserProfile:
    """A per-task browser profile directory.

    The directory name is ``sha256(task_id)[:16]``, so two tasks hold disjoint
    directories by construction: no allocator, no shared counter, and no way for
    one task's cookie jar or local storage to land inside another's. The task id
    is opaque here; the browser worker composes it from the scheduler's
    ``(run_id, stage_id)`` coordinates so two runs of the same flow never collide.
    Allocation is deterministic, so the same task id resolves to the same
    directory across processes -- which is what lets a supervisor tear a profile
    down after a crash.

    Attributes:
        task_id: The task the profile belongs to.
        profile_dir: The isolated directory the driver runs against.
    """

    task_id: str
    profile_dir: Path

    @classmethod
    def allocate(cls, *, root: Path, task_id: str) -> BrowserProfile:
        """Create (or reuse) the isolated profile directory for *task_id*.

        Args:
            root: The directory per-task profiles live under.
            task_id: The task the profile belongs to.

        Returns:
            The allocated :class:`BrowserProfile`.
        """
        slug = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:16]
        profile_dir = root / slug
        profile_dir.mkdir(parents=True, exist_ok=True)
        return cls(task_id=task_id, profile_dir=profile_dir)

    @property
    def cookie_jar_path(self) -> Path:
        """The profile-local cookie jar. Never shared between tasks."""
        return self.profile_dir / "cookies.txt"

    def teardown(self) -> None:
        """Remove the profile tree. Idempotent, so a double teardown is safe."""
        shutil.rmtree(self.profile_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Optional live driver
# ---------------------------------------------------------------------------


def _import_browser_use() -> object | None:
    """Return the optional ``browser_use`` module, or ``None`` when absent.

    Split out so the availability probe is a single seam: the refusal path is
    exercised without depending on whether the extra happens to be installed.
    """
    try:
        import browser_use  # type: ignore[import-not-found]
    except ImportError:
        return None
    return browser_use


class BrowserUseDriver:
    """Live driver backed by the optional ``browser-use`` package.

    Constructed through :func:`browser_use_driver` so the missing-extra path is a
    typed refusal rather than an ``ImportError`` surfacing from inside a run. The
    driver holds a per-task profile directory, so concurrent tasks never share
    cookies or local storage.

    Args:
        session: The underlying ``browser-use`` session object.
        profile_dir: The isolated per-task profile directory.
    """

    def __init__(self, session: object, *, profile_dir: Path) -> None:
        self._session = session
        self.profile_dir = profile_dir

    def _call(self, name: str, *args: object) -> object:
        """Invoke a session method, mapping any failure onto a typed error."""
        method = getattr(self._session, name, None)
        if method is None:
            raise BrowserDriverError(f"browser-use session does not expose {name!r}")
        try:
            return method(*args)
        except TimeoutError as exc:
            raise BrowserStepTimeout(f"browser-use {name} timed out") from exc
        except Exception as exc:
            raise BrowserDriverError(f"browser-use {name} failed: {type(exc).__name__}") from exc

    def navigate(self, url: str) -> None:
        """Navigate the live session to *url*."""
        self._call("go_to_url", url)

    def act(self, action: Action) -> None:
        """Map *action* onto the live session's vocabulary and perform it."""
        if action.kind is ActionKind.NAVIGATE:
            self.navigate(action.target)
            return
        self._call("perform", str(action.kind), action.target)

    def screenshot(self) -> bytes:
        """Return the live viewport screenshot bytes."""
        raw = self._call("screenshot")
        return raw if isinstance(raw, bytes) else str(raw).encode("utf-8")

    def dom_snapshot(self) -> bytes:
        """Return the live accessibility-tree snapshot bytes."""
        raw = self._call("get_accessibility_tree")
        return raw if isinstance(raw, bytes) else str(raw).encode("utf-8")

    def current_url(self) -> str:
        """Return the live session's current URL."""
        return str(self._call("get_current_url"))

    def close(self) -> None:
        """Close the live session, ignoring a already-closed session."""
        closer = getattr(self._session, "close", None)
        if closer is None:
            return
        try:
            closer()
        except Exception:
            return


def browser_use_driver(*, profile_dir: Path) -> BrowserUseDriver:
    """Build the live ``browser-use`` driver, or refuse with a typed error.

    Args:
        profile_dir: The isolated per-task profile directory the session runs in.

    Returns:
        A :class:`BrowserUseDriver` bound to *profile_dir*.

    Raises:
        BrowserDriverUnavailable: When the ``browser-use`` backend is not
            installed.
    """
    module = _import_browser_use()
    if module is None:
        raise BrowserDriverUnavailable(driver_name="browser_use", extra=BROWSER_EXTRA)
    factory = getattr(module, "BrowserSession", None)
    if factory is None:
        raise BrowserDriverUnavailable(driver_name="browser_use", extra=BROWSER_EXTRA)
    return BrowserUseDriver(factory(user_data_dir=str(profile_dir)), profile_dir=profile_dir)


# ---------------------------------------------------------------------------
# Driver Registry
# ---------------------------------------------------------------------------

#: A registered driver factory. Every entry is called with exactly one keyword
#: argument, ``profile_dir`` -- the shape :func:`browser_use_driver` already has
#: -- so the activity boundary can build any registered driver the same way and
#: a new backend is a registry entry rather than a branch in the CLI.
type DriverFactory = Callable[..., BrowserDriver]

_DRIVER_REGISTRY: dict[str, DriverFactory] = {}


def register_driver(name: str, factory: DriverFactory) -> None:
    """Register a browser driver factory under *name*.

    Registration is last-write-wins and silent: a later call under an existing
    name replaces the earlier factory, including the built-ins. The registry is
    module-global and populated at import time, so whichever module imports last
    decides. There is no unregister verb.

    Args:
        name: The name ``--driver`` selects the backend by.
        factory: Builds a driver bound to an isolated profile. It is called as
            ``factory(profile_dir=...)`` and nothing else, so every registered
            entry is interchangeable at the activity boundary. A factory that
            needs more than a profile directory must refuse with a
            :class:`BrowserDriverError` rather than expose a constructor that
            raises :class:`TypeError` when the boundary calls it.
    """
    _DRIVER_REGISTRY[name] = factory


def list_drivers() -> list[str]:
    """Return a sorted list of registered driver names."""
    return sorted(_DRIVER_REGISTRY.keys())


def get_driver_factory(name: str) -> DriverFactory:
    """Return the driver factory for *name*.

    Args:
        name: The registered driver name.

    Returns:
        The factory, callable as ``factory(profile_dir=...)``.

    Raises:
        BrowserDriverError: When *name* is not registered. The message lists the
            registered names, so the refusal happens before a browser is started
            and tells the operator what they could have asked for.
    """
    if name not in _DRIVER_REGISTRY:
        avail = ", ".join(repr(n) for n in list_drivers())
        raise BrowserDriverError(f"Unknown browser driver {name!r}. Registered drivers: {avail}.")
    return _DRIVER_REGISTRY[name]


#: The registered name of the recorded-tape driver.
RECORDED_DRIVER_NAME = "recorded"

#: Why the recorded driver cannot be built from its name alone. Shared so the
#: activity boundary refuses the selection up front with the same wording the
#: factory raises with if something builds it anyway -- a refusal that arrives as
#: a ``driver_error`` terminal state tells the operator nothing about the tape.
RECORDED_DRIVER_REFUSAL = (
    "Browser driver 'recorded' replays a recorded observation tape and cannot be selected "
    "by name alone. Pass the tape instead: --recording <path>."
)


def _recorded_driver_needs_a_tape(*, profile_dir: Path) -> BrowserDriver:
    """Refuse to build :class:`RecordedBrowserDriver` from a name alone.

    The recorded driver replays a fixed observation tape, so unlike a live
    backend it cannot be constructed from a profile directory alone -- it is
    selected by handing the tape in, not by naming it. Registered anyway so the
    name stays discoverable in :func:`list_drivers` and selecting it is a typed
    refusal instead of a :class:`TypeError` escaping a partially applied
    constructor.

    Args:
        profile_dir: Accepted to match the registry calling contract; unused.

    Raises:
        BrowserDriverError: Always.
    """
    raise BrowserDriverError(RECORDED_DRIVER_REFUSAL)


# Register built-in drivers by default
register_driver("browser_use", browser_use_driver)
register_driver(RECORDED_DRIVER_NAME, _recorded_driver_needs_a_tape)


# ---------------------------------------------------------------------------
# Driver Conformance Kit
# ---------------------------------------------------------------------------


#: The six protocol members a conforming driver exposes.
CONFORMANCE_VERBS: tuple[str, ...] = ("navigate", "act", "screenshot", "dom_snapshot", "current_url", "close")

#: The fixed observation tape :func:`verify_driver_conformance` drives by
#: default: the start state, the state after ``navigate``, and the state after
#: ``act``. Three *distinct* frames, deliberately -- every read verb is compared
#: byte-exact against the frame the driver is supposed to be sitting on, so a
#: driver whose snapshot lags or leads the action (a DOM frozen at the start, or
#: the post-action DOM returned before the action) diverges from a frame it does
#: not match and fails at the verb that read it. A tape whose frames repeat would
#: let an ordering violation pass unnoticed.
CONFORMANCE_TAPE: tuple[PageState, ...] = (
    PageState(url="https://example.com/start", screenshot=b"PNG-start", dom=b"<html>start</html>"),
    PageState(url="https://example.com/next", screenshot=b"PNG-next", dom=b"<html>next</html>"),
    PageState(url="https://example.com/final", screenshot=b"PNG-final", dom=b"<html>final</html>"),
)


class ConformanceFailure(AssertionError):
    """Raised when a driver fails the conformance kit.

    Attributes:
        verb: The protocol member that failed, or ``"profile"`` for a profile
            isolation violation. Named so a failure points at the surface to fix
            rather than at the kit.
    """

    def __init__(self, verb: str, message: str) -> None:
        self.verb = verb
        super().__init__(f"Conformance failure in verb {verb!r}: {message}")


def _expect_frame(driver: BrowserDriver, expected: PageState, *, phase: str) -> None:
    """Assert the driver's three read verbs all describe *expected*.

    Args:
        driver: The driver under test.
        expected: The frame the driver is supposed to be sitting on.
        phase: Where in the flow this read happens, for the failure message.

    Raises:
        ConformanceFailure: Naming the read verb that disagreed.
    """
    url = driver.current_url()
    if url != expected.url:
        raise ConformanceFailure("current_url", f"{phase}: expected {expected.url!r}, got {url!r}")

    dom = driver.dom_snapshot()
    if not isinstance(dom, bytes) or not dom:
        raise ConformanceFailure("dom_snapshot", f"{phase}: returned an empty or non-bytes snapshot")
    if dom != expected.dom:
        raise ConformanceFailure("dom_snapshot", f"{phase}: expected {expected.dom!r}, got {dom!r}")

    shot = driver.screenshot()
    if not isinstance(shot, bytes) or not shot:
        raise ConformanceFailure("screenshot", f"{phase}: returned an empty or non-bytes screenshot")
    if shot != expected.screenshot:
        raise ConformanceFailure("screenshot", f"{phase}: expected {expected.screenshot!r}, got {shot!r}")


def _drive_tape(driver: BrowserDriver, expected_tape: Sequence[PageState], *, which: str) -> None:
    """Drive one driver through the fixed flow, checking every observation point.

    Args:
        driver: The driver to drive.
        expected_tape: The three frames it is expected to reproduce.
        which: Which of the kit's two drivers this is, for the failure message.

    Raises:
        ConformanceFailure: Naming the verb that disagreed.
    """
    _expect_frame(driver, expected_tape[0], phase=f"{which} driver, initial state")
    driver.navigate(expected_tape[1].url)
    _expect_frame(driver, expected_tape[1], phase=f"{which} driver, after navigate")
    driver.act(Action(kind=ActionKind.CLICK, target="#conformance"))
    _expect_frame(driver, expected_tape[2], phase=f"{which} driver, after act")


def _close_idempotently(driver: BrowserDriver) -> Exception | None:
    """Close *driver* twice, returning the failure instead of raising it.

    Returned rather than raised so one driver's non-idempotent ``close`` cannot
    leave a sibling session open or mask a conformance failure already in flight.
    """
    try:
        driver.close()
        driver.close()
    except Exception as exc:
        return exc
    return None


def _profile_fingerprint(path: Path) -> dict[str, str]:
    """Fingerprint every entry under *path* by name *and* content.

    A set of names is not enough to say a directory is unchanged: a profile that
    already holds a cookie jar can have it rewritten in place, which leaves the
    listing identical and the session hijacked. Files are hashed, so "unchanged"
    means unchanged bytes. Empty when *path* is absent.
    """
    if not path.exists():
        return {}
    fingerprint: dict[str, str] = {}
    for child in path.rglob("*"):
        key = str(child.relative_to(path))
        if child.is_dir():
            fingerprint[key] = "dir"
        else:
            try:
                fingerprint[key] = f"file:{hashlib.sha256(child.read_bytes()).hexdigest()}"
            except OSError:
                fingerprint[key] = "unreadable"
    return fingerprint


def verify_driver_conformance(
    driver_factory: DriverFactory,
    *,
    root_dir: Path,
    expected_tape: Sequence[PageState] = CONFORMANCE_TAPE,
) -> None:
    """Run the driver conformance suite against *driver_factory*.

    Drives a fixed flow -- observe, ``navigate``, observe, ``act``, observe --
    over a fixed three-frame tape and compares every read verb byte-exact against
    the frame the driver should be sitting on at that point. That is what makes
    the kit able to fail an ordering violation: a driver whose ``dom_snapshot``
    lags or leads the action returns bytes belonging to a different frame.

    What is asserted:

    * all six members of :data:`CONFORMANCE_VERBS` are present and callable;
    * ``current_url``, ``dom_snapshot`` and ``screenshot`` match the expected
      frame at each of the three observation points;
    * ``navigate`` and ``act`` each advance the driver by exactly one frame;
    * ``close`` is idempotent, as the protocol requires, and each driver is
      closed even when a sibling's ``close`` raises; and
    * profile isolation, to the extent the six-verb protocol makes it
      observable -- see below.

    Two drivers are built and *both* are driven through the whole flow. A factory
    that hands out a shared or stateful instance fails, because the second driver
    is then already past the start frame; a backend whose second session is
    broken can no longer hide behind a first session that works.

    What the profile checks do and do not prove. The kit asserts that two tasks
    are allocated disjoint directories, that both exist while both drivers are
    live, that driving one task leaves the other's directory byte-for-byte
    unchanged, and that tearing one down does not remove the other. It cannot
    prove a backend *uses* the directory it was handed: nothing in the six-verb
    protocol exposes where a driver puts its state, so a backend that ignores
    ``profile_dir`` and writes to a fixed location outside ``root_dir`` passes.
    Non-interference inside the profile root is the strongest claim available
    here; anything more has to be asserted by the backend's own tests.

    What the action check does not prove either. ``act`` returns nothing, and the
    only readback is the next frame -- which a legitimate replay driver advances
    by cursor, not by payload. A driver that discards the ``kind`` and ``target``
    it is handed and simply advances therefore passes, and requiring otherwise
    would fail :class:`RecordedBrowserDriver`, which exists to replay a tape.
    Payload handling has to be asserted by the backend's own tests; what is
    pinned here is that ``act`` is exercised with a non-navigation action, the
    route ``BrowserWorker`` sends everything but ``NAVIGATE`` down.

    *driver_factory* is called as ``factory(profile_dir=...)``, the registry
    calling contract, so a registered backend can be handed to the kit directly.
    Being callable is not the same as passing: the kit compares against a fixed
    tape, so a live backend has to be pointed at a fixture that reproduces one.

    Args:
        driver_factory: Builds a driver bound to a profile directory.
        root_dir: Where the kit allocates its throwaway profiles.
        expected_tape: The three frames the driver is expected to reproduce.

    Raises:
        ConformanceFailure: When the driver violates the protocol.
        ValueError: When *expected_tape* does not hold exactly three frames --
            a caller error in the kit's own arguments, not a driver fault.
    """
    if len(expected_tape) != 3:
        raise ValueError(f"conformance tape must hold exactly 3 frames, got {len(expected_tape)}")

    # Per-invocation task ids. BrowserProfile.allocate is deterministic in the
    # task id -- which is what the worker wants, so a supervisor can find a
    # crashed task's profile -- but it means two verifier invocations sharing a
    # root_dir would resolve to the same two directories and tear down each
    # other's live profiles mid-run. The nonce keeps the two ids distinct within
    # an invocation and unique across invocations; nothing here is anchored, so
    # the profile paths carry no determinism requirement.
    nonce = uuid4().hex
    profile_a = BrowserProfile.allocate(root=root_dir, task_id=f"conformance-{nonce}-task-a")
    profile_b = BrowserProfile.allocate(root=root_dir, task_id=f"conformance-{nonce}-task-b")
    close_error: Exception | None = None
    # Every driver the factory hands back, recorded as it is built. A session
    # opened before a failure -- a conformance failure mid-flow, or a factory
    # that refuses the second build -- still has to be closed on the way out.
    built: list[BrowserDriver] = []
    try:
        driver = driver_factory(profile_dir=profile_a.profile_dir)
        built.append(driver)
        other = driver_factory(profile_dir=profile_b.profile_dir)
        built.append(other)

        for name, subject in (("first", driver), ("second", other)):
            for verb in CONFORMANCE_VERBS:
                if not callable(getattr(subject, verb, None)):
                    raise ConformanceFailure(verb, f"the {name} driver does not expose the verb as a callable")

        # Two live tasks off one factory must not share a profile directory.
        if profile_a.profile_dir == profile_b.profile_dir:
            raise ConformanceFailure("profile", "two tasks were handed the same profile directory")
        if not profile_a.profile_dir.exists() or not profile_b.profile_dir.exists():
            raise ConformanceFailure("profile", "an allocated profile directory does not exist")

        # Both drivers are driven, not just the first. A factory that hands out a
        # shared or stateful instance shows up as a second driver that is already
        # past the start frame, and a backend whose second session is broken can
        # no longer hide behind a first session that works.
        sibling_before = _profile_fingerprint(profile_b.profile_dir)
        _drive_tape(driver, expected_tape, which="first")
        if _profile_fingerprint(profile_b.profile_dir) != sibling_before:
            raise ConformanceFailure("profile", "driving one task changed another task's profile directory")

        sibling_before = _profile_fingerprint(profile_a.profile_dir)
        _drive_tape(other, expected_tape, which="second")
        if _profile_fingerprint(profile_a.profile_dir) != sibling_before:
            raise ConformanceFailure("profile", "driving one task changed another task's profile directory")

        # close must be idempotent. Each driver is closed independently: a
        # session that leaks because a sibling's close raised is exactly the
        # failure this check exists to catch. The first error is reported after
        # teardown so it cannot mask a live conformance failure.
        for subject in (driver, other):
            error = _close_idempotently(subject)
            if error is not None and close_error is None:
                close_error = error

        # Terminal state for task A: its profile goes, task B's stays.
        profile_a.teardown()
        if profile_a.profile_dir.exists():
            raise ConformanceFailure("profile", "the profile directory survived its task's teardown")
        if not profile_b.profile_dir.exists():
            raise ConformanceFailure("profile", "tearing down one task's profile removed another task's")
        profile_b.teardown()
        if profile_b.profile_dir.exists():
            raise ConformanceFailure("profile", "the profile directory survived its task's teardown")
    finally:
        # Close every session on every exit path. On the success path each was
        # already closed twice above, and close is required to be idempotent, so
        # this is a no-op there; on a failure path it is the only close that
        # runs. Errors are suppressed because a non-conforming close must not
        # replace the failure that is already being reported.
        for subject in built:
            with suppress(Exception):
                subject.close()
        # Teardown is idempotent, so this is a safety net for the early-exit
        # paths above and a no-op once the isolation assertions have run.
        profile_a.teardown()
        profile_b.teardown()

    if close_error is not None:
        raise ConformanceFailure("close", f"close is not idempotent: {type(close_error).__name__}") from close_error
