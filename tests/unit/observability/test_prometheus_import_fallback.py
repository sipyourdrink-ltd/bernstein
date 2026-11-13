"""The Windows import fallback in ``observability.prometheus`` must not race.

The module imports ``prometheus_client`` on a daemon thread with a timeout, so
three things can happen and all three have to leave the module in one coherent
state: the import lands in time, it fails, or it lands *after* the module has
already given up and built its stubs. The third one broke `main`: the late
import used to rebind ``Counter`` to the real class while ``registry`` was
already a stub instance, and the real ``Counter.__init__`` calls
``registry.register(self)``.
"""

from __future__ import annotations

import builtins
import importlib
import inspect
import re
import sys
from collections.abc import Callable, Iterator
from threading import Event
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from types import ModuleType

MODULE = "bernstein.core.observability.prometheus"

_TIMEOUT = 0.05


class ImportSchedule:
    """Let the real import callback finish on either side of the timeout."""

    def __init__(self, *, late: bool) -> None:
        self.late = late
        self.done = Event()
        self.target: Callable[[], None] | None = None
        self.waits: list[float] = []

    def wait(self, timeout: float) -> bool:
        self.waits.append(timeout)
        return self.done.is_set()

    def thread(self, *, target: Callable[[], None], daemon: bool) -> SimpleNamespace:
        assert daemon
        return SimpleNamespace(start=lambda: self.start(target))

    def start(self, target: Callable[[], None]) -> None:
        assert self.target is None
        self.target = target
        if not self.late:
            self.finish()

    def finish(self) -> None:
        assert self.target is not None
        self.target()
        assert self.done.is_set(), "the import callback did not finish"


def _reimport(
    monkeypatch: pytest.MonkeyPatch,
    *,
    platform: str,
    schedule: ImportSchedule,
) -> ModuleType:
    """Reimport with a controlled worker only for this module's threading import."""
    real_import = builtins.__import__
    controlled_threading = SimpleNamespace(
        Thread=schedule.thread,
        Event=lambda: SimpleNamespace(set=schedule.done.set, wait=schedule.wait),
    )

    def controlled_import(name: str, globals: dict[str, Any] | None = None, *args: Any, **kwargs: Any) -> Any:
        if name == "threading" and globals is not None and globals.get("__name__") == MODULE:
            return controlled_threading
        return real_import(name, globals, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setenv("BERNSTEIN_PROMETHEUS_IMPORT_TIMEOUT", str(_TIMEOUT))
        patch.setattr(sys, "platform", platform)
        patch.setattr(builtins, "__import__", controlled_import)
        sys.modules.pop(MODULE, None)
        return importlib.import_module(MODULE)


@pytest.fixture(autouse=True)
def _restore_module() -> Iterator[None]:
    """Restore both ways of importing the original module after the test."""
    parent_name, _, attribute = MODULE.rpartition(".")
    parent = vars(importlib.import_module(parent_name))
    original = sys.modules.get(MODULE)
    original_attribute = parent.get(attribute)
    yield
    if original is not None:
        sys.modules[MODULE] = original
    else:
        sys.modules.pop(MODULE, None)
    if original_attribute is not None:
        parent[attribute] = original_attribute
    else:
        parent.pop(attribute, None)


def test_module_imports_when_the_windows_import_overruns_its_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression: this raised AttributeError at module scope.

    The stub registry was constructed, the late import rebound ``Counter`` to
    the real class, and constructing it called ``register`` on a stub that had
    no such method.
    """
    importlib.import_module("prometheus_client")
    schedule = ImportSchedule(late=True)
    module = _reimport(monkeypatch, platform="win32", schedule=schedule)
    assert schedule.waits == [_TIMEOUT]
    assert not schedule.done.is_set()

    assert module._PROMETHEUS_AVAILABLE is False
    schedule.finish()
    assert module._import_error is None
    assert module._import_result["Counter"].__module__.startswith("prometheus_client")
    assert module._PROMETHEUS_AVAILABLE is False


def test_a_late_windows_import_does_not_replace_the_stub_metric_classes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An import that lands after the timeout must be discarded, not adopted.

    A process that has already handed out stub metric objects cannot make them
    real retroactively, so a half-adopted result is worse than none.
    """
    importlib.import_module("prometheus_client")
    schedule = ImportSchedule(late=True)
    module = _reimport(monkeypatch, platform="win32", schedule=schedule)
    assert schedule.waits == [_TIMEOUT]
    assert not schedule.done.is_set()
    counter_at_import = module.Counter

    schedule.finish()
    assert module._import_error is None
    assert module._import_result["Counter"].__module__.startswith("prometheus_client")

    assert module.Counter is counter_at_import
    assert module.Counter.__module__ == MODULE, "a real prometheus class was adopted late"
    assert type(module.registry).__module__ == MODULE


def test_stub_metrics_still_record_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disabling metrics must disable them, not break the caller."""
    importlib.import_module("prometheus_client")
    schedule = ImportSchedule(late=True)
    module = _reimport(monkeypatch, platform="win32", schedule=schedule)
    assert schedule.waits == [_TIMEOUT]
    assert not schedule.done.is_set()

    module.tasks_total.labels(status="done", role="backend").inc()
    schedule.finish()
    assert module._import_error is None
    assert module._import_result["Counter"].__module__.startswith("prometheus_client")
    assert module.generate_latest(module.registry) == b""


def test_stub_registry_answers_every_call_this_module_makes_on_registry() -> None:
    """Enumerated from the source, so a new ``registry.<x>`` call is caught here.

    Hand-listing the methods would pass forever after someone adds a call the
    stub does not implement, which is exactly how the ``register`` gap shipped.
    """
    module = importlib.import_module(MODULE)
    source = inspect.getsource(module)

    called = set(re.findall(r"\bregistry\.([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", source))
    # Every real metric constructor registers itself into the registry it is given.
    called.add("register")

    stub_registry = module._StubCollectorRegistry()
    missing = sorted(name for name in called if not hasattr(stub_registry, name))
    assert not missing, f"stub registry is missing {missing}"


def test_a_windows_import_that_beats_the_timeout_is_adopted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive control: without this, always-stub would satisfy every test above."""
    pytest.importorskip("prometheus_client")
    schedule = ImportSchedule(late=False)
    module = _reimport(monkeypatch, platform="win32", schedule=schedule)
    assert schedule.waits == [_TIMEOUT]
    assert schedule.done.is_set()

    assert module._PROMETHEUS_AVAILABLE is True
    assert module.Counter.__module__.startswith("prometheus_client")


@pytest.mark.skipif(sys.platform == "win32", reason="this is the non-Windows path")
def test_import_off_windows_is_adopted_without_the_timeout_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive control: the ordinary path imports without a timeout worker.

    The host's own platform is used rather than a fabricated one, because
    third-party modules imported along the way branch on ``sys.platform`` when
    they load and refuse a platform the process did not start on.
    """
    pytest.importorskip("prometheus_client")
    schedule = ImportSchedule(late=True)
    module = _reimport(monkeypatch, platform=sys.platform, schedule=schedule)
    assert schedule.target is None
    assert schedule.waits == []

    assert module._PROMETHEUS_AVAILABLE is True
    assert module.Counter.__module__.startswith("prometheus_client")
