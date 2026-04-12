"""Backward-compatibility shim — moved to bernstein.core.routing.fast_mode."""

import importlib as _importlib

from bernstein.core.routing.fast_mode import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.routing.fast_mode")


def __getattr__(name: str):
    return getattr(_real, name)
