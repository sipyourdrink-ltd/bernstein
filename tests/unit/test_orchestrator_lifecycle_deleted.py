"""Regression tests for audit-008.

The parallel extraction modules ``orchestrator_lifecycle`` and
``orchestrator_recovery`` were removed because they were never wired in:
the live implementations are inline methods on ``Orchestrator`` (and on
``orchestrator_cleanup``). These tests ensure the dead modules are not
reintroduced and that the legacy ``_REDIRECT_MAP`` entries stay gone, so
old imports fail loudly instead of silently falling back to un-called
parallel copies.
"""

from __future__ import annotations

import importlib

import pytest

from bernstein.core import _REDIRECT_MAP


def test_orchestrator_lifecycle_module_is_gone() -> None:
    """The parallel lifecycle module must not exist under any import path."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("bernstein.core.orchestration.orchestrator_lifecycle")
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("bernstein.core.orchestrator_lifecycle")


def test_orchestrator_recovery_module_is_gone() -> None:
    """The parallel recovery module must not exist under any import path."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("bernstein.core.orchestration.orchestrator_recovery")
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("bernstein.core.orchestrator_recovery")


def test_redirect_map_has_no_dead_entries() -> None:
    """Legacy redirects for the deleted modules must be removed."""
    assert "orchestrator_lifecycle" not in _REDIRECT_MAP
    assert "orchestrator_recovery" not in _REDIRECT_MAP


def test_live_cleanup_module_still_imports() -> None:
    """The real replacement (``orchestrator_cleanup``) must remain importable."""
    mod = importlib.import_module("bernstein.core.orchestration.orchestrator_cleanup")
    for name in ("cleanup", "drain_before_cleanup", "save_session_state"):
        assert hasattr(mod, name), f"orchestrator_cleanup.{name} missing"


def test_orchestrator_retains_live_methods() -> None:
    """The inline methods on Orchestrator (the actual live code paths) stay."""
    from bernstein.core.orchestration.orchestrator import Orchestrator

    for name in (
        "_recover_from_wal",
        "_reconcile_claimed_tasks",
        "_save_session_state",
        "_drain_before_cleanup",
    ):
        assert callable(getattr(Orchestrator, name, None)), (
            f"Orchestrator.{name} is not defined — audit-008 rollback?"
        )
