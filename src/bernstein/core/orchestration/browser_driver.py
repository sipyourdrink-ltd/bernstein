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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from bernstein.core.agents.computer_use import ActionKind

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from bernstein.core.agents.computer_use import Action

__all__ = [
    "BrowserDriver",
    "BrowserDriverError",
    "BrowserDriverUnavailable",
    "BrowserProfile",
    "BrowserStepTimeout",
    "BrowserUseDriver",
    "PageState",
    "RecordedBrowserDriver",
    "browser_use_driver",
    "observe",
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
