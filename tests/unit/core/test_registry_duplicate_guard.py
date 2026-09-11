"""Regression tests for the shared registry duplicate-id guard (issue #5104).

``bernstein.core.registry_guard.DuplicateGuard`` is the one duplicate-check
helper that registry classes under ``src/bernstein`` compose into their
``register`` method, instead of each reinventing (or, as with
``AgentRegistry.register_definition``, promising in its docstring and then
skipping) the check. This slice migrates ``AgentRegistry`` only; the guard
itself is exercised directly, and through that one registry via a fixture
package that fails to import when it registers the same id twice.
"""

from __future__ import annotations

import importlib
import sys
from typing import Any

import pytest
from bernstein.core.models import ModelConfig

from bernstein.agents.registry import AgentDefinition, AgentRegistry
from bernstein.core.registry_guard import DuplicateGuard, DuplicateRegistrationError

_FIXTURE_PACKAGE = "tests.fixtures.duplicate_registry_package"


def _unload_fixture_package() -> None:
    """Drop the fixture package (and its submodules) from ``sys.modules``.

    Each test that imports it needs a fresh run of its module-level
    registration code, and a module that failed to import mid-way can leave
    a partial entry in ``sys.modules`` that would otherwise short-circuit
    the next import.
    """
    for name in list(sys.modules):
        if name == _FIXTURE_PACKAGE or name.startswith(_FIXTURE_PACKAGE + "."):
            del sys.modules[name]


@pytest.fixture(autouse=True)
def _isolate_fixture_package() -> None:
    _unload_fixture_package()
    yield
    _unload_fixture_package()


def _make_definition(name: str, role: str = "role") -> AgentDefinition:
    return AgentDefinition(
        name=name,
        role=role,
        model_config=ModelConfig(model="sonnet", effort="normal"),
        version="1.0.0",
    )


# --- DuplicateGuard, exercised directly ---


class TestDuplicateGuard:
    def test_first_registration_succeeds(self) -> None:
        guard = DuplicateGuard("widgets")
        guard.register("a", "pkg.mod_a")  # must not raise
        assert guard.module_path_for("a") == "pkg.mod_a"

    def test_second_registration_of_same_key_raises(self) -> None:
        guard = DuplicateGuard("widgets")
        guard.register("a", "pkg.mod_a")
        with pytest.raises(DuplicateRegistrationError):
            guard.register("a", "pkg.mod_b")

    def test_duplicate_error_names_both_module_paths(self) -> None:
        guard = DuplicateGuard("widgets")
        guard.register("a", "pkg.mod_a")
        with pytest.raises(DuplicateRegistrationError) as excinfo:
            guard.register("a", "pkg.mod_b")
        message = str(excinfo.value)
        assert "pkg.mod_a" in message
        assert "pkg.mod_b" in message

    def test_duplicate_registration_error_is_a_value_error(self) -> None:
        """Callers that already ``except ValueError`` keep working unchanged."""
        assert issubclass(DuplicateRegistrationError, ValueError)

    def test_custom_error_type_is_honored(self) -> None:
        class _CustomError(ValueError):
            pass

        guard = DuplicateGuard("widgets")
        guard.register("a", "pkg.mod_a")
        with pytest.raises(_CustomError):
            guard.register("a", "pkg.mod_b", error_type=_CustomError)

    def test_forget_allows_re_registration(self) -> None:
        guard = DuplicateGuard("widgets")
        guard.register("a", "pkg.mod_a")
        guard.forget("a")
        guard.register("a", "pkg.mod_b")  # must not raise
        assert guard.module_path_for("a") == "pkg.mod_b"

    def test_different_keys_do_not_collide(self) -> None:
        guard = DuplicateGuard("widgets")
        guard.register("a", "pkg.mod_a")
        guard.register("b", "pkg.mod_b")  # must not raise
        assert guard.module_path_for("b") == "pkg.mod_b"


# --- The exact promise-vs-behavior gap named in the issue ---


class TestAgentRegistryDuplicateGuard:
    def test_agent_registry_raises_not_warns_on_duplicate_definition(self) -> None:
        """``register_definition`` must do what its own docstring promises.

        On main this logs a warning and silently overwrites; it must raise
        instead, matching the ``Raises: ValueError`` in its docstring
        (``DuplicateRegistrationError`` is a ``ValueError`` subclass).
        """
        registry = AgentRegistry()
        registry.register_definition(_make_definition("dup-agent", role="first"))

        with pytest.raises(ValueError, match="dup-agent"):
            registry.register_definition(_make_definition("dup-agent", role="second"))

        # The first registration must survive untouched -- no silent overwrite.
        assert registry.get_definition("dup-agent").role == "first"  # type: ignore[union-attr]

    def test_distinct_names_register_independently(self) -> None:
        registry = AgentRegistry()
        registry.register_definition(_make_definition("agent-one"))
        registry.register_definition(_make_definition("agent-two"))  # must not raise
        assert registry.get_definition("agent-two") is not None

    def test_unregister_then_reregister_is_allowed(self) -> None:
        registry = AgentRegistry()
        registry.register_definition(_make_definition("temp-agent"))
        registry.unregister_definition("temp-agent")
        registry.register_definition(_make_definition("temp-agent"))  # must not raise
        assert registry.get_definition("temp-agent") is not None


# --- Load-bearing: a fixture package that double-registers fails to import ---


def test_duplicate_id_registration_raises_at_import() -> None:
    """A package registering one id twice from two modules fails to import.

    Fails on main today: ``AgentRegistry.register_definition`` warns and
    overwrites instead of raising, so importing the fixture package
    succeeds when it must not.
    """
    with pytest.raises(DuplicateRegistrationError) as excinfo:
        importlib.import_module(_FIXTURE_PACKAGE)

    message = str(excinfo.value)
    assert f"{_FIXTURE_PACKAGE}.first" in message
    assert f"{_FIXTURE_PACKAGE}.second" in message


# --- Slices migrated under issue #5104 ---


def _dummy_tracker_factory(**kwargs: Any) -> Any:
    return None


class TestTrackersRegistryDuplicateGuard:
    def test_tracker_registry_duplicate_raises_duplicate_tracker_error(self) -> None:
        from bernstein.core.trackers.registry import DuplicateTrackerError, TrackerRegistry

        registry = TrackerRegistry()
        registry.register("test-tracker", _dummy_tracker_factory)

        with pytest.raises(DuplicateTrackerError) as excinfo:
            registry.register("test-tracker", _dummy_tracker_factory)

        assert "test-tracker" in str(excinfo.value)
        assert __name__ in str(excinfo.value)

    def test_tracker_registry_overwrite_allows_reregistration(self) -> None:
        from bernstein.core.trackers.registry import TrackerRegistry

        registry = TrackerRegistry()
        registry.register("test-tracker", _dummy_tracker_factory, summary="first")
        registry.register("test-tracker", _dummy_tracker_factory, summary="second", overwrite=True)
        assert registry.get("test-tracker").summary == "second"

    def test_tracker_registry_unregister_allows_reregistration(self) -> None:
        from bernstein.core.trackers.registry import TrackerRegistry

        registry = TrackerRegistry()
        registry.register("test-tracker", _dummy_tracker_factory)
        registry.unregister("test-tracker")
        registry.register("test-tracker", _dummy_tracker_factory)  # must not raise
        assert registry.get("test-tracker") is not None


class TestSandboxRegistryDuplicateGuard:
    def test_sandbox_registry_duplicate_raises(self) -> None:
        from bernstein.core.sandbox.registry import _Registry

        registry = _Registry()
        dummy_backend = object()  # type: ignore[arg-type]
        registry.register("test-sandbox", dummy_backend)

        with pytest.raises(DuplicateRegistrationError) as excinfo:
            registry.register("test-sandbox", dummy_backend)

        assert "Duplicate sandbox backend" in str(excinfo.value)
        assert "test-sandbox" in str(excinfo.value)

    def test_sandbox_registry_unregister_allows_reregistration(self) -> None:
        from bernstein.core.sandbox.registry import _Registry

        registry = _Registry()
        dummy_backend = object()  # type: ignore[arg-type]
        registry.register("test-sandbox", dummy_backend)
        registry.unregister("test-sandbox")
        registry.register("test-sandbox", dummy_backend)  # must not raise


class TestStorageRegistryDuplicateGuard:
    def test_storage_registry_duplicate_raises(self) -> None:
        from bernstein.core.storage.registry import _Registry

        registry = _Registry()
        dummy_sink = object()  # type: ignore[arg-type]
        registry.register("test-sink", dummy_sink)

        with pytest.raises(DuplicateRegistrationError) as excinfo:
            registry.register("test-sink", dummy_sink)

        assert "Duplicate artifact sink" in str(excinfo.value)
        assert "test-sink" in str(excinfo.value)

    def test_storage_registry_unregister_allows_reregistration(self) -> None:
        from bernstein.core.storage.registry import _Registry

        registry = _Registry()
        dummy_sink = object()  # type: ignore[arg-type]
        registry.register("test-sink", dummy_sink)
        registry.unregister("test-sink")
        registry.register("test-sink", dummy_sink)  # must not raise
