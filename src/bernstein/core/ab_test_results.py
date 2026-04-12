"""Backward-compatibility shim — moved to bernstein.core.quality.ab_test_results."""

import importlib as _importlib

from bernstein.core.quality.ab_test_results import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.quality.ab_test_results")


def __getattr__(name: str):
    return getattr(_real, name)
