"""Regression tests for audit-007: heartbeat_v2 removal.

The ``heartbeat_v2`` module and its ping/pong protocol classes were
removed as part of audit-007 (unbuilt road-055 protocol). These tests
guard against accidental resurrection or broken imports in
``bernstein.core``.
"""

from __future__ import annotations

import importlib

import pytest


def test_heartbeat_v2_module_is_absent() -> None:
    """Direct import of the removed module must fail."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("bernstein.core.agents.heartbeat_v2")


def test_heartbeat_v2_legacy_shim_is_absent() -> None:
    """The legacy ``bernstein.core.heartbeat_v2`` shim must not resolve."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("bernstein.core.heartbeat_v2")


def test_redirect_map_excludes_heartbeat_v2() -> None:
    """The core redirect map must not reference the removed module."""
    core_pkg = importlib.import_module("bernstein.core")
    redirect_map: dict[str, str] = core_pkg._REDIRECT_MAP  # type: ignore[attr-defined]
    assert "heartbeat_v2" not in redirect_map
    for target in redirect_map.values():
        assert not target.endswith(".heartbeat_v2")


def test_production_heartbeat_still_importable() -> None:
    """The in-use heartbeat module must remain importable."""
    module = importlib.import_module("bernstein.core.agents.heartbeat")
    assert module is not None
