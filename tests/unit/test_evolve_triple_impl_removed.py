"""Regression tests for audit-003: the two dead evolve implementations are gone.

Historically three parallel evolve implementations coexisted:

1. ``Orchestrator`` methods (inline, live) -- kept.
2. ``EvolveMixin`` in ``bernstein.core.orchestration.evolve_mode`` -- never mixed
   in, ~600 LOC of dead code. Deleted.
3. Free functions in ``bernstein.core.orchestration.orchestrator_evolve`` --
   only referenced from the equally dead ``orchestrator_tick`` module-level
   ``tick``/``_tick_internal``. Deleted.

These tests guard against anyone resurrecting the dead implementations.
"""

from __future__ import annotations

import importlib

import pytest

_DEAD_MODULES: tuple[str, ...] = (
    "bernstein.core.orchestration.evolve_mode",
    "bernstein.core.orchestration.orchestrator_evolve",
)


@pytest.mark.parametrize("module_name", _DEAD_MODULES)
def test_dead_evolve_module_cannot_be_imported(module_name: str) -> None:
    """Dead evolve modules must no longer be importable."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module_name)


@pytest.mark.parametrize("module_name", _DEAD_MODULES)
def test_dead_evolve_module_not_in_core_redirect_map(module_name: str) -> None:
    """Backward-compat redirect map must not advertise the dead modules."""
    from bernstein.core import _REDIRECT_MAP

    short_name = module_name.rsplit(".", maxsplit=1)[-1]
    assert short_name not in _REDIRECT_MAP, (
        f"{short_name!r} is still advertised in bernstein.core._REDIRECT_MAP; "
        "delete the entry so `from bernstein.core.<name>` no longer resolves."
    )
    assert module_name not in _REDIRECT_MAP.values(), (
        f"{module_name!r} is still a redirect target in bernstein.core._REDIRECT_MAP."
    )


def test_orchestrator_has_no_evolve_mixin_base() -> None:
    """``Orchestrator`` must be a plain class with no ``EvolveMixin`` base.

    The dead ``EvolveMixin`` was never actually mixed in; we guard against
    anyone re-introducing it by accident without a deliberate refactor
    (tracked as audit-009).
    """
    from bernstein.core.orchestration.orchestrator import Orchestrator

    base_names = {base.__name__ for base in Orchestrator.__mro__ if base is not Orchestrator}
    assert "EvolveMixin" not in base_names
    # The only ancestor of a regular class should be ``object``.
    assert base_names == {"object"}


def test_live_evolve_methods_still_exist_on_orchestrator() -> None:
    """The live evolve methods (the implementation we kept) must still be on
    ``Orchestrator``. If any of these vanish, the runtime evolve cycle is broken
    and this ticket's scope assumption ("leave live methods in orchestrator.py")
    has been violated.
    """
    from bernstein.core.orchestration.orchestrator import Orchestrator

    required = (
        "_check_evolve",
        "_replenish_backlog",
        "_evolve_run_tests",
        "_evolve_auto_commit",
        "_evolve_spawn_manager",
    )
    missing = [name for name in required if not hasattr(Orchestrator, name)]
    assert not missing, f"Orchestrator is missing live evolve methods: {missing}"


def test_orchestration_package_still_imports() -> None:
    """The orchestration sub-package must still import cleanly after the deletion.

    The ``__getattr__`` in ``orchestration/__init__.py`` iterates sibling modules;
    a straggler import of the deleted modules would surface here.
    """
    pkg = importlib.import_module("bernstein.core.orchestration")
    # Trigger the lazy module scan via a known-good attribute.
    assert pkg.orchestrator is not None  # type: ignore[attr-defined]
