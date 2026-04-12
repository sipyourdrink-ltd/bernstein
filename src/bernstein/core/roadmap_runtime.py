"""Backward-compatibility shim — moved to bernstein.core.planning.roadmap_runtime."""

import importlib as _importlib

from bernstein.core.planning.roadmap_runtime import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.planning.roadmap_runtime")


def __getattr__(name: str):
    return getattr(_real, name)
