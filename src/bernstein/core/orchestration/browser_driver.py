"""Driver interface and per-task profile isolation for browser activities (#2523).

The browser worker never imports a concrete browser tool. It drives a
:class:`BrowserDriver` -- four verbs (``navigate``, ``act``, ``screenshot``,
``dom_snapshot``) plus ``current_url`` and ``close`` -- so a second driver can be
added without touching the activity boundary, and so the whole determinism and
tamper-evidence suite runs against a recorded observation tape with no network.

The pieces that live here:

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
* :class:`PlaywrightBrowserDriver` -- a second live backend (#3115). It hands the
  per-task profile directory to ``launch_persistent_context`` as the browser's
  ``user_data_dir``, so isolation is enforced by the browser process rather than
  by convention on our side, and it pins the build identity it ran under.

Failure is typed, never free text. :class:`BrowserDriverUnavailable` names the
pip package to install, :class:`BrowserStepTimeout` marks a step that did not
complete, and both derive from :class:`BrowserDriverError` so the worker maps
them onto the closed
:class:`~bernstein.core.orchestration.activity.TerminalState` set.
"""

from __future__ import annotations

import hashlib
import shutil
from contextlib import ExitStack, suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast, runtime_checkable
from uuid import uuid4

from bernstein.core.agents.computer_use import Action, ActionKind

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

__all__ = [
    "UNKNOWN_BUILD_VERSION",
    "BrowserDriver",
    "BrowserDriverError",
    "BrowserDriverUnavailable",
    "BrowserProfile",
    "BrowserStepTimeout",
    "BrowserUseDriver",
    "ConformanceFailure",
    "PageState",
    "PlaywrightBrowserDriver",
    "RecordedBrowserDriver",
    "browser_use_driver",
    "get_driver_factory",
    "list_drivers",
    "observe",
    "playwright_browser_driver",
    "record_tape_from_driver",
    "register_driver",
    "verify_driver_conformance",
]

#: The bernstein extra namespace the live browser driver belongs to. Declared
#: empty in pyproject (like ``graphics``) so a default install and the project
#: lock stay lean and license-clean; the live backend is installed on demand.
BROWSER_EXTRA = "browser"

#: The pip package that backs the live ``browser-use`` driver.
BROWSER_DRIVER_PACKAGE = "browser-use>=0.7"

#: The pip package that backs the Playwright driver. Installing it is two steps:
#: the wheel does not bring a browser, so a run that stopped at the wheel refuses
#: again at launch with a message about a missing executable.
PLAYWRIGHT_DRIVER_PACKAGE = "playwright>=1.40"

#: How each live backend is installed, keyed by registered driver name. Named in
#: the typed refusal so an operator is told exactly what to install rather than
#: left to guess, and keyed by driver so a second backend does not inherit the
#: first one's install command.
_INSTALL_COMMANDS: dict[str, str] = {
    "browser_use": f"pip install '{BROWSER_DRIVER_PACKAGE}'",
    "playwright": f"pip install '{PLAYWRIGHT_DRIVER_PACKAGE}' && playwright install chromium",
}


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
    with the ``driver_unavailable`` reason code. The message names the install
    command for *the driver that was asked for* so the refusal is actionable --
    no backend is vendored via a bernstein extra, so pointing at the extra alone
    would install nothing, and quoting one backend's command for another sends
    the operator to install a package that will not make their run work.

    Attributes:
        driver_name: The driver that could not be constructed.
        extra: The bernstein extra namespace it belongs to (declared empty).
    """

    def __init__(self, *, driver_name: str, extra: str) -> None:
        self.driver_name = driver_name
        self.extra = extra
        install = _INSTALL_COMMANDS.get(driver_name)
        # A third-party backend registered from outside this module has no entry
        # here. Saying nothing beats quoting a built-in's command at it.
        hint = f" Install it with: {install}." if install is not None else ""
        super().__init__(f"Browser driver {driver_name!r} is not installed.{hint}")


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
# Optional Playwright driver
# ---------------------------------------------------------------------------


class _SyncPlaywrightManager(Protocol):
    """The slice of Playwright's sync context manager this module drives.

    ``sync_playwright()`` returns a context manager. Calling ``start()`` enters
    it without a ``with`` block, so the *driver* owns the runtime's lifetime and
    stops it on ``close`` rather than a lexical scope ending it mid-flow.
    """

    def start(self) -> object:
        """Start the Playwright runtime and return it."""
        ...


def _import_playwright() -> Callable[[], _SyncPlaywrightManager] | None:
    """Return ``playwright.sync_api.sync_playwright``, or ``None`` when absent.

    Split out so the availability probe is a single seam, the same way
    :func:`_import_browser_use` is: the refusal path is exercised without
    depending on whether the backend happens to be installed.
    """
    try:
        from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]
    except ImportError:
        return None
    # The backend ships no stubs this build resolves, so the import lands as an
    # unknown symbol. Naming the slice we use keeps the callers checked instead
    # of letting the whole factory decay into ``Any``.
    return cast("Callable[[], _SyncPlaywrightManager]", sync_playwright)


def _is_timeout_error(exc: BaseException) -> bool:
    """Report whether *exc* is a step deadline rather than a driver fault.

    ``playwright.sync_api.TimeoutError`` derives from Playwright's own ``Error``
    base, not from the builtin :class:`TimeoutError`, so an ``isinstance`` check
    against the builtin alone never fires: every Playwright deadline would reach
    the worker as :class:`BrowserDriverError` and land on ``FAILED`` instead of
    ``TIMED_OUT``. Matching the class by name and root package keeps the check
    honest without importing the optional backend at module scope.

    Args:
        exc: The exception raised by a Playwright call.

    Returns:
        ``True`` when the exception denotes an expired step deadline.
    """
    if isinstance(exc, TimeoutError):
        return True
    cls = type(exc)
    return cls.__name__ == "TimeoutError" and cls.__module__.partition(".")[0] == "playwright"


#: The action kinds :meth:`PlaywrightBrowserDriver.act` maps onto a concrete
#: Playwright call. The rest are refused at the boundary rather than guessed at,
#: so the implemented set is a closed, readable list instead of whatever the page
#: object happens to expose under a matching attribute name.
_PLAYWRIGHT_ACTION_KINDS: frozenset[ActionKind] = frozenset({ActionKind.NAVIGATE, ActionKind.CLICK, ActionKind.KEY})

#: Build-identity component used when the backend cannot name its own version.
#: Deliberately not version-shaped: a run whose evidence cannot name the renderer
#: that produced it has to say so, not report a plausible number nobody pinned.
UNKNOWN_BUILD_VERSION = "unknown"


class PlaywrightBrowserDriver:
    """Live driver backed by Playwright with per-task profile isolation (#3115).

    Constructed through :func:`playwright_browser_driver`, which is where the
    isolation and the refusal live: it launches the context against the per-task
    profile directory as the browser's ``user_data_dir`` and turns a missing
    backend into :class:`BrowserDriverUnavailable`. Constructing this class
    directly binds it to whatever context the caller already has.

    The driver owns what the factory started. :meth:`close` closes the context and
    stops the Playwright runtime, and is idempotent as the protocol requires.

    Args:
        context: The Playwright ``BrowserContext`` the session runs in.
        page: The page within *context* the verbs act on.
        profile_dir: The isolated per-task profile directory the context was
            launched against. Kept so isolation assertions can read it back.
        build_id: The build identity of the browser behind *context*, as
            ``"<browser_type>-<version>"``. Recorded on the driver; not yet
            carried into the anchored activity report.
        playwright_obj: The Playwright runtime to stop on close, when this driver
            owns one.
    """

    def __init__(
        self,
        context: object,
        page: object,
        *,
        profile_dir: Path,
        build_id: str = UNKNOWN_BUILD_VERSION,
        playwright_obj: object | None = None,
    ) -> None:
        self.context = context
        self.page = page
        self.profile_dir = profile_dir
        self.build_id = build_id
        self._playwright_obj = playwright_obj
        self.closed = False

    def _invoke(self, label: str, method: Callable[..., object], *args: object, **kwargs: object) -> object:
        """Call *method*, mapping any failure onto the typed driver errors."""
        try:
            return method(*args, **kwargs)
        except Exception as exc:
            if _is_timeout_error(exc):
                raise BrowserStepTimeout(f"Playwright {label} timed out") from exc
            raise BrowserDriverError(f"Playwright {label} failed: {type(exc).__name__}") from exc

    def _call_page(self, name: str, *args: object, **kwargs: object) -> object:
        """Invoke a page method by name, mapping any failure onto a typed error."""
        method = getattr(self.page, name, None)
        if method is None:
            raise BrowserDriverError(f"Playwright page does not expose {name!r}")
        return self._invoke(name, method, *args, **kwargs)

    def navigate(self, url: str) -> None:
        """Navigate to *url*."""
        self._call_page("goto", url)

    def _press_key(self, key: str) -> None:
        """Press *key* through the page keyboard."""
        press = getattr(getattr(self.page, "keyboard", None), "press", None)
        if press is None:
            raise BrowserDriverError("Playwright page does not expose 'keyboard.press'")
        self._invoke("keyboard.press", press, key)

    def act(self, action: Action) -> None:
        """Map *action* onto a Playwright call, or refuse with a typed error.

        Only the kinds whose Playwright call is fully determined by
        ``(kind, target)`` are mapped. Every other kind raises
        :class:`BrowserDriverError` rather than resolving
        ``getattr(page, str(action.kind))``: a dynamic lookup turns ``select`` or
        ``submit`` into whatever page attribute happens to share the name, with
        an argument shape nobody checked, and an unmapped kind then fails
        somewhere inside the browser instead of at this boundary.

        ``TYPE`` is refused deliberately. The anchored action vocabulary carries
        only :attr:`~bernstein.core.agents.computer_use.Action.value_digest` --
        the SHA-256 of the typed value, never the keystrokes -- so the text to
        fill does not exist at this boundary. Filling the digest would type 64
        hex characters into the field and call it a successful step.

        Args:
            action: The canonicalised action to perform.

        Raises:
            BrowserDriverError: When the kind has no determined Playwright call.
            BrowserStepTimeout: When the underlying call exceeded its deadline.
        """
        if action.kind is ActionKind.NAVIGATE:
            self.navigate(action.target)
            return
        if action.kind is ActionKind.CLICK:
            self._call_page("click", action.target)
            return
        if action.kind is ActionKind.KEY:
            self._press_key(action.target)
            return
        if action.kind is ActionKind.TYPE:
            raise BrowserDriverError(
                "Playwright driver cannot perform a 'type' action: the anchored action carries only "
                "value_digest, never the typed value, so there is no text to fill."
            )
        raise BrowserDriverError(
            f"Playwright driver does not implement action kind {action.kind.value!r}. "
            f"Implemented kinds: {', '.join(sorted(k.value for k in _PLAYWRIGHT_ACTION_KINDS))}."
        )

    def screenshot(self) -> bytes:
        """Return the current viewport screenshot bytes.

        The viewport, not the whole document: the observation hash binds a
        decision to the bytes it was made on, and ``full_page=True`` would fold
        content below the fold into that hash.
        """
        raw = self._call_page("screenshot")
        return raw if isinstance(raw, bytes) else str(raw).encode("utf-8")

    def dom_snapshot(self) -> bytes:
        """Return the serialized DOM bytes."""
        raw = self._call_page("content")
        if isinstance(raw, bytes):
            return raw
        return raw.encode("utf-8") if isinstance(raw, str) else str(raw).encode("utf-8")

    def current_url(self) -> str:
        """Return the current page URL."""
        return str(getattr(self.page, "url", ""))

    def close(self) -> None:
        """Close the context and stop the runtime this driver owns. Idempotent."""
        if self.closed:
            return
        self.closed = True
        close_ctx = getattr(self.context, "close", None)
        if close_ctx is not None:
            with suppress(Exception):
                close_ctx()
        if self._playwright_obj is not None:
            stop_pw = getattr(self._playwright_obj, "stop", None)
            if stop_pw is not None:
                with suppress(Exception):
                    stop_pw()


def _playwright_build_id(context: object, *, browser_type: str) -> str:
    """Return the pinned build identity of the browser behind *context*."""
    browser_obj = getattr(context, "browser", None)
    version = str(getattr(browser_obj, "version", "") or "").strip() if browser_obj is not None else ""
    return f"{browser_type}-{version or UNKNOWN_BUILD_VERSION}"


def _stop_quietly(target: object, method: str) -> None:
    """Best-effort teardown used while unwinding a failed construction."""
    call = getattr(target, method, None)
    if call is not None:
        with suppress(Exception):
            call()


def playwright_browser_driver(
    *,
    profile_dir: Path,
    browser_type: str = "chromium",
    headless: bool = True,
) -> PlaywrightBrowserDriver:
    """Build the Playwright driver bound to *profile_dir*, or refuse with a typed error.

    The profile directory is handed to ``launch_persistent_context`` as the
    browser's ``user_data_dir``, so the cookie jar and local storage are scoped
    by the browser process itself rather than by convention on our side.

    Everything started here is owned by the returned driver. If any later step
    fails, the runtime and the context are torn down before the error leaves, so
    a refused construction does not strand a Playwright node process.

    *profile_dir* is keyword-only and the remaining arguments are defaulted, so
    the function satisfies the registry calling contract -- every registered
    factory is invoked as ``factory(profile_dir=...)`` and nothing else -- while
    still being usable directly with a different browser type.

    Args:
        profile_dir: The isolated per-task profile directory to launch against.
        browser_type: The Playwright browser type attribute to launch.
        headless: Whether to launch the browser headless.

    Returns:
        A :class:`PlaywrightBrowserDriver` bound to *profile_dir*.

    Raises:
        BrowserDriverUnavailable: When the backend is not installed, or does not
            expose *browser_type*. Mapped onto ``REFUSED``.
        BrowserDriverError: When the backend is installed but the runtime or the
            browser failed to start. A browser that crashed on launch is not an
            uninstalled backend, so the two never collapse onto one terminal
            state and onto one install instruction the operator does not need.
    """
    sync_pw = _import_playwright()
    if sync_pw is None:
        raise BrowserDriverUnavailable(driver_name="playwright", extra=BROWSER_EXTRA)
    try:
        pw: object = sync_pw().start()
    except Exception as exc:
        raise BrowserDriverError(f"Playwright runtime failed to start: {type(exc).__name__}") from exc

    with ExitStack() as stack:
        stack.callback(_stop_quietly, pw, "stop")
        b_type = getattr(pw, browser_type, None)
        if b_type is None:
            raise BrowserDriverUnavailable(driver_name="playwright", extra=BROWSER_EXTRA)
        try:
            context = b_type.launch_persistent_context(str(profile_dir), headless=headless)
        except Exception as exc:
            raise BrowserDriverError(f"Playwright {browser_type} failed to launch: {type(exc).__name__}") from exc
        stack.callback(_stop_quietly, context, "close")
        try:
            pages = tuple(getattr(context, "pages", ()) or ())
            page = pages[0] if pages else context.new_page()
        except Exception as exc:
            raise BrowserDriverError(f"Playwright {browser_type} exposed no usable page: {type(exc).__name__}") from exc
        driver = PlaywrightBrowserDriver(
            context=context,
            page=page,
            profile_dir=profile_dir,
            build_id=_playwright_build_id(context, browser_type=browser_type),
            playwright_obj=pw,
        )
        # Construction succeeded: the driver owns the context and the runtime now,
        # so the unwind callbacks must not also close them.
        stack.pop_all()
    return driver


def record_tape_from_driver(
    driver: BrowserDriver,
    steps: Sequence[Action],
    *,
    start_url: str | None = None,
) -> tuple[PageState, ...]:
    """Record a tape of :class:`PageState` frames from a live driver session.

    One observation is captured before each action in *steps*, plus one after the
    final action, so the tape holds ``len(steps) + 1`` frames and frame *i* is
    exactly the state that justified ``steps[i]``. That is the ordering
    :class:`RecordedBrowserDriver` replays and the one the worker anchors, so a
    tape recorded here replays to the same head anchor as the live session.

    The optional *start_url* navigation happens *before* the first observation
    and is not itself a frame: it puts the session at the flow's starting state
    rather than recording a step. A replay therefore starts at frame 0 already
    and must not re-issue that navigation, or it advances the tape by one and
    reads every state one step late.

    Args:
        driver: The live driver to read.
        steps: The actions to issue, in order.
        start_url: When set, navigated to before the first observation.

    Returns:
        The recorded frames, in capture order, ready for
        :class:`RecordedBrowserDriver`.
    """
    if start_url is not None:
        driver.navigate(start_url)

    frames: list[PageState] = [observe(driver)]
    for action in steps:
        driver.act(action)
        frames.append(observe(driver))
    return tuple(frames)


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
register_driver("playwright", playwright_browser_driver)
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
