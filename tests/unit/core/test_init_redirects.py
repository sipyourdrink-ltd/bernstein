"""Tests for core/__init__.py redirect map."""

from __future__ import annotations

import importlib

import pytest


def test_workload_prediction_redirect_removed() -> None:
    """The workload_prediction redirect must be removed from _REDIRECT_MAP."""
    core_pkg = importlib.import_module("bernstein.core")
    redirect_map: dict[str, str] = core_pkg._REDIRECT_MAP  # type: ignore[attr-defined]
    assert "workload_prediction" not in redirect_map


def test_workload_prediction_legacy_shim_absent() -> None:
    """The legacy bernstein.core.workload_prediction shim must not resolve."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("bernstein.core.workload_prediction")


def test_other_redirects_still_present() -> None:
    """Ensure other common redirects still exist to prevent false positives."""
    core_pkg = importlib.import_module("bernstein.core")
    redirect_map: dict[str, str] = core_pkg._REDIRECT_MAP  # type: ignore[attr-defined]
    # Check a few key redirects are still present
    assert "workspace" in redirect_map
    assert "worktree" in redirect_map
    assert redirect_map["workspace"] == "bernstein.core.persistence.workspace"
    assert redirect_map["worktree"] == "bernstein.core.git.worktree"
