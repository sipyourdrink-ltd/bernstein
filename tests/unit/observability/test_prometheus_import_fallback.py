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
import time
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from types import ModuleType

MODULE = "bernstein.core.observability.prometheus"

# Well under any real import, so a "slow" import in these tests is still fast.
_TIMEOUT = "0.05"
_SLOWER_THAN_TIMEOUT = 0.35


def _reimport(
    monkeypatch: pytest.MonkeyPatch,
    *,
    platform: str,
    import_delay: float = 0.0,
) -> ModuleType:
    """Import the module fresh under a chosen platform and import speed.

    ``sys.platform`` and ``__import__`` are restored before this returns rather
    than at test teardown. Leaving them patched for the rest of the test lets
    another fixture's teardown import a third-party module under a platform the
    process never started on, which fails in a place that has nothing to do with
    what is being tested.
    """
    real_import = builtins.__import__
    real_platform = sys.platform

    def maybe_slow_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "prometheus_client" and import_delay:
            time.sleep(import_delay)
        return real_import(name, *args, **kwargs)

    monkeypatch.setenv("BERNSTEIN_PROMETHEUS_IMPORT_TIMEOUT", _TIMEOUT)
    sys.modules.pop(MODULE, None)
    sys.platform = platform  # type: ignore[misc]
    builtins.__import__ = maybe_slow_import  # type: ignore[assignment]
    try:
        return importlib.import_module(MODULE)
    finally:
        builtins.__import__ = real_import  # type: ignore[assignment]
        sys.platform = real_platform  # type: ignore[misc]


@pytest.fixture(autouse=True)
def _restore_module() -> Any:
    """Put the original module object back for everything else in the process.

    Restoring the object rather than re-importing it matters: a re-import here
    would run while this test's platform patch may still be in effect, and
    third-party modules that branch on ``sys.platform`` at import time refuse to
    load under a platform they were not started on.
    """
    original = sys.modules.get(MODULE)
    yield
    if original is not None:
        sys.modules[MODULE] = original
    else:
        sys.modules.pop(MODULE, None)


def test_module_imports_when_the_windows_import_overruns_its_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression: this raised AttributeError at module scope.

    The stub registry was constructed, the late import rebound ``Counter`` to
    the real class, and constructing it called ``register`` on a stub that had
    no such method.
    """
    module = _reimport(monkeypatch, platform="win32", import_delay=_SLOWER_THAN_TIMEOUT)

    assert module._PROMETHEUS_AVAILABLE is False
    # Give the daemon thread more than enough time to finish and try to interfere.
    time.sleep(_SLOWER_THAN_TIMEOUT)
    assert module._PROMETHEUS_AVAILABLE is False


def test_a_late_windows_import_does_not_replace_the_stub_metric_classes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An import that lands after the timeout must be discarded, not adopted.

    A process that has already handed out stub metric objects cannot make them
    real retroactively, so a half-adopted result is worse than none.
    """
    module = _reimport(monkeypatch, platform="win32", import_delay=_SLOWER_THAN_TIMEOUT)
    counter_at_import = module.Counter

    time.sleep(_SLOWER_THAN_TIMEOUT)

    assert module.Counter is counter_at_import
    assert module.Counter.__module__ == MODULE, "a real prometheus class was adopted late"
    assert type(module.registry).__module__ == MODULE


def test_stub_metrics_still_record_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disabling metrics must disable them, not break the caller."""
    module = _reimport(monkeypatch, platform="win32", import_delay=_SLOWER_THAN_TIMEOUT)

    module.tasks_total.labels(status="done", role="backend").inc()
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
    module = _reimport(monkeypatch, platform="win32")

    assert module._PROMETHEUS_AVAILABLE is True
    assert module.Counter.__module__.startswith("prometheus_client")


@pytest.mark.skipif(sys.platform == "win32", reason="this is the non-Windows path")
def test_a_slow_import_off_windows_is_still_adopted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive control: the ordinary path has no timeout, so slow is fine.

    The host's own platform is used rather than a fabricated one, because
    third-party modules imported along the way branch on ``sys.platform`` when
    they load and refuse a platform the process did not start on.
    """
    pytest.importorskip("prometheus_client")
    module = _reimport(monkeypatch, platform=sys.platform, import_delay=_SLOWER_THAN_TIMEOUT)

    assert module._PROMETHEUS_AVAILABLE is True
    assert module.Counter.__module__.startswith("prometheus_client")
