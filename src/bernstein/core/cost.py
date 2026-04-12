"""Backward-compatibility shim — moved to bernstein.core.cost.cost."""

import importlib as _importlib

from bernstein.core.cost.cost import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.cost.cost")


def __getattr__(name: str):
    return getattr(_real, name)
