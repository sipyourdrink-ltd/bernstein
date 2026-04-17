"""Regression guard for audit-004: dead tick/concurrency guards stay deleted.

The orchestrator tick loop is serial (see ``orchestrator.py`` module
docstring). Both ``tick_guard`` and ``concurrency_guard`` were imported
nowhere except their own modules and the redirect map, so they were removed
in audit-004. This test catches an accidental reintroduction of the dead
modules or their redirect-map aliases.
"""

from __future__ import annotations

import importlib

import pytest

from bernstein.core import _REDIRECT_MAP


class TestGuardModulesRemoved:
    """Both guard modules must stay deleted until threaded ticks return."""

    @pytest.mark.parametrize(
        "module_path",
        [
            "bernstein.core.orchestration.tick_guard",
            "bernstein.core.orchestration.concurrency_guard",
        ],
    )
    def test_real_module_is_gone(self, module_path: str) -> None:
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(module_path)

    @pytest.mark.parametrize("alias", ["tick_guard", "concurrency_guard"])
    def test_redirect_alias_is_gone(self, alias: str) -> None:
        assert alias not in _REDIRECT_MAP

    @pytest.mark.parametrize(
        "shim_path",
        [
            "bernstein.core.tick_guard",
            "bernstein.core.concurrency_guard",
        ],
    )
    def test_shim_import_fails(self, shim_path: str) -> None:
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(shim_path)
