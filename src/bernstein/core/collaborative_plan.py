"""Backward-compatibility shim — moved to bernstein.core.planning.collaborative_plan."""

import importlib as _importlib

from bernstein.core.planning.collaborative_plan import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.planning.collaborative_plan")


def __getattr__(name: str):
    return getattr(_real, name)
