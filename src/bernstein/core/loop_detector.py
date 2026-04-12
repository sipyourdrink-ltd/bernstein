"""Backward-compatibility shim — moved to bernstein.core.observability.loop_detector."""

import importlib as _importlib

from bernstein.core.observability.loop_detector import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.observability.loop_detector")


def __getattr__(name: str):
    return getattr(_real, name)
