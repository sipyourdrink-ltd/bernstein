"""Backward-compatibility shim — moved to bernstein.core.quality.cycle_detector."""

import importlib as _importlib

from bernstein.core.quality.cycle_detector import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.quality.cycle_detector")


def __getattr__(name: str):
    return getattr(_real, name)
