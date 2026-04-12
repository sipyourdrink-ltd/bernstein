"""Tests for bernstein.core.defaults override/reset mechanics."""

from __future__ import annotations

import pytest

from bernstein.core import defaults
from bernstein.core.defaults import override, reset


def test_override_scalar() -> None:
    reset()
    assert defaults.ORCHESTRATOR.tick_interval_s == 3.0
    override("orchestrator", {"tick_interval_s": 5.0})
    assert defaults.ORCHESTRATOR.tick_interval_s == 5.0
    reset()


def test_override_dict_merges() -> None:
    reset()
    override("task", {"scope_timeout_s": {"large": 7200}})
    assert defaults.TASK.scope_timeout_s["large"] == 7200
    assert defaults.TASK.scope_timeout_s["small"] == 900  # unchanged
    reset()


def test_override_invalid_section() -> None:
    with pytest.raises(KeyError):
        override("nonexistent", {"foo": 1})


def test_override_invalid_field() -> None:
    reset()
    with pytest.raises(AttributeError):
        override("orchestrator", {"bogus_field": 1})
    reset()
