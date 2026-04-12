"""Backward-compatibility shim — moved to bernstein.core.planning.planner."""

import importlib as _importlib

from bernstein.core.planning.planner import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.planning.planner")


def __getattr__(name: str):
    return getattr(_real, name)
