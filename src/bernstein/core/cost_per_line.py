"""Backward-compatibility shim — moved to bernstein.core.cost.cost_per_line."""

import importlib as _importlib

from bernstein.core.cost.cost_per_line import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.cost.cost_per_line")


def __getattr__(name: str):
    return getattr(_real, name)
